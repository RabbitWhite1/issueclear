from typing import Iterator, Optional


class IssueScraper:
    """Minimal base for provider-specific issue scrapers.

    Subclasses must implement the methods below. This class intentionally avoids
    prescribing schema/logic beyond required method signatures.
    """

    def __init__(self):
        pass

    # Required interface -------------------------------------------------
    def list_issues(self, since_iso: Optional[str] = None) -> Iterator[dict]:
        """Yield raw issue/pr dictionaries (provider-specific shape)."""
        raise NotImplementedError

    def list_comments(
        self, issue_identifier, since_iso: Optional[str] = None
    ) -> Iterator[dict]:
        """Yield raw comment dictionaries for a given issue identifier/key/number."""
        raise NotImplementedError

    def get_issue_total_count(self, since_iso: Optional[str] = None) -> Optional[int]:
        """Return total count (issues + PRs if applicable) or None if unknown."""
        raise NotImplementedError

    def incremental_sync(self, db, limit: Optional[int] = None):
        """Perform incremental sync into the provided RepoDatabase instance."""
        raise NotImplementedError
