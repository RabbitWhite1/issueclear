import argparse
import json
from typing import Optional


from issueclear.db import RepoDatabase
from issueclear.scrape.github import GitHubIssueScraper
from issueclear.scrape.jira import JiraIssueScraper


def cmd_sync(args):
    db = RepoDatabase(args.platform, args.owner, args.repo)
    if args.platform == "github":
        scraper = GitHubIssueScraper(args.owner, args.repo)
    elif args.platform == "jira":
        if not args.jira_base_url:
            raise SystemExit("--jira-base-url is required when platform is jira")
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


def build_parser():
    p = argparse.ArgumentParser(prog="ic", description="IssueClear CLI")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--platform", default="github")
        sp.add_argument("--owner", required=True)
        sp.add_argument("--repo", required=True)
    # JIRA specific base URL (required when --platform jira; no environment fallback)
        sp.add_argument(
            "--jira-base-url",
            dest="jira_base_url",
            required=False,
            help="Base URL for JIRA (required when --platform jira; e.g. https://jira.example.com)",
        )

    sp_sync = sub.add_parser("sync", help="Sync issues/comments incrementally")
    add_common(sp_sync)
    sp_sync.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of issues/PRs to process this run (useful for huge repos)",
    )
    sp_sync.set_defaults(func=cmd_sync)

    sp_show = sub.add_parser("show", help="Show raw issue JSON")
    add_common(sp_show)
    # Prefer --issue_id; keep --number as a hidden alias for backward compatibility
    sp_show.add_argument(
        "--issue_id",
        "--number",
        dest="issue_id",
        type=int,
        required=False,
        help="Issue id/number; omit to list all",
    )
    sp_show.set_defaults(func=cmd_show)

    sp_stats = sub.add_parser("stats", help="Show counts")
    add_common(sp_stats)
    sp_stats.set_defaults(func=cmd_stats)

    return p


def main(argv: Optional[list] = None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":  # pragma: no cover
    main()
