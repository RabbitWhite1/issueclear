import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional, Tuple

from issueclear.issue import Comment, Issue

DATA_ROOT = Path(os.environ.get("ISSUES_DATA_DIR", "data"))


def repo_dir(platform: str, owner: str, repo: str) -> Path:
    """Return nested directory path data/platform/owner/repo."""
    return DATA_ROOT / platform / owner / repo


def repo_db_path(platform: str, owner: str, repo: str) -> Path:
    """Return path data/platform/owner/{repo}.sqlite (no additional repo directory)."""
    return DATA_ROOT / platform / owner / f"{repo}.sqlite"


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

/* issue_id (TEXT) is the primary key (human-facing issue identifier as string).
   For GitHub we use str(issue_json['number']). Provider numeric id remains inside metadata JSON. */
CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    number INTEGER NOT NULL UNIQUE,
    title TEXT,
    body TEXT,
    state TEXT,
    user TEXT,
    created_at TEXT,
    updated_at TEXT,
    closed_at TEXT,
    comments_count INTEGER,
    metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    comment_id INTEGER NOT NULL UNIQUE,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id) ON DELETE CASCADE,
    user TEXT,
    body TEXT,
    created_at TEXT,
    updated_at TEXT,
    metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_issue_sync TEXT
);
"""


def _normalize_user(user_val):
    """Return a plain string user login/name given various provider forms.

    Accepts:
      - string (returned as-is)
      - dict with 'login' or 'name' or 'displayName'
      - None / other -> ""
    """
    if isinstance(user_val, str):
        return user_val
    if isinstance(user_val, dict):
        return (
            user_val.get("login")
            or user_val.get("name")
            or user_val.get("displayName")
            or ""
        )
    return ""


class RepoDatabase:
    def __init__(self, platform: str, owner: str, repo: str):
        self.platform = platform
        self.owner = owner
        self.repo = repo
        self.path = repo_db_path(platform, owner, repo)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.get_conn() as conn:
            self._init_schema(conn)

    @contextmanager
    def get_conn(self):
        conn = sqlite3.connect(self.path)
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self, conn: sqlite3.Connection):
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        # Basic schema validation: ensure new primary key column exists; if not, raise.
        cur = conn.execute("PRAGMA table_info(issues)")
        cols = {r[1] for r in cur.fetchall()}
        if "issue_id" not in cols:
            raise RuntimeError(
                "Existing database uses legacy schema (missing issue_id). Delete the old database file to recreate."
            )

    def get_last_issue_sync(self) -> Optional[str]:
        with self.get_conn() as conn:
            cur = conn.execute("SELECT last_issue_sync FROM sync_state WHERE id=1")
            row = cur.fetchone()
            return row[0] if row else None

    def update_last_issue_sync(self, ts: str):
        with self.get_conn() as conn:
            conn.execute(
                "INSERT INTO sync_state(id, last_issue_sync) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET last_issue_sync=excluded.last_issue_sync",
                (ts,),
            )
            conn.commit()

    def upsert_issue(self, issue_json: dict) -> Tuple[str, bool, bool]:
        """Insert or update an issue.

        Returns (issue_key, inserted, changed_content).
        issue_key = str(issue number) so users can reference issues uniformly across providers.
        Provider specific numeric id remains inside raw_json should we need a stable opaque ID later.
        """
        number = issue_json.get("number")
        if number is None:
            raise ValueError("Issue JSON missing 'number'")
        issue_id = str(number)
        updated_at = issue_json.get("updated_at")
        title = issue_json.get("title")
        body = issue_json.get("body")
        state = issue_json.get("state")
        user_login = _normalize_user(issue_json.get("user"))
        created_at = issue_json.get("created_at")
        closed_at = issue_json.get("closed_at")
        comments_count = issue_json.get("comments")
        raw_str = json.dumps(issue_json)
        with self.get_conn() as conn:
            cur = conn.execute(
                "SELECT issue_id, updated_at, title, body, state FROM issues WHERE issue_id=?",
                (issue_id,),
            )
            row = cur.fetchone()
            if row:
                _, existing_updated_at, ex_title, ex_body, ex_state = row
                inserted = False
                changed_content = (
                    (title != ex_title)
                    or (body != ex_body)
                    or (state != ex_state)
                    or (updated_at != existing_updated_at)
                )
                if changed_content:
                    conn.execute(
                        "UPDATE issues SET number=?, title=?, body=?, state=?, user=?, created_at=?, updated_at=?, closed_at=?, comments_count=?, metadata=? WHERE issue_id=?",
                        (
                            number,
                            title,
                            body,
                            state,
                            user_login,
                            created_at,
                            updated_at,
                            closed_at,
                            comments_count,
                            raw_str,
                            issue_id,
                        ),
                    )
                    conn.commit()
                return issue_id, inserted, changed_content
            else:
                conn.execute(
                    "INSERT INTO issues(issue_id, number, title, body, state, user, created_at, updated_at, closed_at, comments_count, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        issue_id,
                        number,
                        title,
                        body,
                        state,
                        user_login,
                        created_at,
                        updated_at,
                        closed_at,
                        comments_count,
                        raw_str,
                    ),
                )
                conn.commit()
                return issue_id, True, True

    def get_issue(self, number: int):
        with self.get_conn() as conn:
            cur = conn.execute("SELECT metadata FROM issues WHERE number=?", (number,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None

    def upsert_comment(
        self, issue_id: str, comment_json: dict
    ) -> Tuple[int, bool, bool]:
        comment_id = comment_json.get("id")
        if comment_id is None:
            raise ValueError("Comment JSON missing 'id'")
        body = comment_json.get("body")
        user_login = _normalize_user(comment_json.get("user"))
        created_at = comment_json.get("created_at")
        updated_at = comment_json.get("updated_at")
        raw_str = json.dumps(comment_json)
        with self.get_conn() as conn:
            cur = conn.execute(
                "SELECT id, body, updated_at FROM comments WHERE comment_id=?",
                (comment_id,),
            )
            row = cur.fetchone()
            if row:
                cid, ex_body, ex_updated_at = row
                inserted = False
                changed = (body != ex_body) or (updated_at != ex_updated_at)
                if changed:
                    conn.execute(
                        "UPDATE comments SET issue_id=?, body=?, user=?, created_at=?, updated_at=?, metadata=? WHERE id=?",
                        (
                            issue_id,
                            body,
                            user_login,
                            created_at,
                            updated_at,
                            raw_str,
                            cid,
                        ),
                    )
                    conn.commit()
                return cid, inserted, changed
            else:
                cur = conn.execute(
                    "INSERT INTO comments(comment_id, issue_id, body, user, created_at, updated_at, metadata) VALUES (?,?,?,?,?,?,?)",
                    (
                        comment_id,
                        issue_id,
                        body,
                        user_login,
                        created_at,
                        updated_at,
                        raw_str,
                    ),
                )
                cid = cur.lastrowid
                conn.commit()
                return cid, True, True

    def list_issue_numbers(self) -> List[int]:
        with self.get_conn() as conn:
            cur = conn.execute("SELECT number FROM issues ORDER BY number ASC")
            return [r[0] for r in cur.fetchall()]

    def list_issues(self) -> List[dict]:
        """Return minimal metadata for all issues (ordered)."""
        with self.get_conn() as conn:
            cur = conn.execute(
                "SELECT number, title, state, updated_at, comments_count FROM issues ORDER BY number ASC"
            )
            return [
                {
                    "number": n,
                    "title": t,
                    "state": s,
                    "updated_at": u,
                    "comments_count": c,
                }
                for (n, t, s, u, c) in cur.fetchall()
            ]

    def stats(self):
        with self.get_conn() as conn:
            issue_count = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
            comment_count = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
            return {"issues": issue_count, "comments": comment_count}

    def get_issues_with_comments(self) -> List[Issue]:
        """Return all issues (including PRs if stored) with attached comments as dataclasses.

        Issue.issue_id: numeric issue number
        Issue.timestamp: updated_at or created_at
        Comment.timestamp: updated_at or created_at
        Comments ordered by created_at; issues ordered by number.
        metadata: full raw JSON for both Issue and Comment objects.
        """
        with self.get_conn() as conn:
            cur = conn.execute(
                "SELECT issue_id, number, title, body, state, user, created_at, updated_at, metadata, closed_at, comments_count FROM issues ORDER BY number ASC"
            )
            issue_rows = cur.fetchall()
            issue_map: dict[str, Issue] = {}
            for (
                issue_id,
                number,
                title,
                body,
                state,
                user_login,
                created_at,
                updated_at,
                raw_json,
                closed_at,
                comments_count,
            ) in issue_rows:
                timestamp = updated_at or created_at or ""
                try:
                    meta = json.loads(raw_json) if raw_json else {}
                except Exception:
                    meta = {}
                # Ensure some core fields present even if raw_json changes in future
                meta.setdefault("issue_id", issue_id)
                meta.setdefault("number", number)
                meta.setdefault("state", state)
                meta.setdefault("closed_at", closed_at)
                meta.setdefault("comments_count", comments_count)
                issue_obj = Issue(
                    issue_id=int(number) if number is not None else -1,
                    title=title or "",
                    body=body or "",
                    state=state or "",
                    user=user_login or "",
                    comments=[],
                    timestamp=timestamp,
                    metadata=meta,
                )
                issue_map[issue_id] = issue_obj
            ccur = conn.execute(
                "SELECT comment_id, issue_id, body, user, created_at, updated_at, metadata FROM comments ORDER BY created_at ASC"
            )
            for (
                comment_id,
                issue_id,
                body,
                user_login,
                created_at,
                updated_at,
                raw_json,
            ) in ccur.fetchall():
                parent = issue_map.get(issue_id)
                if not parent:
                    continue
                try:
                    c_meta = json.loads(raw_json) if raw_json else {}
                except Exception:
                    c_meta = {}
                c_meta.setdefault("comment_id", comment_id)
                c_meta.setdefault("issue_id", issue_id)
                parent.comments.append(
                    Comment(
                        id=comment_id,
                        issue_id=parent.issue_id,
                        body=body or "",
                        user=user_login or "",
                        timestamp=updated_at or created_at or "",
                        metadata=c_meta,
                    )
                )
        return list(issue_map.values())


__all__ = ["RepoDatabase", "repo_dir", "repo_db_path"]
