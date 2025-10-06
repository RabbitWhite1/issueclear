"""LLM-powered issue filtering using litellm.

This module provides a function `filter_issues_with_llm` that evaluates each
issue (and optionally its comments) against a natural language user query and
returns structured match results.

Supported models: anything accepted by litellm, e.g. "gpt-4o-mini", "gpt-4",
"claude-3-5-sonnet-20240620", local models via OpenAI-compatible endpoints,
ollama (e.g. model name "ollama/llama3" if litellm configured), etc.

Environment variables / configuration:
  * Standard provider keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.) as required
    by litellm.
  * For local endpoints, set LITELLM_ENDPOINT / LITELLM_API_BASE per litellm docs.

Return schema (list of dicts):
  {
    "issue_id": int,
    "title": str,
    "score": float,            # 0..1 relevance score (LLM provided or heuristic)
    "match": bool,             # True if passes threshold
    "reason": str,             # Short justification from the model
    "raw_decision": str        # Raw model JSON/text before parsing (for audit)
  }

We keep throughput modest (sequential) to avoid rate spikes. Future enhancement
could implement batching or parallelization.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict
from typing import Iterable, List, Optional, Sequence, Tuple

import litellm
from tqdm.rich import tqdm

from issueclear.db import RepoDatabase
from issueclear.issue import Issue

SYSTEM_PROMPT = """You are a helpful assistant that classifies software issues for relevance.
Given: (1) a user query describing what kind of issues they want, and (2) an issue
with optional comments, output a concise JSON object with fields:
{
  "match": true|false,   # whether the issue satisfies the user's query
  "score": number,       # 0..1 confidence or relevance
  "reason": "short reason"
}
Guidelines:
- Be strict: only match if the issue clearly aligns.
- If unsure, set match=false and score<=0.5.
- score should reflect strength of match; 1.0 only for perfect, obvious matches.
Respond with ONLY the JSON.
"""

# Minimal JSON extraction regex fallback if the model adds text around output
JSON_REGEX = re.compile(r"\{[\s\S]*\}")


def _render_issue(issue: Issue, include_comments: bool, max_comment_chars: int) -> str:
    parts = [
        f"Title: {issue.title}",
        f"State: {issue.state}",
        f"Body:\n{issue.body[:4000]}",
    ]
    if include_comments and issue.comments:
        acc = []
        total = 0
        for c in issue.comments:
            snippet = c.body.strip().replace("\n", " ")
            if not snippet:
                continue
            if total + len(snippet) > max_comment_chars:
                remaining = max_comment_chars - total
                if remaining > 50:  # add partial if somewhat meaningful
                    acc.append(snippet[:remaining])
                break
            acc.append(snippet)
            total += len(snippet)
        if acc:
            parts.append("Comments: " + " \n".join(acc))
    return "\n".join(parts)


def _safe_parse_json(text: str) -> Optional[dict]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = JSON_REGEX.search(text)
        if m:
            try:
                return json.loads(m.group(0))
            except (json.JSONDecodeError, TypeError):
                return None
    return None


def evaluate_issues_with_llm(
    issues: Sequence[Issue],
    user_query: str,
    model: str = "gpt-4o-mini",
    include_comments: bool = True,
    max_comment_chars: int = 2000,
    max_issues: Optional[int] = None,
    litellm_kwargs: Optional[dict] = None,
) -> List[dict]:
    """Return list of match dicts for issues relevant to `user_query`.

    Args:
        issues: sequence of Issue dataclasses.
        user_query: natural language query.
        model: litellm model name.
        include_comments: whether to pass comments context.
        max_comment_chars: cap aggregated comment text length.
        max_issues: optional cap on number of issues processed.

    Returns list sorted by score descending (only matched ones).
    """
    litellm_kwargs = litellm_kwargs or {}
    results: List[dict] = []
    processed = 0
    for issue in tqdm(issues):
        if max_issues and processed >= max_issues:
            break
        processed += 1
        rendered = _render_issue(
            issue,
            include_comments=include_comments,
            max_comment_chars=max_comment_chars,
        )
        user_prompt = (
            f"User Query:\n{user_query}\n---\nIssue #: {issue.issue_id}\n" + rendered
        )
        try:
            resp = litellm.completion(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=256,
                **litellm_kwargs,
            )
            # Two accepted shapes:
            # 1) OpenAI style: resp.choices[0].message["content"]
            # 2) Custom dict with same
            raw_text = ""
            if resp is not None:
                try:
                    raw_text = resp.choices[0].message["content"]  # type: ignore[attr-defined]
                except (AttributeError, KeyError, IndexError):
                    try:
                        raw_text = resp["choices"][0]["message"]["content"]  # type: ignore[index]
                    except (KeyError, IndexError, TypeError):
                        # Maybe direct text
                        raw_text = getattr(resp, "text", "") or resp.get("text", "")  # type: ignore
        except Exception as e:
            print(f"Failed calling LLM: {e}")
            raw_text = f'{{"match": false, "score": 0.0, "reason": "error: {e}"}}'

        parsed = _safe_parse_json(raw_text) or {}
        matched = bool(parsed.get("match"))
        score = parsed.get("score")
        try:
            score = float(score)
        except (ValueError, TypeError):
            score = 0.0
        reason = str(parsed.get("reason") or "")[:500]

        results.append(
            {
                "issue_id": issue.issue_id,
                "title": issue.title,
                "match": matched,
                "score": round(score, 4),
                "reason": reason,
                "raw_decision": raw_text.strip(),
            }
        )

    # Keep only matches, sort by score
    results.sort(key=lambda r: (r["match"], r["score"]), reverse=True)
    return results


class IssueRelevanceQuerier:
    """Reusable querier object wrapping LLM-based filtering.

    Configure once with model + defaults; call .run() with a user query and a
    list of Issue objects, or .run_on_db() providing a RepoDatabase.
    """

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        include_comments: bool = True,
        max_comment_chars: int = 2000,
        max_issues: Optional[int] = None,
        completion_fn=None,
        litellm_kwargs: Optional[dict] = None,
    ):
        self.model = model
        self.include_comments = include_comments
        self.max_comment_chars = max_comment_chars
        self.max_issues = max_issues
        self.completion_fn = completion_fn
        self.litellm_kwargs = litellm_kwargs or {}

    def run(self, issues: Sequence[Issue], query: str) -> List[dict]:
        return evaluate_issues_with_llm(
            issues,
            user_query=query,
            model=self.model,
            include_comments=self.include_comments,
            max_comment_chars=self.max_comment_chars,
            max_issues=self.max_issues,
            litellm_kwargs=self.litellm_kwargs,
        )

    def run_on_db(self, db: RepoDatabase, query: str) -> List[dict]:
        issues = db.get_issues_with_comments()
        if self.max_issues:
            issues = issues[: self.max_issues]
        return self.run(issues, query)


__all__ = ["filter_issues_with_llm", "IssueRelevanceQuerier"]
