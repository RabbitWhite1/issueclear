import os
import re
import time  # retained for potential future timing metrics
from datetime import datetime, timezone
from typing import Iterator, Optional

import requests
from rich.progress import BarColumn  # BarColumn currently unused but kept if needed for future customization

from issueclear.db import RepoDatabase
from issueclear.scrape.common import IssueScraper
from issueclear.utils import create_progress, polite_sleep

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

    def list_issues(
        self,
        since_iso: Optional[str] = None,
        per_page: int = 100,
        sortby: str = "updated",
    ) -> Iterator[dict]:
        """Unified listing of issues & PRs (optionally updated since a timestamp).

        Args:
            since_iso: If provided, only items updated at or after this ISO8601 timestamp.
            state: GitHub issues state filter (open, closed, all).
            per_page: Page size (max 100).
        Yields:
            Raw JSON dict for each issue / PR.
        """
        if sortby not in {"updated", "created"}:
            raise ValueError("sort must be 'updated' or 'created'")
        # Initial URL
        next_url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/issues"
        params = {
            "per_page": per_page,
            "sort": sortby,
            "direction": "asc",
            "state": "all",
        }
        if since_iso and sortby == "updated":
            params["since"] = since_iso
            print("Using since filter:", since_iso)
        elif since_iso and sortby == "created":
            print(f"[{datetime.now()}] Warning: ignoring since filter when sorting by {sortby}")
        while next_url:
            resp = requests.get(next_url, headers=self.headers, params=params)
            # After first request, subsequent requests should follow Link header; clear params once URL already has its own query.
            if resp.status_code == 403:
                print(f"[{datetime.now()}] Rate limit hit listing issues: sleeping 1h")
                time.sleep(3600)
                continue
            if resp.status_code == 422:
                raise RuntimeError(f"[{datetime.now()}] Unexpected 422 listing issues: {resp.status_code}, {resp.text}")
            if resp.status_code != 200:
                raise RuntimeError(f"[{datetime.now()}] Failed to list issues: {resp.status_code}, {resp.text}")
            params = None  # ensure we don't re-append params when using full URLs from Link header
            batch = resp.json()
            if not batch:
                break
            for issue in batch:
                yield issue
            # Parse Link header for rel="next"
            next_url = self._parse_next_link(resp.headers.get("Link"))
            polite_sleep(base=0.25)

    def list_comments(self, issue_number: int, since_iso: Optional[str] = None, per_page: int = 100) -> Iterator[dict]:
        base_url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments"
        params = {"per_page": per_page}
        if since_iso:
            params["since"] = since_iso
        next_url = base_url
        while next_url:
            resp = requests.get(next_url, headers=self.headers, params=params)
            if resp.status_code == 403:
                print(f"[{datetime.now()}] Failed to list comments for issue {issue_number}: {resp.status_code}, {resp.text}")
                print(f"[{datetime.now()}] Wait for 1 hour until rate limit recovers.")
                time.sleep(3600)
                print(f"[{datetime.now()}] Resuming.")
                continue
            if resp.status_code == 404:
                print(f"[{datetime.now()}] Issue {issue_number} not found (404). Maybe jitter. Retry within 10s.")
                time.sleep(10)
                continue
            if resp.status_code != 200:
                raise RuntimeError(
                    f"[{datetime.now()}] Failed to list comments for issue {issue_number}: {resp.status_code}, {resp.text}\n"
                    f"The request is {next_url} with params {params}"
                )
            params = None
            data = resp.json()
            if not data:
                break
            for c in data:
                yield c
            next_url = self._parse_next_link(resp.headers.get("Link"))
            polite_sleep(base=0.15)

    # Internal helpers -------------------------------------------------
    def _parse_next_link(self, link_header: Optional[str]) -> Optional[str]:
        """Extract the rel="next" URL from a GitHub Link header.

        Returns None when no next page is available or header absent.
        """
        if not link_header:
            return None
        m = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
        return m.group(1) if m else None

    def get_issue_total_count(self, since_iso: Optional[str] = None, sortby: str = "updated") -> Optional[int]:
        """Return total number of issues + pull requests.

        When since_iso is provided, use the GitHub Search API to approximate the count of issues/PRs
        filtered by either updated or created timestamp depending on the active sort field.

        Strategy:
        - Without since_iso: run a single GraphQL query to fetch total issues + pull requests (all states) and sum.
        - With since_iso & sort == 'updated': search twice (is:issue / is:pr) using updated:>= qualifier.
        - With since_iso & sort == 'created': search twice using created:>= qualifier.

        Notes:
        - Search API total_count is an estimate but generally good enough for a progress bar.
        - We intentionally keep per_page=1 to minimize payload; total_count unaffected by that parameter.
        - On any failure we return None so caller can fallback to indeterminate progress.
        """
        if sortby not in {"updated", "created"}:
            raise ValueError("sortby must be 'updated' or 'created'")
        if since_iso:
            qualifier = "updated" if sortby == "updated" else "created"
            try:
                q = f"repo:{self.owner}/{self.repo} {qualifier}:>={since_iso}"
                resp = requests.get(
                    f"{GITHUB_API}/search/issues",
                    headers=self.headers,
                    params={
                        "q": q,
                        "per_page": 1,
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    print(f"GitHub search API error {resp.status_code}: {resp.text[:200]}")
                    return None
                data = resp.json()
                return data.get("total_count", 0)
            except (requests.RequestException, ValueError) as e:
                print(f"GitHub search API request failed: {e}")
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
                print(f"GitHub GraphQL API error {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            if "errors" in data:
                print(f"GitHub GraphQL errors: {data['errors']}")
                return None
            repo_info = data.get("data", {}).get("repository", {})
            issues_total = repo_info.get("issues", {}).get("totalCount", 0)
            prs_total = repo_info.get("pullRequests", {}).get("totalCount", 0)
            return issues_total + prs_total
        except (requests.RequestException, ValueError) as e:
            print(f"GitHub GraphQL request failed: {e}")
            return None

    def incremental_sync(
        self,
        db: RepoDatabase,
        limit: Optional[int] = None,
        force_all: bool = False,
        sortby: str = "updated",
    ):
        """Perform incremental sync for this repository into per-repo SQLite db.

        Args:
            db: RepoDatabase instance.
            limit: Optional maximum number of issues/PRs to process this run (for very large repos / rate friendliness).

        Always displays a Rich progress bar. Returns a dict with processed_issues and last_updated.
        If limit is provided progress total is min(estimated_total, limit) when an estimate exists.
        """
        if force_all:
            last_issue_sync = None
        else:
            last_issue_sync = db.get_last_issue_sync()
        issue_iter = (
            self.list_issues(since_iso=last_issue_sync, sortby=sortby) if last_issue_sync else self.list_issues(sortby=sortby)
        )
        total_estimate = self.get_issue_total_count(last_issue_sync, sortby=sortby)
        with create_progress() as progress:
            display_total = None
            if total_estimate and limit:
                display_total = min(total_estimate, limit)
            elif total_estimate:
                display_total = total_estimate
            elif limit:
                display_total = limit
            task_id = progress.add_task("Sync Issues", total=display_total)
            max_cursor: Optional[str] = last_issue_sync
            processed = 0
            for issue_json in issue_iter:
                issue_id, inserted, changed = db.upsert_issue(issue_json)
                processed += 1
                progress.update(task_id, advance=1)
                if limit and processed >= limit:
                    break
                # Determine timestamp field based on sort preference
                if sortby == "updated":
                    ts = issue_json.get("updated_at") or issue_json.get("created_at")
                else:  # created sort
                    ts = None  # We don't update timestamp under create mode, because we need a complete creation.
                if ts and (max_cursor is None or ts > max_cursor):
                    max_cursor = ts
                    # Persist progress each advancement for crash resilience
                    db.update_last_issue_sync(max_cursor)

                comments_count = issue_json.get("comments", 0) or 0
                if comments_count and (inserted or changed):
                    for comment_json in self.list_comments(issue_json["number"]):
                        db.upsert_comment(issue_id, comment_json)
            return {"processed_issues": processed, "last_updated": max_cursor}
