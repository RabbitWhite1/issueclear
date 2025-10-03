from issueclear.issue import Issue


class IssueScraper:
    def __init__(self): ...

    def fetch_issue(self, issue_id) -> Issue:
        raise NotImplementedError("Subclasses must implement fetch_issue")
