"""Public API helpers for constructing typed Issue and Comment objects.

This module provides convenience functions for consumers that want fully
materialized issues along with their comments using the dataclasses defined
in `issueclear.issue`.
"""
from __future__ import annotations

from typing import List

from issueclear.db import RepoDatabase
from issueclear.issue import Issue, Comment


def get_issues_with_comments(db: RepoDatabase) -> List[Issue]:
    """Return all issues (including PRs if stored) with their comments.

    For each issue we populate:
      - issue_id: numeric issue number (int) from the provider
      - title/body/state/user: basic metadata
      - comments: list[Comment] ordered by comment creation time
      - timestamp: updated_at if present else created_at

    Each Comment:
      - id: comment unique id from provider
      - issue_id: parent issue number (int)
      - body/user: metadata
      - timestamp: updated_at if present else created_at
    """
    issues: List[Issue] = []
    # Build issue mapping first
    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT issue_key, number, title, body, state, user_login, created_at, updated_at FROM issues ORDER BY number ASC"
        )
        rows = cur.fetchall()
        # Prepare placeholder for comments accumulation
        issue_map = {}
        for (
            issue_key,
            number,
            title,
            body,
            state,
            user_login,
            created_at,
            updated_at,
        ) in rows:
            issue_id = int(number) if number is not None else None  # fallback
            timestamp = updated_at or created_at or ""
            issue_obj = Issue(
                issue_id=issue_id,
                title=title or "",
                body=body or "",
                state=state or "",
                user=user_login or "",
                comments=[],
                timestamp=timestamp,
            )
            issue_map[issue_key] = issue_obj
        # Fetch all comments and attach
        ccur = conn.execute(
            "SELECT comment_id, issue_key, body, user_login, created_at, updated_at FROM comments ORDER BY created_at ASC"
        )
        for (comment_id, issue_key, body, user_login, created_at, updated_at) in ccur.fetchall():
            parent_issue = issue_map.get(issue_key)
            if not parent_issue:
                continue  # orphan (should not happen)
            comment = Comment(
                id=comment_id,
                issue_id=parent_issue.issue_id,
                body=body or "",
                user=user_login or "",
                timestamp=updated_at or created_at or "",
            )
            parent_issue.comments.append(comment)
    # Preserve ordering by number
    issues = list(issue_map.values())
    return issues

__all__ = ["get_issues_with_comments"]
