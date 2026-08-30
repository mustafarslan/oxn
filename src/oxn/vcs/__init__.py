"""Tier 2.5: behavioural metrics from version-control history.

Exact, zero dependencies, and the best value-to-effort ratio in the catalogue: no parser,
no resolver and no grammar work, just ``git log``. What history reveals -- which code churns,
which files change together, who knows what -- is invisible to any purely static analysis.
"""

from oxn.vcs.analysis import (
    ChangeCoupling,
    FileHistory,
    Hotspot,
    Ownership,
    change_coupling,
    file_histories,
    hotspots,
    ownership,
)
from oxn.vcs.log import Commit, FileChange, GitLogError, read_log

__all__ = [
    "ChangeCoupling",
    "Commit",
    "FileChange",
    "FileHistory",
    "GitLogError",
    "Hotspot",
    "Ownership",
    "change_coupling",
    "file_histories",
    "hotspots",
    "ownership",
    "read_log",
]
