"""Tests for explicit information routing."""

from __future__ import annotations

import pytest

from werewolf.boundary import InformationBoundary
from werewolf.models import PlayerState, Role, Visibility


def make_player(player_id: str, name: str, role: Role) -> PlayerState:
    """Create a minimal judge-owned player for routing tests."""
    return PlayerState(
        player_id=player_id,
        name=name,
        role=role,
        controller=object(),
        skills=(),
    )


def test_information_is_copied_only_to_explicit_recipients() -> None:
    """Private and team messages must never enter an outsider's memory."""
    wolf_a = make_player("p1", "狼一", Role.WEREWOLF)
    wolf_b = make_player("p2", "狼二", Role.WEREWOLF)
    seer = make_player("p3", "预言家", Role.SEER)
    boundary = InformationBoundary([wolf_a, wolf_b, seer])

    boundary.public(day=1, phase="day", text="公开消息")
    boundary.private(day=1, phase="night", text="查验秘密", recipient="p3")
    boundary.werewolves(
        day=1,
        phase="night",
        text="狼队秘密",
        recipients=("p1", "p2"),
    )

    assert [event.text for event in wolf_a.memory.events] == ["公开消息", "狼队秘密"]
    assert [event.text for event in wolf_b.memory.events] == ["公开消息", "狼队秘密"]
    assert [event.text for event in seer.memory.events] == ["公开消息", "查验秘密"]
    assert wolf_a.memory.events[-1].visibility is Visibility.WEREWOLF
    assert seer.memory.events[-1].visibility is Visibility.PRIVATE


def test_boundary_rejects_unknown_recipients() -> None:
    """A typo in a recipient ID must fail closed instead of broadening scope."""
    player = make_player("p1", "一号", Role.VILLAGER)
    boundary = InformationBoundary([player])

    with pytest.raises(ValueError, match="missing"):
        boundary.private(day=0, phase="setup", text="秘密", recipient="missing")


def test_lover_channel_is_delivered_only_to_the_linked_pair() -> None:
    """Lover chat is a separate capability and must fail closed for outsiders."""
    first = make_player("p1", "恋人一", Role.VILLAGER)
    second = make_player("p2", "恋人二", Role.WEREWOLF)
    outsider = make_player("p3", "旁观者", Role.SEER)
    boundary = InformationBoundary([first, second, outsider])

    boundary.lovers(
        day=1,
        phase="night",
        text="恋人秘密",
        recipients=("p1", "p2"),
    )

    assert first.memory.events[-1].visibility is Visibility.LOVERS
    assert second.memory.events[-1].text == "恋人秘密"
    assert not outsider.memory.events
