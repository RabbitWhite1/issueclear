import argparse
import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import rich
from datasets import Dataset, DatasetDict, Features, Value

from issueclear.utils import patch_datasets_tqdm
from issueclear.db import RepoDatabase
from issueclear.llm_query import IssueRelevanceQuerier
from issueclear.scrape.github import GitHubIssueScraper
from issueclear.scrape.jira import JiraIssueScraper

patch_datasets_tqdm()

ROOT_DATA_PATH = "data"
ISSUES_FEATURES = Features(
    {
        "issue_id": Value("string"),
        "number": Value("int64"),
        "title": Value("string"),
        "body": Value("string"),
        "state": Value("string"),
        "user": Value("string"),
        "created_at": Value("string"),
        "updated_at": Value("string"),
        "closed_at": Value("string"),
        "comments_count": Value("int64"),
        "metadata": Value("string"),
    }
)
SYNC_STATE_FEATURES = Features({"id": Value("int64"), "last_issue_sync": Value("string")})


def _find_sqlite_files(root: str) -> list[str]:
    root_path = Path(root)
    return [str(p) for p in root_path.rglob("*.sqlite")]


def cmd_tohf(args):
    """Push all local .sqlite tables as splits to a Hugging Face dataset repo.

    Each table becomes a dataset config; each database file path becomes a split name.
    Example resulting dataset_repo (config 'issues') splits: github__pygraphviz__pygraphviz, jira__XYZ__XYZ, etc.
    """
    db_paths = _find_sqlite_files(ROOT_DATA_PATH)
    if len(db_paths) == 0:
        rich.print("[yellow]No .sqlite files found under 'data/'. Run sync first.[/yellow]")
        return

    rich.print(f"Found {len(db_paths)} dbs", db_paths)

    datasets_map: dict[str, dict[str, "Dataset"]] = {}
    for db_path in db_paths:
        split_name = db_path.removeprefix(ROOT_DATA_PATH).removesuffix(".sqlite").strip("/").replace("/", "__")
        rich.print(f"Loading subset: [bold]{split_name}[/bold]")
        conn = sqlite3.connect(db_path)
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';",
            conn,
        )["name"].tolist()
        for t in tables:
            rich.print(f"  Table: {t}")
            df = pd.read_sql_query(f"SELECT * FROM {t}", conn)
            if t == "issues":
                features = ISSUES_FEATURES
            elif t == "sync_state":
                features = SYNC_STATE_FEATURES
            else:
                features = None
            datasets_map.setdefault(t, dict())[split_name] = Dataset.from_pandas(df, features=features)
        conn.close()

    for table_name, splits in datasets_map.items():
        rich.print(f"Pushing config: [cyan]{table_name}[/cyan] with {len(splits)} split(s)")
        ds = DatasetDict(splits)
        if args.dry_run:
            rich.print(f"[green]Dry run: would push to {args.hf_repo} (config={table_name}, private={args.private})[/green]")
            continue
        ds.push_to_hub(args.hf_repo, config_name=table_name, private=args.private)
    rich.print("[bold green]Done.[/bold green]")


def cmd_sync(args):
    db = RepoDatabase(args.platform, args.owner, args.repo)
    if args.platform == "github":
        scraper = GitHubIssueScraper(args.owner, args.repo)
    elif args.platform == "jira":
        if not args.jira_base_url:
            raise SystemExit("--jira-base-url is required when platform is jira")
        # For JIRA: args.owner=organization marker, args.repo=project
        scraper = JiraIssueScraper(args.owner, args.repo, base_url=args.jira_base_url)
    else:
        raise SystemExit(f"Unsupported platform: {args.platform}")
    result = scraper.incremental_sync(db, limit=args.limit)

    print(json.dumps(result, default=str))


def cmd_show(args):
    db = RepoDatabase(args.platform, args.owner, args.repo)
    issue_id = args.issue_id  # unified internal name
    if issue_id is None:
        rows = db.list_issues()
        print(json.dumps(rows, indent=2))
        return
    issue = db.get_issue(issue_id)
    if not issue:
        print(f"Issue #{issue_id} not found")
        return
    print(json.dumps(issue, indent=2))


def cmd_stats(args):
    db = RepoDatabase(args.platform, args.owner, args.repo)
    print(json.dumps(db.stats()))


def cmd_query(args):
    args = build_parser().parse_args()
    if args.api_base and "localhost" in args.api_base and not args.api_key:
        args.api_key = "NA"

    db = RepoDatabase(args.platform, args.owner, args.repo)
    litellm_kwargs = {}
    if args.api_base:
        litellm_kwargs["api_base"] = args.api_base
    if args.api_key:
        litellm_kwargs["api_key"] = args.api_key
    querier = IssueRelevanceQuerier(
        model=args.model,
        max_issues=args.max_issues,
        litellm_kwargs=litellm_kwargs or None,
    )
    print(f"Running model={args.model}, api_base={litellm_kwargs.get('api_base')}, max_issues={args.max_issues}")
    matches = querier.run_on_db(db, query=args.query)
    for m in matches[:10]:
        print(f"#{m['issue_id']} score={m['score']} :: {m['reason']}")


