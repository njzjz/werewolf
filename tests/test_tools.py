"""Tests for player-scoped read-only LLM evidence tools."""

from __future__ import annotations

import json

from werewolf.models import (
    Faction,
    MemoryEvent,
    PlayerView,
    Role,
    Thought,
    Visibility,
)
from werewolf.tools import PlayerToolbox


def tool_view() -> PlayerView:
    """Return one view containing public, private, and team-visible evidence."""
    return PlayerView(
        player_id="p1",
        name="真人玩家",
        role=Role.SEER,
        role_name="预言家",
        role_description="每晚查验一人",
        faction=Faction.GOOD,
        lover=None,
        alive_players=(("p1", "真人玩家"), ("p2", "智能体2"), ("p3", "智能体3")),
        dead_players=(),
        events=(
            MemoryEvent(
                sequence=1,
                day=1,
                phase="discussion",
                sender="2号 智能体2",
                text="2号 智能体2：我认为3号像狼人，但我不是预言家。",
                visibility=Visibility.PUBLIC,
            ),
            MemoryEvent(
                sequence=2,
                day=1,
                phase="vote",
                text="公开投票结果：1号 真人玩家→3号 智能体3；2号 智能体2→3号 智能体3；3号 智能体3→弃权。",
                visibility=Visibility.PUBLIC,
            ),
            MemoryEvent(
                sequence=3,
                day=1,
                phase="night",
                text="查验结果：3号 智能体3 属于【狼人侧】。",
                visibility=Visibility.PRIVATE,
            ),
            MemoryEvent(
                sequence=4,
                day=1,
                phase="night",
                sender="真人玩家",
                text="队伍频道里提到了2号 智能体2。",
                visibility=Visibility.LOVERS,
            ),
        ),
        thoughts=(Thought(day=1, phase="vote", text="下一轮重点核验3号。"),),
        skills=(),
        day=2,
        phase="discussion",
        language="zh-CN",
        seat_number=1,
        seat_players=(
            ("p1", 1, "真人玩家"),
            ("p2", 2, "智能体2"),
            ("p3", 3, "智能体3"),
        ),
        mechanical_context="最近一次胜负检查允许至多1名存活狼人。",
    )


def execute(toolbox: PlayerToolbox, name: str, arguments: dict[str, object]) -> dict:
    """Decode one tool result so assertions remain readable."""
    return json.loads(toolbox.execute(name, json.dumps(arguments, ensure_ascii=False)))


def test_evidence_ledger_separates_claims_votes_and_private_facts() -> None:
    """The ledger should label public claims without promoting them to facts."""
    result = execute(PlayerToolbox(tool_view()), "get_evidence_ledger", {})

    assert result["ok"] is True
    ledger = result["result"]
    assert set(
        ledger["public_role_mentions_not_confirmed_facts"][0]["mentioned_roles"],
    ) == {"狼人", "预言家"}
    assert ledger["public_vote_rounds"][0]["pairs"][0] == {
        "target": "3号 智能体3",
        "voter": "1号 真人玩家",
    }
    assert "狼人侧" in ledger["private_visible_events"][0]["text"]
    assert ledger["private_strategy_notes"][0]["text"] == "下一轮重点核验3号。"


def test_history_search_respects_visibility_filters() -> None:
    """A model may query only the channels already present in its PlayerView."""
    toolbox = PlayerToolbox(tool_view())

    public = execute(
        toolbox,
        "search_visible_history",
        {"query": "3号", "visibility": "public"},
    )
    private = execute(
        toolbox,
        "search_visible_history",
        {"query": "3号", "visibility": "private"},
    )
    team = execute(
        toolbox,
        "search_visible_history",
        {"query": "2号", "visibility": "team"},
    )

    assert {item["visibility"] for item in public["result"]["matches"]} == {"public"}
    assert {item["visibility"] for item in private["result"]["matches"]} == {"private"}
    assert {item["visibility"] for item in team["result"]["matches"]} == {"lovers"}


def test_player_dossier_uses_public_ids_and_never_claims_a_hidden_role() -> None:
    """Dossiers organize visible evidence but cannot inspect authoritative state."""
    result = execute(
        PlayerToolbox(tool_view()),
        "get_player_dossier",
        {"player_id": "p3"},
    )

    assert result["ok"] is True
    dossier = result["result"]
    assert dossier["player"]["label"] == "3号 智能体3"
    assert dossier["public_vote_records"][0]["pairs"][0]["target"] == "3号 智能体3"
    assert "hidden_role" not in dossier
    assert "role" not in dossier["player"]


def test_tool_arguments_are_strict_and_fail_as_model_visible_json() -> None:
    """Bad model-generated arguments should not escape into controller errors."""
    toolbox = PlayerToolbox(tool_view())

    unknown = json.loads(toolbox.execute("shell", "{}"))
    extra = execute(toolbox, "get_evidence_ledger", {"path": "/etc/passwd"})
    bad_player = execute(toolbox, "get_player_dossier", {"player_id": "p999"})

    assert unknown["ok"] is False
    assert extra["ok"] is False
    assert bad_player["ok"] is False
    assert "/etc/passwd" not in json.dumps(extra, ensure_ascii=False)
