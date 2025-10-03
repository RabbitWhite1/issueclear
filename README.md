IssueClear
==========

Lightweight CLI + library for incrementally syncing issues and comments from multiple providers into per-repo SQLite databases. Currently supports:

* GitHub (issues + PRs)
* JIRA (tested with jira.mongodb.org)

Data Model
----------
Each provider stores issues in `data/<platform>/<owner>/<repo>.sqlite` with tables:

* `issues(issue_id TEXT PK, number INTEGER UNIQUE, ... , metadata TEXT)`
* `comments(id INTEGER PK, comment_id INTEGER UNIQUE, issue_id, metadata TEXT)`
* `sync_state(last_issue_sync TEXT)`

`issue_id` is a string primary key (for GitHub it's the issue/PR number string). Full raw provider JSON is stored in `metadata` (issue) and `metadata` (comment) so future fields are preserved.

User Field (Breaking Change)
----------------------------
As of current version, the `user` field in stored `metadata` for issues and comments is a plain string (login / display name). Earlier drafts used a nested object with a `login` key; that structure is no longer produced.

Incremental Sync Strategy
-------------------------
* Uses provider updated timestamp to request only changed/new issues since the last successful sync.
* For any issue that is new or changed, all its comments are refreshed (simple + reliable; avoids per-comment delta logic for now).
* A Rich progress bar is displayed. If a total count estimate is available (GitHub: sum issues+PRs; JIRA: search total) it is shown; otherwise the bar is indeterminate.
* You can cap work per run with `--limit N` to avoid long initial syncs or stressing large providers (partial sync; resume later).

Environment Variables
---------------------
GitHub:
* `GITHUB_TOKEN` (required) – classic or fine‑grained token with `repo` (public) access to read issues & PRs.

JIRA:
* Provide base URL via the `--jira-base-url` flag (all requests are anonymous; e.g. `--jira-base-url https://jira.example.com`).

CLI Usage
---------
Sync GitHub repository (issues + PRs):

```
python -m issueclear.ic sync --platform github --owner pygraphviz --repo pygraphviz
```

Show a specific issue raw metadata:

```
python -m issueclear.ic show --platform github --owner pygraphviz --repo pygraphviz --issue_id 1
```

Sync JIRA project (must specify base URL):

```
python -m issueclear.ic sync --platform jira --owner SERVER --repo SERVER --jira-base-url https://jira.example.com
```

Partial sync (limit to first 500 changed/new issues this run):

```
python -m issueclear.ic sync --platform github --owner someorg --repo bigrepo --limit 500
```

Notes:
* For JIRA, `--owner` is treated as the project key. `--repo` is still required (choose the same value if you don't need extra namespacing).
* JIRA issue keys like `SERVER-1234` are mapped to a numeric `number` by extracting the trailing digits; the full key is retained inside `metadata` under `key`.
* JIRA "state" is the lowercased status name.
* Closed timestamp for JIRA is not currently derived; `closed_at` remains null.

Programmatic Access
-------------------
```python
from issueclear.db import RepoDatabase

db = RepoDatabase("github", "pygraphviz", "pygraphviz")
issues = db.get_issues_with_comments()
for issue in issues:
	print(issue.issue_id, issue.title, len(issue.comments))
```

License
-------
See `LICENSE`.
