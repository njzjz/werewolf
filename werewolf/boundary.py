"""Explicit message routing for public and private game information."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import MemoryEvent, PlayerState, Visibility

if TYPE_CHECKING:
    from collections.abc import Iterable


class InformationBoundary:
    """Deliver events only to explicit recipients.

    The authoritative history is useful for audits, but controllers can only
    access their own ``PlayerMemory``. There is intentionally no API that asks
    a controller to filter a global transcript itself.
    """

    def __init__(self, players: Iterable[PlayerState]) -> None:
        self._players = {player.player_id: player for player in players}
        self._sequence = 0
        self.audit_log: list[tuple[MemoryEvent, frozenset[str]]] = []

    def publish(
        self,
        *,
        day: int,
        phase: str,
        text: str,
        visibility: Visibility,
        recipients: Iterable[str],
        sender: str | None = None,
    ) -> MemoryEvent:
        """Create an event and copy it only into the named memories."""
        recipient_ids = frozenset(recipients)
        unknown = recipient_ids - self._players.keys()
        if unknown:
            msg = f"Unknown message recipients: {sorted(unknown)}"
            raise ValueError(msg)
        self._sequence += 1
        event = MemoryEvent(
            sequence=self._sequence,
            day=day,
            phase=phase,
            text=text,
            visibility=visibility,
            sender=sender,
        )
        for player_id in recipient_ids:
            self._players[player_id].memory.remember(event)
        self.audit_log.append((event, recipient_ids))
        return event

    def public(
        self,
        *,
        day: int,
        phase: str,
        text: str,
        sender: str | None = None,
    ) -> MemoryEvent:
        """Publish to every player, including eliminated observers."""
        return self.publish(
            day=day,
            phase=phase,
            text=text,
            visibility=Visibility.PUBLIC,
            recipients=self._players,
            sender=sender,
        )

    def private(
        self,
        *,
        day: int,
        phase: str,
        text: str,
        recipient: str,
        sender: str | None = None,
    ) -> MemoryEvent:
        """Deliver a secret to exactly one player."""
        return self.publish(
            day=day,
            phase=phase,
            text=text,
            visibility=Visibility.PRIVATE,
            recipients=(recipient,),
            sender=sender,
        )

    def werewolves(
        self,
        *,
        day: int,
        phase: str,
        text: str,
        recipients: Iterable[str],
        sender: str | None = None,
    ) -> MemoryEvent:
        """Deliver a team-channel message to an explicit wolf roster."""
        return self.publish(
            day=day,
            phase=phase,
            text=text,
            visibility=Visibility.WEREWOLF,
            recipients=recipients,
            sender=sender,
        )
