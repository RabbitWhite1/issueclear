import asyncio
import json
import os
from typing import Any, Optional

import datasets
import datasets.data_files
import datasets.download
import datasets.io.csv
import datasets.io.json
import datasets.io.parquet
import datasets.io.sql
import datasets.search
import psutil
from dotenv import load_dotenv
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
    filesize,
)
from rich.text import Text
from tqdm.rich import tqdm as rich_tqdm

RST = "\033[0m"
BRED = "\033[1;31m"
BGREEN = "\033[1;32m"
BYELLOW = "\033[1;33m"
BRI = "\033[1m"


def size_str(size_in_bytes: int) -> str:
    if size_in_bytes < 1024:
        return f"{size_in_bytes} B"
    elif size_in_bytes < (1024**2):
        return f"{size_in_bytes / 1024:.2f} KB"
    elif size_in_bytes < (1024**3):
        return f"{size_in_bytes / (1024 ** 2):.2f} MB"
    else:
        return f"{size_in_bytes / (1024 ** 3):.2f} GB"


def memory_usage() -> dict:
    """Get current memory usage"""
    process = psutil.Process(os.getpid())
    mem_info = process.memory_info()
    return {
        "rss": size_str(mem_info.rss),
        "vms": size_str(mem_info.vms),
        "shared": size_str(mem_info.shared),
    }


class my_tqdm_for_hf(rich_tqdm):
    """
    Class to override `disable` argument in case progress bars are globally disabled.

    Taken from https://github.com/tqdm/tqdm/issues/619#issuecomment-619639324.
    """

    def __init__(self, *args, **kwargs):
        if datasets.utils.are_progress_bars_disabled():
            kwargs["disable"] = True
        super().__init__(*args, **kwargs)

    def __delattr__(self, attr: str) -> None:
        """Fix for https://github.com/huggingface/datasets/issues/6066"""
        try:
            super().__delattr__(attr)
        except AttributeError:
            if attr != "_lock":
                raise


def patch_datasets_tqdm():
    datasets.arrow_dataset.hf_tqdm = my_tqdm_for_hf
    datasets.arrow_reader.hf_tqdm = my_tqdm_for_hf
    datasets.builder.hf_tqdm = my_tqdm_for_hf
    datasets.data_files.hf_tqdm = my_tqdm_for_hf
    datasets.download.download_manager.tqdm = my_tqdm_for_hf
    datasets.io.csv.hf_tqdm = my_tqdm_for_hf
    datasets.io.json.hf_tqdm = my_tqdm_for_hf
    datasets.io.parquet.hf_tqdm = my_tqdm_for_hf
    datasets.io.sql.hf_tqdm = my_tqdm_for_hf
    datasets.search.hf_tqdm = my_tqdm_for_hf

    try:
        import huggingface_hub.utils._xet_progress_reporting

        huggingface_hub.utils._xet_progress_reporting.tqdm = rich_tqdm
    except ModuleNotFoundError as e:
        # NOTE: Given `_xet_progress_reporting` is private, it may not exist in future versions.
        pass


class RateColumn(ProgressColumn):
    """Renders human readable transfer speed."""

    def __init__(self, unit="", unit_scale=False, unit_divisor=1000):
        self.unit = unit
        self.unit_scale = unit_scale
        self.unit_divisor = unit_divisor
        super().__init__()

    def render(self, task):
        """Show data transfer speed."""
        speed = task.speed
        if speed is None:
            return Text(f"? {self.unit}/s", style="progress.data.speed")
        if self.unit_scale:
            unit, suffix = filesize.pick_unit_and_suffix(
                speed,
                ["", "K", "M", "G", "T", "P", "E", "Z", "Y"],
                self.unit_divisor,
            )
        else:
            unit, suffix = filesize.pick_unit_and_suffix(speed, [""], 1)
        precision = 0 if unit == 1 else 1
        return Text(
            f"{speed/unit:,.{precision}f} {suffix}{self.unit}/s",
            style="progress.data.speed",
        )


def make_progress(description: str) -> Progress:
    return Progress(
        TextColumn(description),
        "[progress.percentage]{task.percentage:>4.0f}%",
        BarColumn(),
        MofNCompleteColumn(),
        "[",
        TimeElapsedColumn(),
        "<",
        TimeRemainingColumn(),
        ",",
        RateColumn(),
        "]",
    )
