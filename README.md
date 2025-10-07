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

### Option A: uv (Recommended)

```bash
uv sync
# Then you may either call python from uv
uv run python
uv run ic --help
# Or source the venv
source .venv/bin/activate
```

### Option B: Conda / pip

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
- `--sortby created|updated`: choose the field used for incremental ordering and cursors. Use `created` for the very first full ingestion (ensures you enumerate everything once). After an initial full sync finishes, switch to `updated` to skip unchanged historical issues and only process new or recently modified ones. If your initial run was interrupted, keep using `created` until you are confident the historical backlog is complete.

#### Github

Sync GitHub repository (issues + PRs):

Before scraping Github issues, you must set environment varialbe `GITHUB_TOKEN`.

```shell
ic sync --platform github --owner pygraphviz --repo pygraphviz
ic sync --platform github --owner cockroachdb --repo cockroach
ic sync --platform github --owner etcd-io --repo etcd
ic sync --platform github --owner etcd-io --repo raft
ic sync --platform github --owner RedisLabs --repo redisraft
```

#### JIRA

First, inspect available projects on the JIRA server:

```shell
ic jira_inspect --jira_base_url https://jira.mongodb.org
ic jira_inspect --jira_base_url https://issues.apache.org/jira
```

Then sync JIRA project:

```shell
# MongoDB JIRA examples
ic sync --platform jira --owner mongodb --repo SERVER --jira_base_url https://jira.mongodb.org

# Apache JIRA examples  
ic sync --platform jira --owner apache --repo ZOOKEEPER --jira_base_url https://issues.apache.org/jira
```

Notes:
* For JIRA: `--owner` is an organization/company marker (e.g., `mongodb`, `apache`), `--repo` is the JIRA project key (e.g., `SERVER`, `PYTHON`, `ZOOKEEPER`)
* This creates organized storage: `data/jira/mongodb/SERVER.sqlite`, `data/jira/apache/ZOOKEEPER.sqlite`
* Use `ic jira_inspect` to discover available projects and their issue counts
* JIRA issue keys like `SERVER-1234` are mapped to a numeric `number` by extracting the trailing digits; the full key is retained inside `metadata` under `key`
* Closed timestamp for JIRA is not currently derived; `closed_at` remains null


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

# Run query (in another terminal). After install, script is available; or use uv run.
ic query --model hosted_vllm/Qwen/Qwen2-7B-Instruct --api_base http://localhost:8080/v1 --owner pygraphviz --repo pygraphviz --query "memory leak in layout"
```

#### Online API

```shell
export OPENAI_API_KEY=sk-...
# OR for Anthropic
export ANTHROPIC_API_KEY=...
ic query --model gpt-4o-mini --owner pygraphviz --repo pygraphviz --query "memory leak in layout"
```


## License
See [LICENSE](./LICENSE).
