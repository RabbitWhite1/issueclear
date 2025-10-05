import random
import time
from typing import Optional

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

__all__ = ["create_progress", "polite_sleep"]


def create_progress(description: str = "Sync") -> Progress:
    """Return a standardized Rich Progress instance.

    Usage:
        with create_progress() as progress:
            task_id = progress.add_task("Sync Issues", total=123)
            progress.update(task_id, advance=1)
    """
    return Progress(
        "{task.description}",
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    )


def polite_sleep(base: float, jitter: float = 0.15, factor: float = 1.0):
    """Sleep for a polite interval with jitter.

    Args:
        base: Base delay in seconds (e.g. 0.4 for JIRA issues, 0.25 for GitHub issues).
        jitter: Fractional jitter range (default 0.15 => +/-15%).
        factor: Additional scaling factor (e.g. shorter sleep for comments).
    """
    effective = base * factor
    span = effective * jitter
    # random.uniform(effective - span, effective + span)
    time.sleep(random.uniform(effective - span, effective + span))
