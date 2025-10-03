import os
import time  # retained for potential future timing metrics
from datetime import datetime
from typing import Iterator, Optional

import requests
from rich.progress import BarColumn  # BarColumn currently unused but kept if needed for future customization
from issueclear.utils import create_progress, polite_sleep

from issueclear.db import RepoDatabase
from issueclear.scrape.common import IssueScraper

GITHUB_API = "https://api.github.com"  # kept for backward compatibility
GITHUB_API_VERSION = "2022-11-28"


class GitHubIssueScraper(IssueScraper):
    provider_name = "github"
    """GitHub issue & comment scraper with incremental sync support.

    All GitHub-specific HTTP operations are encapsulated as methods on this class
    (including counting, listing, and per-issue retrieval) so callers need not
    use module-level helpers.
    """

    def __init__(self, owner: str, repo: str):
        super().__init__()
        self.token = os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise ValueError("GITHUB_TOKEN environment variable not set")
        self.headers = {
            "Accept": "application/vnd.github+json",  # recommended media type
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "issueclear-github-scraper",
        }
        self.owner = owner
        self.repo = repo

    def list_issues(self, since_iso: Optional[str] = None, state: str = "all", per_page: int = 100) -> Iterator[dict]:
        """Unified listing of issues & PRs (optionally updated since a timestamp).

        Args:
            since_iso: If provided, only items updated at or after this ISO8601 timestamp.
            state: GitHub issues state filter (open, closed, all).
            per_page: Page size (max 100).
        Yields:
            Raw JSON dict for each issue / PR.
        """
        page = 1
        while True:
            # Explicitly sort by updated ascending so incremental sync with a limit is deterministic.
            params = {"state": state, "per_page": per_page, "page": page, "sort": "updated", "direction": "asc"}
            if since_iso:
                params["since"] = since_iso
            url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/issues"
            resp = requests.get(url, headers=self.headers, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to list issues {params=}: {resp.status_code}, {resp.text}")
            batch = resp.json()
            if not batch:
                break
            for issue in batch:
                yield issue
            if len(batch) < per_page:
                break
            page += 1
            polite_sleep(base=0.25)  # courteous pacing for issues listing

    def list_comments(self, issue_number: int, since_iso: Optional[str] = None, per_page: int = 100) -> Iterator[dict]:
        page = 1
        while True:
            params = {"per_page": per_page, "page": page}
            if since_iso:
                params["since"] = since_iso
            url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments"
            resp = requests.get(url, headers=self.headers, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to list comments for issue {issue_number}: {resp.status_code}, {resp.text}")
            data = resp.json()
            if not data:
                break
            for c in data:
                yield c
            if len(data) < per_page:
                break
            page += 1
            polite_sleep(base=0.15)  # slightly faster but still polite for comments

    def get_issue_total_count(self, since_iso: Optional[str] = None) -> Optional[int]:
        """Return total number of issues + pull requests, optionally filtered by last updated time.

        Strategy:
        - Without since_iso: use a single GraphQL query to retrieve issues.totalCount and pullRequests.totalCount
          (pull requests include OPEN, CLOSED, MERGED states) and sum them.
        - With since_iso: GraphQL only supports a since filter for issues (via filterBy). There is no direct
          equivalent for pull requests. For an updated-since estimate we fall back to the REST Search API twice:
            * search/issues?q=repo:owner/repo is:issue updated:>=since
            * search/issues?q=repo:owner/repo is:pr    updated:>=since
          and sum their total_count fields. If any request fails we return None so the caller can display an indeterminate progress bar.

        Returns None on any failure so progress can remain indeterminate rather than misleading.
        """
        if since_iso:
            try:
                base = f"repo:{self.owner}/{self.repo} updated:>={since_iso}"
                totals = 0
                for kind in ("issue", "pr"):
                    q = f"{base} is:{kind}"
                    resp = requests.get(
                        f"{GITHUB_API}/search/issues",
                        headers=self.headers,
                        params={"q": q, "per_page": 1},  # per_page=1 for minimal payload; total_count unaffected
                        timeout=15,
                    )
                    if resp.status_code != 200:
                        return None
                    data = resp.json()
                    # If incomplete_results is True we still have a total_count that's a best effort; accept it.
                    totals += data.get("total_count", 0)
                return totals
            except Exception:
                return None
        # No since filter: use GraphQL (fewer rate limit costs than search)
        graphql_url = "https://api.github.com/graphql"
        query = (
            "query($owner:String!,$repo:String!){repository(owner:$owner,name:$repo){"  # noqa: E501
            "issues(states:[OPEN,CLOSED]){totalCount}"  # issues (excludes PRs)
            "pullRequests(states:[OPEN,CLOSED,MERGED]){totalCount}"  # pull requests
            "}}"
        )
        variables = {"owner": self.owner, "repo": self.repo}
        try:
            resp = requests.post(
                graphql_url,
                headers={**self.headers, "Content-Type": "application/json"},
                json={"query": query, "variables": variables},
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if "errors" in data:
                return None
            repo_info = data.get("data", {}).get("repository", {})
            issues_total = repo_info.get("issues", {}).get("totalCount", 0)
            prs_total = repo_info.get("pullRequests", {}).get("totalCount", 0)
            return issues_total + prs_total
        except Exception:
            return None

    def incremental_sync(self, db: RepoDatabase, limit: Optional[int] = None):
        """Perform incremental sync for this repository into per-repo SQLite db.

        Args:
            db: RepoDatabase instance.
            limit: Optional maximum number of issues/PRs to process this run (for very large repos / rate friendliness).

        Always displays a Rich progress bar. Returns a dict with processed_issues and last_updated.
        If limit is provided progress total is min(estimated_total, limit) when an estimate exists.
        """
        last_issue_sync = db.get_last_issue_sync()
        issue_iter = self.list_issues(since_iso=last_issue_sync) if last_issue_sync else self.list_issues()

        total_estimate = self.get_issue_total_count(last_issue_sync)

        with create_progress() as progress:
            display_total = None
            if total_estimate and limit:
                display_total = min(total_estimate, limit)
            elif total_estimate:
                display_total = total_estimate
            elif limit:
                display_total = limit
            task_id = progress.add_task("Sync Issues", total=display_total)
            max_updated: Optional[str] = last_issue_sync
            processed = 0
            for issue_json in issue_iter:
                issue_id, inserted, changed = db.upsert_issue(issue_json)
                processed += 1
                if progress is not None:
                    progress.update(task_id, advance=1)
                if limit and processed >= limit:
                    break
                gh_updated = issue_json.get("updated_at")
                if gh_updated and (max_updated is None or gh_updated > max_updated):
                    max_updated = gh_updated

                comments_count = issue_json.get("comments", 0) or 0
                if comments_count and (inserted or changed):
                    for comment_json in self.list_comments(issue_json["number"]):
                        db.upsert_comment(issue_id, comment_json)
            if max_updated:
                db.update_last_issue_sync(max_updated)
            return {"processed_issues": processed, "last_updated": max_updated}
