"""Terminal-first Werewolf game for human and LLM players."""

from __future__ import annotations

from .engine import Game, GameResult

try:
    from ._version import __version__
except ModuleNotFoundError:  # Source trees have no generated setuptools-scm file yet.
    __version__ = "0.0.0+unknown"

__all__ = ["Game", "GameResult", "__version__"]
