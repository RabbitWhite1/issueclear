from dataclasses import dataclass


@dataclass
class Comment:
    id: int
    issue_id: int
    body: str
    user: str
    timestamp: str
    metadata: dict = None


@dataclass
class Issue:
    issue_id: int
    title: str
    body: str
    state: str
    user: str
    comments: list[Comment]
    timestamp: str
    metadata: dict = None
