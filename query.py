"""Simple litellm-based querying example.

Use this script to run relevance querying against a locally hosted or remote
LLM endpoint supported by litellm (OpenAI, Anthropic, local inference server,
OpenAI-compatible proxy, etc.). You no longer need to manually load a
`transformers` model here; just point `--model` (and optionally `--api-base`)
to your running endpoint.

Examples:

  # Remote (OpenAI-compatible) service
  python test_query.py --model gpt-4o-mini --query "memory leak in layout"

  # Local text-generation-inference / vLLM / custom OpenAI-compatible server
  python test_query.py \
      --model huggingface/meta-llama/Llama-2-7b-chat-hf \
      --api-base http://localhost:8080/v1 \
      --api-key NA \
      --query "crash when rendering large graphs"

  # Ollama (if litellm is configured to route 'ollama/llama3')
  python test_query.py --model ollama/llama3 --query "segfault during dot parsing"

Notes:
  * --api-key is required syntactically by litellm even for some local servers; use NA if not needed.
  * --no-comments restricts context to issue title/body only (faster, less tokens).
  * Adjust --max-issues to limit processing during experimentation.
"""

import argparse
from issueclear.db import RepoDatabase
from issueclear.llm_query import IssueRelevanceQuerier


def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--platform", default="github")
    p.add_argument("--owner", default="pygraphviz")
    p.add_argument("--repo", default="pygraphviz")
    p.add_argument("--model", required=True, help="litellm model name or provider/model path")
    p.add_argument("--query", required=True)
    p.add_argument("--max-issues", type=int, default=None)
    p.add_argument("--api-base", default=None, help="Override API base (e.g. http://localhost:8080/v1)")
    p.add_argument("--api-key", default=None, help="Explicit API key (use NA for local if required)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.api_base and args.api_base.find("localhost") != -1:
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
    print(f"""Running model={args.model}, api_base={litellm_kwargs.get("api_base")}, max_issues={args.max_issues}""")
    matches = querier.run_on_db(db, query=args.query)
    for m in matches[:10]:
        print(f"#{m['issue_id']} score={m['score']} :: {m['reason']}")


if __name__ == "__main__":  # pragma: no cover
    main()
