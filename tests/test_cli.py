"""Command-line ergonomics tests."""

from __future__ import annotations

from werewolf.cli import build_parser


def test_play_accepts_a_concise_positional_config_path() -> None:
    """Users should not need --config for the common play command."""
    args = build_parser().parse_args(["play", "custom.json"])

    assert args.config_path == "custom.json"
    assert args.config_option is None


def test_init_can_request_the_exhaustive_reference_template() -> None:
    """The noisy full schema should remain an explicit advanced option."""
    args = build_parser().parse_args(["init", "custom.json", "--full"])

    assert args.path == "custom.json"
    assert args.full is True