def cmd_jira_inspect(args):
    """Inspect JIRA server: show server info, projects, and help identify correct owner/repo values."""
    import requests
    
    base_url = args.jira_base_url.rstrip("/")
    headers = {
        "Accept": "application/json",
        "User-Agent": "issueclear-jira-inspector",
    }
    
    def jira_request(path: str, **params):
        url = f"{base_url}{path}"
        resp = requests.get(url, headers=headers, timeout=30, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"JIRA API error {resp.status_code} {url}: {resp.text[:500]}")
        return resp.json()
    
    print(f"🔍 Inspecting JIRA server: {base_url}")
    print()
    
    # Get server info
    try:
        server_info = jira_request("/rest/api/2/serverInfo")
        print("📊 Server Information:")
        print(f"  Version: {server_info.get('version', 'Unknown')}")
        print(f"  Build: {server_info.get('buildNumber', 'Unknown')}")
        print(f"  Title: {server_info.get('serverTitle', 'Unknown')}")
        print(f"  URL: {server_info.get('baseUrl', 'Unknown')}")
        print()
    except Exception as e:
        print(f"⚠️  Could not fetch server info: {e}")
        print()
    
    # Get available projects
    try:
        projects = jira_request("/rest/api/2/project")
        print(f"📁 Available Projects ({len(projects)} total):")
        print()
        
        # Sort projects by key for easier browsing
        projects_sorted = sorted(projects, key=lambda p: p.get('key', ''))
        
        for project in projects_sorted:
            key = project.get('key', 'N/A')
            name = project.get('name', 'N/A')
            project_type = project.get('projectTypeKey', 'unknown')
            
            # Try to get issue count for this project
            try:
                search_result = jira_request("/rest/api/2/search", 
                                           jql=f"project={key}", 
                                           maxResults=0)  # Just get total count
                issue_count = search_result.get('total', 0)
                print(f"  {key:<12} | {issue_count:>6} issues | {name} ({project_type})")
            except Exception as e:
                print(f"  {key:<12} | {'?':>6} issues | {name} ({project_type}) [Error: {e}]")
        
        print()
        print("💡 Usage Tips:")
        print("  • Use organization/company name as --owner (e.g., mongodb, apache, etc.)")
        print("  • Use project KEY as --repo (e.g., SERVER, PYTHON, etc.)")
        print("  • This creates organized storage: data/jira/organization/project.sqlite")
        print()
        print("Example commands:")
        if projects_sorted:
            example_key = projects_sorted[0].get('key', 'PROJECT')
            print(f"  uv run ic sync --platform jira --owner mongodb --repo {example_key} --jira_base_url {base_url}")
            print(f"  uv run ic show --platform jira --owner mongodb --repo {example_key}")
        
    except Exception as e:
        print(f"⚠️  Could not fetch projects: {e}")
        print("   This might indicate authentication issues or API restrictions.")


def build_parser():
    p = argparse.ArgumentParser(prog="ic", description="IssueClear CLI")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--platform", default="github")
        sp.add_argument("--owner", required=True)
        sp.add_argument("--repo", required=True)
        # JIRA specific base URL (required when --platform jira; no environment fallback)
        # Use underscore naming for consistency; keep legacy hyphenated alias for backward compatibility.
        sp.add_argument(
            "--jira_base_url",
            "--jira-base-url",
            dest="jira_base_url",
            required=False,
            help="Base URL for JIRA (required when --platform jira; e.g. https://jira.example.com)",
        )

    # Subcommand: sync
    sp_sync = sub.add_parser("sync", help="Sync issues/comments incrementally")
    add_common(sp_sync)
    sp_sync.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of issues/PRs to process this run (useful for huge repos)",
    )
    sp_sync.set_defaults(func=cmd_sync)

    # Subcommand: show
    sp_show = sub.add_parser("show", help="Show raw issue JSON")
    add_common(sp_show)
    sp_show.add_argument(
        "--issue_id",
        dest="issue_id",
        type=int,
        required=False,
        help="Issue id; omit to list all",
    )
    sp_show.set_defaults(func=cmd_show)

    sp_stats = sub.add_parser("stats", help="Show counts")
    add_common(sp_stats)
    sp_stats.set_defaults(func=cmd_stats)

    # Subcommand: tohf
    sp_tohf = sub.add_parser("tohf", help="Push local SQLite data -> Hugging Face dataset repo")
    sp_tohf.add_argument(
        "--hf_repo",
        "--hf-repo",
        required=True,
        help="Target Hub repo like username/issues",
    )
    sp_tohf.add_argument("--private", action="store_true", help="Mark dataset private")
    sp_tohf.add_argument(
        "--dry_run",
        "--dry-run",
        action="store_true",
        help="Load/prepare but do not push",
    )
    sp_tohf.set_defaults(func=cmd_tohf)

    # Subcommand: query
    sp_query = sub.add_parser("query", help="Query issues with LLM relevance filtering")
    add_common(sp_query)
    sp_query.add_argument("--model", required=True, help="litellm model name or provider/model path")
    sp_query.add_argument("--query", required=True)
    sp_query.add_argument("--max_issues", "--max-issues", type=int, default=None)
    sp_query.add_argument(
        "--api_base",
        "--api-base",
        default=None,
        help="Override API base (e.g. http://localhost:8080/v1)",
    )
    sp_query.add_argument(
        "--api_key",
        "--api-key",
        default=None,
        help="Explicit API key (use NA for local if required)",
    )
    sp_query.set_defaults(func=cmd_query)
    
    # Subcommand: jira_inspect
    sp_inspect = sub.add_parser("jira_inspect", help="Inspect JIRA server: show projects and help identify correct owner/repo")
    sp_inspect.add_argument(
        "--jira_base_url",
        "--jira-base-url", 
        dest="jira_base_url",
        required=True,
        help="Base URL for JIRA (e.g. https://jira.mongodb.org)"
    )
    sp_inspect.set_defaults(func=cmd_jira_inspect)
    
    return p


def main(argv: Optional[list] = None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
