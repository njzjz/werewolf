"""Rules-engine integration tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from werewolf.agents import Terminal
from werewolf.config import (
    GameConfig,
    PlayerConfig,
    RuleConfig,
    demo_config,
    load_config,
)
from werewolf.engine import DeathCause, Game, role_deck
from werewolf.models import Faction, Role, Visibility


class SilentTerminal(Terminal):
    """Suppress judge announcements during tests."""

    def __init__(self) -> None:
        super().__init__(clear_screen=False)

    def announce(self, text: str) -> None:
        """Discard public output; the boundary still records it."""


@pytest.mark.parametrize(
    ("count", "wolves", "hunters"),
    [(6, 2, 0), (8, 2, 1), (9, 3, 1), (12, 4, 1), (16, 5, 1)],
)
def test_role_deck_is_balanced(count: int, wolves: int, hunters: int) -> None:
    """Classic decks include the expected hostile and special roles."""
    deck = role_deck(count)
    assert len(deck) == count
    assert deck.count(Role.WEREWOLF) == wolves
    assert deck.count(Role.SEER) == 1
    assert deck.count(Role.WITCH) == 1
    assert deck.count(Role.HUNTER) == hunters


def fixed_config() -> GameConfig:
    """Return a deterministic six-player role assignment."""
    roles = [
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.SEER,
        Role.WITCH,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    return GameConfig(
        language="zh-CN",
        players=tuple(
            PlayerConfig(
                name=f"玩家{index}",
                controller="bot",
                fixed_role=role,
            )
            for index, role in enumerate(roles, start=1)
        ),
        seed=1,
        clear_screen=False,
        memory_directory=None,
        rules=RuleConfig(max_days=5),
    )


def test_setup_keeps_roles_private_and_wolf_roster_team_only() -> None:
    """Setup secrets are delivered to the owning player or the wolf team."""
    game = Game(fixed_config(), terminal=SilentTerminal())
    game._setup()  # noqa: SLF001 - setup routing is the unit under test.

    for player in game.players:
        own_role_events = [
            event for event in player.memory.events if "你的身份是" in event.text
        ]
        assert len(own_role_events) == 1
        assert own_role_events[0].visibility is Visibility.PRIVATE
    wolf_roster_recipients = [
        recipients
        for event, recipients in game.boundary.audit_log
        if "狼人队友名单" in event.text
    ]
    assert wolf_roster_recipients == [frozenset({"p1", "p2"})]
    assert all(
        "狼人队友名单" not in event.text for event in game.players[2].memory.events
    )


def test_players_receive_global_and_role_specific_skills() -> None:
    """Every seat should automatically receive only its own role playbook."""
    game = Game(fixed_config(), terminal=SilentTerminal())

    for player in game.players:
        skill_names = {skill.name for skill in player.skills}
        assert "global_gamecraft" in skill_names
        assert f"role_{player.role.value}" in skill_names
        other_role_skills = {
            f"role_{role.value}" for role in Role if role is not player.role
        }
        assert not skill_names & other_role_skills


def test_sixteen_llm_case_config_is_loadable() -> None:
    """The documented case configuration should stay executable."""
    path = Path(__file__).parents[1] / "examples" / "16_llm_responses.json"

    config = load_config(path)

    assert len(config.players) == 16
    assert all(player.controller == "llm" for player in config.players)
    assert config.providers["responses"].wire_api == "responses"

    transcript = (
        path.parent / "case_studies" / "16_llm_2026-07-20_public_transcript.txt"
    ).read_text(encoding="utf-8")
    assert "[法官] 狼人阵营获胜" in transcript
    assert "达到最大天数" not in transcript
    assert "[恢复]" not in transcript
    assert "[技术回滚]" not in transcript
    assert "控制器调用失败" not in transcript
    assert "OPENAI_API_KEY" not in transcript


def test_offline_game_completes_and_exports_separate_memories(tmp_path: Path) -> None:
    """A full judge-only simulation should terminate without any API access."""
    output = str(tmp_path)
    config = replace(demo_config(8, seed=7), memory_directory=output)
    game = Game(config, terminal=SilentTerminal())

    result = game.run()

    assert result.winner in {Faction.GOOD, Faction.WEREWOLF, None}
    assert result.days <= config.rules.max_days
    wolf_ids = {
        player.player_id for player in game.players if player.role is Role.WEREWOLF
    }
    for event, recipients in game.boundary.audit_log:
        if event.visibility is Visibility.WEREWOLF:
            assert recipients <= wolf_ids
        elif event.visibility is Visibility.PRIVATE:
            assert len(recipients) == 1
    paths = sorted(tmp_path.glob("*.json"))
    assert len(paths) == 8
    for path, player in zip(paths, game.players):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["player_id"] == player.player_id
        assert payload["role"] == player.role.value
    non_wolf = next(
        player for player in game.players if player.role is not Role.WEREWOLF
    )
    non_wolf_path = next(
        path for path in paths if path.name.startswith(f"{non_wolf.player_id}_")
    )
    non_wolf_payload = json.loads(non_wolf_path.read_text(encoding="utf-8"))
    assert all(
        event["visibility"] != Visibility.WEREWOLF.value
        for event in non_wolf_payload["events"]
    )
    assert all(
        "狼人队友名单" not in event["text"] for event in non_wolf_payload["events"]
    )
    assert any(player.memory.thoughts for player in game.players)


def test_invalid_role_count_is_rejected() -> None:
    """Unsupported table sizes should fail before a match starts."""
    with pytest.raises(ValueError, match="6 to 16"):
        role_deck(5)


def test_last_words_follow_death_timing_and_cause() -> None:
    """Later night deaths stay silent while daytime exiles may speak."""
    game = Game(fixed_config(), terminal=SilentTerminal())

    game.day = 1
    assert game._allows_last_words({DeathCause.WOLF})  # noqa: SLF001
    game.day = 2
    assert not game._allows_last_words({DeathCause.WOLF})  # noqa: SLF001
    assert game._allows_last_words({DeathCause.VOTE})  # noqa: SLF001
    assert not game._allows_last_words({DeathCause.HUNTER})  # noqa: SLF001
