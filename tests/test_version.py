"""Test version."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from werewolf import __version__


def test_version() -> None:
    """Test version."""
    try:
        installed_version = version("llm-werewolf")
    except PackageNotFoundError:
        assert __version__ == "0.0.0+unknown"
    else:
        assert installed_version == __version__
