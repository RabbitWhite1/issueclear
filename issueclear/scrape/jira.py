import os
import time
from datetime import datetime
from typing import Iterator, Optional

import requests
from rich.progress import (
    MofNCompleteColumn,
)  # retained for potential future per-task customization

from issueclear.db import RepoDatabase
from issueclear.scrape.common import IssueScraper
from issueclear.utils import create_progress, polite_sleep

# Basic JIRA REST API assumptions:
# - Base URL provided via JIRA_BASE_URL env (e.g. https://jira.mongodb.org)
# - We treat 'owner' as organization/company marker (e.g. mongodb, apache), and 'project' as JIRA project key (e.g. SERVER, PYTHON)
#   so DB path becomes platform/owner/project.sqlite (e.g. jira/mongodb/SERVER.sqlite)
# - Authentication: optional basic auth via JIRA_USER / JIRA_TOKEN (API token or password). If absent, we do anonymous requests.
# - Incremental sync uses 'updated' field with JQL updated >= "timestamp". JIRA timestamps are in ISO8601 with timezone, we'll store as given.

ISO_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
]


def parse_jira_datetime(value: str) -> str:
    if not value:
        return value
    for fmt in ISO_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            # Normalize to UTC ISO8601 like GitHub (no microseconds for consistency)
            return dt.astimezone().isoformat()
        except Exception:
            continue
    return value  # fallback


