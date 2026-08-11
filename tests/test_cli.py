"""Command-line ergonomics tests."""

from __future__ import annotations

import json

import pytest

import werewolf.cli as cli_module
from werewolf.cli import build_parser, main


def test_play_accepts_a_concise_positional_config_path() -> None:
    """Users should not need --config for the common play command."""
    args = build_parser().parse_args(["play", "custom.json"])

    assert args.config_path == "custom.json"
    assert args.config_option is None


def test_play_requires_an_explicit_flag_to_replace_an_old_run() -> None:
    """The destructive fresh-run override should be visible at the CLI boundary."""
    args = build_parser().parse_args(["play", "custom.json", "--force-new"])

    assert args.force_new is True


def test_init_can_request_the_exhaustive_reference_template() -> None:
    """The noisy full schema should remain an explicit advanced option."""
    args = build_parser().parse_args(["init", "custom.json", "--full"])

    assert args.path == "custom.json"
    assert args.full is True


@pytest.mark.parametrize("preset", ["killer", "ghost_similar"])
def test_demo_accepts_each_social_deduction_mode(preset: str) -> None:
    """The social modes should be directly discoverable from the demo CLI."""
    args = build_parser().parse_args(["demo", "--preset", preset])

    assert args.preset == preset


@pytest.mark.parametrize("preset", ["killer", "ghost_similar"])
def test_demo_accepts_player_count_independently_from_social_mode(
    preset: str,
) -> None:
    """The CLI parser should preserve an explicitly selected non-eight table."""
    args = build_parser().parse_args(
        ["demo", "--preset", preset, "--players", "12"],
    )

    assert args.preset == preset
    assert args.players == 12


def test_demo_rejects_blank_ghost_without_an_llm_provider() -> None:
    """The no-word variant cannot claim to be an API-free local demo."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["demo", "--preset", "ghost_blank"])


def test_configure_exposes_the_interactive_workbench_aliases() -> None:
    """The TUI should be discoverable through common configuration verbs."""
    parser = build_parser()

    assert parser.parse_args(["configure", "custom.json"]).path == "custom.json"
    assert parser.parse_args(["config"]).command == "config"
    assert parser.parse_args(["setup"]).command == "setup"


def test_play_rejects_a_missing_provider_environment_key_before_startup(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    """A missing operator credential must not create a half-started match."""
    environment_name = "WEREWOLF_TEST_MISSING_API_KEY"
    monkeypatch.delenv(environment_name, raising=False)
    checkpoint = tmp_path / "private.checkpoint.json"
    config_path = tmp_path / "game.json"
    config_path.write_text(
        json.dumps(
            {
                "checkpoint_path": str(checkpoint),
                "public_transcript_path": None,
                "memory_directory": None,
                "roles": [
                    "werewolf",
                    "werewolf",
                    "seer",
                    "witch",
                    "villager",
                    "villager",
                ],
                "providers": {
                    "default": {
                        "base_url": "https://example.invalid/v1",
                        "api_key_env": environment_name,
                        "model": "test-model",
                    },
                },
                "players": [
                    {
                        "name": f"智能体{index}",
                        "controller": "llm",
                        "provider": "default",
                    }
                    for index in range(1, 7)
                ],
            },
        ),
        encoding="utf-8",
    )

    class UnexpectedGame:
        def __init__(self, *_args, **_kwargs) -> None:
            pytest.fail("Game must not be constructed without its configured key")

    monkeypatch.setattr(cli_module, "Game", UnexpectedGame)

    with pytest.raises(SystemExit) as captured:
        main(["play", str(config_path)])

    stderr = capsys.readouterr().err
    assert captured.value.code == 2
    assert environment_name in stderr
    assert "当前终端" in stderr
    assert not checkpoint.exists()
    assert "Traceback" not in stderr


def test_eof_is_a_recoverable_cli_interruption_with_resume_command(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    """Ctrl-D should retain recovery instructions instead of printing a traceback."""
    checkpoint = tmp_path / "private.checkpoint.json"
    checkpoint.write_text("{}", encoding="utf-8")
    config_path = tmp_path / "game.json"
    config_path.write_text(
        json.dumps(
            {
                "checkpoint_path": str(checkpoint),
                "public_transcript_path": None,
                "memory_directory": None,
                "roles": [
                    "werewolf",
                    "werewolf",
                    "seer",
                    "witch",
                    "villager",
                    "villager",
                ],
                "players": [
                    {"name": f"玩家{index}", "controller": "bot"}
                    for index in range(1, 7)
                ],
            },
        ),
        encoding="utf-8",
    )

    class EOFGame:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self):
            raise EOFError

    monkeypatch.setattr(cli_module, "Game", EOFGame)

    with pytest.raises(SystemExit) as captured:
        main(["play", str(config_path)])

    stderr = capsys.readouterr().err
    assert captured.value.code == 130
    assert "输入已关闭" in stderr
    assert f"--resume {checkpoint}" in stderr
    assert "Traceback" not in stderr


def test_memory_export_oserror_uses_concise_cli_recovery_path(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    """Filesystem failures after a match should retain the checkpoint hint."""
    checkpoint = tmp_path / "private.checkpoint.json"
    checkpoint.write_text("{}", encoding="utf-8")
    config_path = tmp_path / "game.json"
    config_path.write_text(
        json.dumps(
            {
                "checkpoint_path": str(checkpoint),
                "public_transcript_path": None,
                "memory_directory": None,
                "roles": [
                    "werewolf",
                    "werewolf",
                    "seer",
                    "witch",
                    "villager",
                    "villager",
                ],
                "players": [
                    {"name": f"玩家{index}", "controller": "bot"}
                    for index in range(1, 7)
                ],
            },
        ),
        encoding="utf-8",
    )

    class FailingExportGame:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self):
            raise OSError(36, "memory export filename too long")

    monkeypatch.setattr(cli_module, "Game", FailingExportGame)

    with pytest.raises(SystemExit) as captured:
        main(["play", str(config_path)])

    stderr = capsys.readouterr().err
    assert captured.value.code == 2
    assert "memory export filename too long" in stderr
    assert f"--resume {checkpoint}" in stderr
    assert "Traceback" not in stderr
