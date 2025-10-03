import argparse
import json
from typing import Optional


from issueclear.db import RepoDatabase
from issueclear.scrape.github import GitHubIssueScraper


def cmd_sync(args):
    db = RepoDatabase(args.platform, args.owner, args.repo)
    scraper = GitHubIssueScraper(args.owner, args.repo)
    result = scraper.incremental_sync(db)

    print(json.dumps(result, default=str))


def cmd_show(args):
    db = RepoDatabase(args.platform, args.owner, args.repo)
    if args.number is None:
        rows = db.list_issues()
        print(json.dumps(rows, indent=2))
        return
    issue = db.get_issue(args.number)
    if not issue:
        print(f"Issue #{args.number} not found")
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

    sp_sync = sub.add_parser("sync", help="Sync issues/comments incrementally")
    add_common(sp_sync)
    sp_sync.set_defaults(func=cmd_sync)

    sp_show = sub.add_parser("show", help="Show raw issue JSON")
    add_common(sp_show)
    sp_show.add_argument("--number", type=int, required=False, help="Issue number; omit to list all")
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
