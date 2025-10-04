# IssueClear

This is essentially a tool for issue scraping and LLM-based analysis.

Support incrementally syncing issues and comments from multiple providers into per-repo SQLite databases. Currently supports:

* GitHub (issues + PRs)
* JIRA (tested with jira.mongodb.org)

Then help you filter out issues you may be interested using LLM.

## Design

### Issue Data Storage

Each provider stores issues in `data/<platform>/<owner>/<repo>.sqlite` with tables:

* `issues(issue_id TEXT PK, number INTEGER UNIQUE, ... , metadata TEXT)`
* `comments(id INTEGER PK, comment_id INTEGER UNIQUE, issue_id, metadata TEXT)`
* `sync_state(last_issue_sync TEXT)`

`issue_id` is a string primary key (for GitHub it's the issue/PR number string). Full raw provider JSON is stored in `metadata` (issue) and `metadata` (comment) so future fields are preserved.

### Incremental Sync Strategy

* Uses provider updated timestamp to request only changed/new issues since the last successful sync.
* For any issue that is new or changed, all its comments are refreshed (simple + reliable; avoids per-comment delta logic for now).
* You can cap work per run with `--limit N` to avoid long initial syncs or stressing large providers (partial sync; resume later).

----------

## Setup

```shell
conda create -n ic python=3.11
conda activate ic
pip install -r requirements.txt
```

----------

## Usage

### Scraping CLI Usage

#### General Args

- `--limit`: you can use this to limit number of issues you scrape. This is a polite behavior in the Internet.

#### Github

Sync GitHub repository (issues + PRs):

Before scraping Github issues, you must set environment varialbe `GITHUB_TOKEN`.

```shell
python -m issueclear.ic sync --platform github --owner pygraphviz --repo pygraphviz
```

#### JIRA

Sync JIRA project (must specify base URL):

```shell
python -m issueclear.ic sync --platform jira --owner ZOOKEEPER --repo ZOOKEEPER --jira-base-url https://issues.apache.org/jira
```


Notes:
* For JIRA, `--owner` is treated as the project key. `--repo` is still required (choose the same value if you don't need extra namespacing).
* JIRA issue keys like `SERVER-1234` are mapped to a numeric `number` by extracting the trailing digits; the full key is retained inside `metadata` under `key`.
* Closed timestamp for JIRA is not currently derived; `closed_at` remains null.


### Inspecting Database

```python
from issueclear.db import RepoDatabase

db = RepoDatabase("github", "pygraphviz", "pygraphviz")
issues = db.get_issues_with_comments()
for issue in issues:
	print(issue.issue_id, issue.title, len(issue.comments))
```

### LLM Query

#### Local Model (vllm example)

```shell
# Run vLLM server
python -m vllm.entrypoints.openai.api_server --model Qwen/Qwen2-7B-Instruct --port 8080 --quantization bitsandbytes --dtype auto

# curl -s http://localhost:8080/v1/models  # Use this to check endpoint

# Run query (in another terminal)
python query.py --model hosted_vllm/Qwen/Qwen2-7B-Instruct --api-base http://localhost:8080/v1 --query "memory leak in layout"
```

#### Online API

```shell
export OPENAI_API_KEY=sk-...
# OR for Anthropic
export ANTHROPIC_API_KEY=...
python query.py --model="gpt-4o-mini" --query  "memory leak in layout"
```


## License
See [LICENSE](./LICENSE).
