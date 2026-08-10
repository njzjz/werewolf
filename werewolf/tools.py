"""Read-only evidence tools exposed to one isolated LLM player.

Every handler accepts only :class:`~werewolf.models.PlayerView`, which has
already passed through the engine's information boundary. Tools therefore
cannot inspect authoritative roles, another player's memory, the filesystem,
or the network even when a model supplies adversarial arguments.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, NoReturn

from .models import (
    ROLE_NAMES,
    MemoryEvent,
    PlayerView,
    Visibility,
    localized,
    seat_label,
)
from .rendering import sanitize_rendered_text

MAX_TOOL_ARGUMENT_CHARS = 4096
MAX_TOOL_OUTPUT_CHARS = 12000
MAX_TOOL_QUERY_CHARS = 80


class ToolInputError(ValueError):
    """One model-visible invalid tool name or argument."""


@dataclass(frozen=True)
class ToolSpec:
    """Canonical function-tool definition shared by both OpenAI wire APIs."""

    name: str
    description: str
    parameters: dict[str, Any]

    def as_function(self) -> dict[str, Any]:
        """Return the API-neutral function schema without mutable aliases."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class PlayerToolbox:
    """Execute bounded evidence queries against exactly one player's view."""

    SPECS: tuple[ToolSpec, ...] = (
        ToolSpec(
            name="get_evidence_ledger",
            description=(
                "Return a compact evidence ledger from the current player's visible "
                "history: public role mentions, vote rounds, latest statements, "
                "private visible events, and private strategy notes. Use this before "
                "important votes or when checking whether a theory fits the timeline."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="search_visible_history",
            description=(
                "Search only events visible to the current player for an exact text "
                "fragment. Useful for checking a claimed role, target, contradiction, "
                "or earlier statement without rereading the full transcript."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Exact case-insensitive text fragment to search.",
                    },
                    "visibility": {
                        "type": "string",
                        "enum": ["all", "public", "private", "team"],
                        "description": "Limit results by information channel.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Maximum number of newest matches to return.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="get_player_dossier",
            description=(
                "Return the current player's visible dossier for one public player ID: "
                "their own public statements, mentions by others, relevant vote records, "
                "and any private visible events that mention them. This never reveals "
                "the target's hidden role."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "player_id": {
                        "type": "string",
                        "description": "Opaque player ID from the public seat map, e.g. p3.",
                    },
                },
                "required": ["player_id"],
                "additionalProperties": False,
            },
        ),
    )

    def __init__(self, view: PlayerView) -> None:
        self.view = view
        self._players = {
            player_id: {
                "player_id": player_id,
                "seat": seat,
                "name": name,
                "label": seat_label(seat, name, view.language),
            }
            for player_id, seat, name in view.seat_players
        }

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """Return immutable tool definitions safe to send to the provider."""
        return self.SPECS

    def execute(self, name: str, raw_arguments: str) -> str:
        """Run one known tool and return a bounded JSON result for the model."""
        try:
            arguments = self._arguments(raw_arguments)
            if name == "get_evidence_ledger":
                self._reject_extra(arguments, set())
                result = self._evidence_ledger()
            elif name == "search_visible_history":
                result = self._search_visible_history(arguments)
            elif name == "get_player_dossier":
                result = self._player_dossier(arguments)
            else:
                self._invalid(f"Unknown tool {name!r}")
        except (KeyError, ToolInputError, TypeError, ValueError) as exc:
            return self._serialize(
                {
                    "ok": False,
                    "error": sanitize_rendered_text(exc, limit=300),
                },
            )
        return self._serialize({"ok": True, "result": result})

    @staticmethod
    def _arguments(raw_arguments: str) -> dict[str, Any]:
        if len(raw_arguments) > MAX_TOOL_ARGUMENT_CHARS:
            PlayerToolbox._invalid("Tool arguments exceeded the size limit")
        try:
            value = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            PlayerToolbox._invalid("Tool arguments must be one JSON object")
        if not isinstance(value, dict):
            PlayerToolbox._invalid("Tool arguments must be one JSON object")
        return value

    @staticmethod
    def _reject_extra(arguments: dict[str, Any], allowed: set[str]) -> None:
        extra = sorted(set(arguments) - allowed)
        if extra:
            PlayerToolbox._invalid(f"Unknown tool argument {extra[0]!r}")

    @staticmethod
    def _invalid(message: str) -> NoReturn:
        """Raise argument failures outside the dispatcher's recovery block."""
        raise ToolInputError(message)

    @staticmethod
    def _bounded_text(value: object, *, limit: int = 300) -> str:
        return sanitize_rendered_text(value, limit=limit)

    def _event(self, event: MemoryEvent, *, text_limit: int = 300) -> dict[str, Any]:
        return {
            "sequence": event.sequence,
            "day": event.day,
            "phase": event.phase,
            "visibility": event.visibility.value,
            "sender": event.sender,
            "text": self._bounded_text(event.text, limit=text_limit),
        }

    def _evidence_ledger(self) -> dict[str, Any]:
        public = [
            event for event in self.view.events if event.visibility is Visibility.PUBLIC
        ]
        private = [
            event
            for event in self.view.events
            if event.visibility is not Visibility.PUBLIC
        ]
        role_names = tuple(localized(ROLE_NAMES, self.view.language).values())
        role_mentions = [
            {
                **self._event(event, text_limit=220),
                "mentioned_roles": [role for role in role_names if role in event.text],
            }
            for event in public
            if event.sender and any(role in event.text for role in role_names)
        ][-10:]
        vote_rounds = [
            {
                **self._event(event, text_limit=500),
                "pairs": self._vote_pairs(event.text),
            }
            for event in public
            if "公开投票结果" in event.text or "Public votes:" in event.text
        ][-6:]
        latest_by_sender: dict[str, dict[str, Any]] = {}
        for event in public:
            if event.sender:
                latest_by_sender[event.sender] = self._event(event, text_limit=220)
        return {
            "snapshot": {
                "day": self.view.day,
                "phase": self.view.phase,
                "mechanical_context": self.view.mechanical_context,
                "alive_player_ids": [
                    player_id for player_id, _ in self.view.alive_players
                ],
                "dead_player_ids": [
                    player_id for player_id, _ in self.view.dead_players
                ],
            },
            "public_role_mentions_not_confirmed_facts": role_mentions,
            "public_vote_rounds": vote_rounds,
            "latest_public_statement_by_sender": list(latest_by_sender.values())[-8:],
            "private_visible_events": [
                self._event(event, text_limit=260) for event in private[-8:]
            ],
            "private_strategy_notes": [
                {
                    "day": thought.day,
                    "phase": thought.phase,
                    "text": self._bounded_text(thought.text, limit=180),
                }
                for thought in self.view.thoughts[-6:]
            ],
        }

    @staticmethod
    def _vote_pairs(text: str) -> list[dict[str, str]]:
        pairs: list[dict[str, str]] = []
        for segment in text.replace("。", "").split("；"):
            if "→" not in segment:
                continue
            voter, target = segment.rsplit("→", 1)
            voter = voter.split("：")[-1].strip()
            if voter and target.strip():
                pairs.append({"voter": voter, "target": target.strip()})
        return pairs

    def _search_visible_history(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra(arguments, {"query", "visibility", "limit"})
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            self._invalid("query must be a non-empty string")
        query = query.strip()
        if len(query) > MAX_TOOL_QUERY_CHARS:
            self._invalid("query exceeded the size limit")
        visibility = arguments.get("visibility", "all")
        if visibility not in {"all", "public", "private", "team"}:
            self._invalid("visibility must be all, public, private, or team")
        limit = arguments.get("limit", 10)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 20
        ):
            self._invalid("limit must be an integer between 1 and 20")
        normalized = query.casefold()
        matches = [
            event
            for event in self.view.events
            if normalized in event.text.casefold()
            and self._matches_visibility(event.visibility, visibility)
        ]
        return {
            "query": query,
            "visibility": visibility,
            "matches": [
                self._event(event, text_limit=500) for event in matches[-limit:]
            ],
            "total_matches": len(matches),
        }

    @staticmethod
    def _matches_visibility(event: Visibility, requested: object) -> bool:
        if requested == "all":
            return True
        if requested == "public":
            return event is Visibility.PUBLIC
        if requested == "private":
            return event is Visibility.PRIVATE
        return event in {Visibility.WEREWOLF, Visibility.LOVERS}

    def _player_dossier(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra(arguments, {"player_id"})
        player_id = arguments.get("player_id")
        if not isinstance(player_id, str) or player_id not in self._players:
            self._invalid("player_id must come from the public seat map")
        player = self._players[player_id]
        # The heterogeneous serialized dossier stores a numeric seat alongside
        # textual fields; only these two known strings are valid search aliases.
        aliases = {str(player["name"]), str(player["label"]), player_id}
        public = [
            event for event in self.view.events if event.visibility is Visibility.PUBLIC
        ]
        own_statements = [
            event
            for event in public
            if event.sender in {player["name"], player["label"]}
        ]
        mentions = [
            event
            for event in public
            if any(alias and alias in event.text for alias in aliases)
            and event not in own_statements
        ]
        vote_records = [
            event
            for event in public
            if ("公开投票结果" in event.text or "Public votes:" in event.text)
            and any(alias and alias in event.text for alias in aliases)
        ]
        private_mentions = [
            event
            for event in self.view.events
            if event.visibility is not Visibility.PUBLIC
            and any(alias and alias in event.text for alias in aliases)
        ]
        return {
            "player": player,
            "public_statements": [
                self._event(event, text_limit=400) for event in own_statements[-8:]
            ],
            "public_mentions_by_others": [
                self._event(event, text_limit=320) for event in mentions[-8:]
            ],
            "public_vote_records": [
                {
                    **self._event(event, text_limit=500),
                    "pairs": self._vote_pairs(event.text),
                }
                for event in vote_records[-6:]
            ],
            "private_visible_mentions": [
                self._event(event, text_limit=280) for event in private_mentions[-6:]
            ],
        }

    @staticmethod
    def _serialize(value: dict[str, Any]) -> str:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(serialized) <= MAX_TOOL_OUTPUT_CHARS:
            return serialized
        return json.dumps(
            {
                "ok": False,
                "error": "Tool output exceeded the size limit; narrow the query.",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