class JiraIssueScraper(IssueScraper):
    provider_name = "jira"

    def __init__(self, owner: str, project: str, base_url: Optional[str] = None):
        super().__init__()
        self.project = project  # project contains the JIRA project key
        self.owner = owner  # owner is organization/company marker
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "issueclear-jira-scraper",
        }

    def _request(self, method: str, path: str, **kwargs):
        """Issue a JIRA REST request (anonymous only)."""
        url = f"{self.base_url}{path}"
        resp = requests.request(method, url, headers=self.headers, timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"JIRA API error {resp.status_code} {url}: {resp.text[:500]}"
            )
        return resp

    def list_issues(self, since_iso: Optional[str] = None) -> Iterator[dict]:
        # Use JQL with project and updated filter.
        jql = f"project={self.project} ORDER BY updated ASC"
        if since_iso:
            reformatted = self._format_updated_since(since_iso)
            jql = f"project={self.project} AND updated >= '{reformatted}' ORDER BY updated ASC"
        start_at = 0
        while True:
            params = {
                "jql": jql,
                "startAt": start_at,
                # Minimize payload: only essential fields (summary, updated, created, status, comment summary)
                "fields": "summary,description,status,updated,created,comment,reporter",
            }
            resp = self._request("GET", "/rest/api/2/search", params=params)
            data = resp.json()
            issues = data.get("issues", [])
            if not issues:
                break
            for issue in issues:
                yield issue
            start_at += len(issues)
            if start_at >= data.get("total", 0):
                break
            polite_sleep(base=0.4)  # polite pacing for issue pages

    def list_comments(self, issue_key: str) -> Iterator[dict]:
        # JIRA comments pagination (no explicit maxResults; rely on server default)
        start_at = 0
        while True:
            resp = self._request(
                "GET",
                f"/rest/api/2/issue/{issue_key}/comment",
                params={"startAt": start_at},
            )
            data = resp.json()
            comments = data.get("comments", [])
            if not comments:
                break
            for c in comments:
                yield c
            start_at += len(comments)
            if start_at >= data.get("total", 0):
                break
            polite_sleep(base=0.4, factor=0.6)  # slightly quicker for comments

    def get_issue_total_count(self, since_iso: Optional[str] = None) -> Optional[int]:
        # Use search with maxResults=0 to obtain a total count quickly.
        jql = f"project={self.project}"
        if since_iso:
            reformatted = self._format_updated_since(since_iso)
            jql += f" AND updated >= '{reformatted}'"
        resp = self._request(
            "GET",
            "/rest/api/2/search",
            params={"jql": jql, "maxResults": 0, "fields": "id"},
        )
        return resp.json().get("total")

    def incremental_sync(self, db: RepoDatabase, limit: Optional[int] = None):
        last_issue_sync = db.get_last_issue_sync()
        issue_iter = (
            self.list_issues(since_iso=last_issue_sync)
            if last_issue_sync
            else self.list_issues()
        )
        total_estimate = self.get_issue_total_count(last_issue_sync)
        with create_progress() as progress:
            display_total = None
            if total_estimate and limit:
                display_total = min(total_estimate, limit)
            elif total_estimate:
                display_total = total_estimate
            elif limit:
                display_total = limit
            task_id = progress.add_task("Sync JIRA Issues", total=display_total)
            processed = 0
            max_updated: Optional[str] = last_issue_sync
            for issue_json in issue_iter:
                # Map JIRA issue to GitHub-like shape expected by db.upsert_issue
                gh_like = self._map_issue(issue_json)
                issue_id, inserted, changed = db.upsert_issue(gh_like)
                processed += 1
                progress.update(task_id, advance=1)
                if limit and processed >= limit:
                    break
                gh_updated = gh_like.get("updated_at")
                if gh_updated and (max_updated is None or gh_updated > max_updated):
                    max_updated = gh_updated
                if inserted or changed:
                    for comment_json in self.list_comments(issue_json["key"]):
                        gh_comment = self._map_comment(issue_json["key"], comment_json)
                        db.upsert_comment(issue_id, gh_comment)
            if max_updated:
                db.update_last_issue_sync(max_updated)
            return {"processed_issues": processed, "last_updated": max_updated}

    def _map_issue(self, jira_issue: dict) -> dict:
        key = jira_issue.get("key")  # JIRA key like PROJECT-123
        fields = jira_issue.get("fields", {})
        updated_raw = fields.get("updated")
        created_raw = fields.get("created")
        status = (fields.get("status") or {}).get("name")
        summary = fields.get("summary")
        description = fields.get("description") or ""
        reporter = (fields.get("reporter") or {}).get("displayName")
        comment_meta = fields.get("comment", {})
        comments_total = (
            comment_meta.get("total") if isinstance(comment_meta, dict) else None
        )
        # Convert to GitHub-like schema
        # We need a numeric 'number' for the DB uniqueness; extract trailing digits if present; else hash fallback.
        number_part = 0
        if key and "-" in key:
            tail = key.split("-")[-1]
            if tail.isdigit():
                number_part = int(tail)
        github_like = {
            "number": number_part,
            "title": summary,
            "body": description,
            "state": status.lower() if status else "unknown",
            "user": reporter or "",
            "created_at": parse_jira_datetime(created_raw) if created_raw else None,
            "updated_at": parse_jira_datetime(updated_raw) if updated_raw else None,
            "closed_at": None,  # deriving closed time requires changelog expansion; skipped
            "comments": comments_total or 0,
            "key": key,
            "_provider": "jira",
            "raw_fields": fields,
        }
        return github_like

    def _map_comment(self, issue_key: str, jira_comment: dict) -> dict:
        cid = jira_comment.get("id")
        author = (jira_comment.get("author") or {}).get("displayName")
        body = jira_comment.get("body") or ""
        created = jira_comment.get("created")
        updated = jira_comment.get("updated")
        return {
            "id": (
                int(cid) if cid and str(cid).isdigit() else hash(f"{issue_key}:{cid}")
            ),
            "body": body,
            "user": author or "",
            "created_at": parse_jira_datetime(created) if created else None,
            "updated_at": parse_jira_datetime(updated) if updated else None,
            "_provider": "jira",
            "raw": jira_comment,
        }

    # Helper methods
    def _format_updated_since(self, since_iso: str) -> str:
        """Convert stored ISO timestamp (possibly with timezone) to JIRA-accepted JQL format.

        Preference order:
        1. Full datetime minutes precision: YYYY-MM-DD HH:mm
        2. Date only: YYYY-MM-DD
        3. Original input as last resort (may 400 but we attempted normalization)
        """
        try:
            cleaned = since_iso.replace("Z", "")
            dt = datetime.fromisoformat(cleaned)
            return dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError) as e:
            # If datetime parsing fails, try fallback strategies
            print(f"Warning: Failed to parse JIRA datetime '{since_iso}': {e}")
        # Fallbacks
        date_part = None
        if "T" in since_iso:
            date_part = since_iso.split("T")[0]
        else:
            candidate = since_iso[:10]
            if len(candidate) == 10:
                date_part = candidate
        if date_part and len(date_part) == 10:
            return date_part
        return since_iso


__all__ = ["JiraIssueScraper"]
