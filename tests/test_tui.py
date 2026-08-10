"""Interactive configuration workbench tests."""

from __future__ import annotations

import io
import stat
from dataclasses import replace
from typing import TYPE_CHECKING

from werewolf.config import (
    LLMProviderConfig,
    RuleConfig,
    demo_config,
    load_config,
    write_config,
)
from werewolf.engine import role_deck
from werewolf.models import Role
from werewolf.tui import run_config_tui

if TYPE_CHECKING:
    from pathlib import Path


def test_tui_can_create_a_valid_config_without_direct_json_editing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The numbered fallback should cover the complete create-and-save path."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_path = tmp_path / "guided.json"
    answers = io.StringIO(
        "1\n"  # Start configuring.
        "3\n"  # Open providers from the dashboard.
        "1\n"  # Edit the default provider.
        "2\n"  # Edit its model ID.
        "test-model\n"
        "14\n"  # Finish provider fields.
        "3\n"  # Finish the provider list.
        "6\n"  # Review and save.
        "2\n",  # Save without starting the game.
    )
    output = io.StringIO()

    result = run_config_tui(
        config_path,
        stdin=answers,
        stdout=output,
        color=False,
    )

    assert result is not None
    assert result.saved is True
    assert result.start_game is False
    config = load_config(config_path)
    assert config.providers["default"].model == "test-model"
    assert len(config.players) == 8
    assert config.players[0].controller == "human"
    assert all(player.controller == "llm" for player in config.players[1:])
    assert "配置校验通过" in output.getvalue()


def test_atomic_config_writer_round_trips_advanced_values_privately(
    tmp_path: Path,
) -> None:
    """Saving through the TUI backend must preserve advanced existing fields."""
    base = demo_config(6, seed=23)
    players = list(base.players)
    players[0] = replace(
        players[0],
        persona="只使用可公开验证的信息",
        skills=("logic", "social", "memory"),
        fixed_role=Role.SEER,
    )
    config = replace(
        base,
        players=tuple(players),
        roles=tuple(role_deck(6)),
        providers={
            "unused": LLMProviderConfig(
                base_url="https://example.invalid/v1",
                model="advanced-model",
                extra_headers={"X-Deployment": "staging"},
            ),
        },
        rules=replace(RuleConfig(), reveal_roles_on_death=True),
        public_transcript_path="runs/public.log",
        checkpoint_path="runs/private.json",
    )
    path = tmp_path / "complete.json"

    write_config(config, path)

    assert load_config(path) == config
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
