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
    ActionRequest,
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
        ToolSpec(
            name="get_vote_analysis",
            description=(
                "Convert visible public vote announcements into structured rounds, "
                "per-player vote histories, target coalitions, and target switches. "
                "This reports behavior only and never labels a faction."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="get_claim_matrix",
            description=(
                "Organize public role mentions, explicit self-claims, and explicit "
                "denials by speaker and event sequence. Every item remains an "
                "unverified public claim rather than a judge-confirmed role."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="review_action_draft",
            description=(
                "Review a proposed action before the final answer. Check the exact "
                "choice value, required text, cited visible evidence sequences, "
                "counter-case, and follow-up plan. Revise the final JSON when this "
                "tool reports issues."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "choice": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Draft exact option value, or null only for abstention.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Draft channel text, possibly empty for choice-only actions.",
                    },
                    "evidence_sequences": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "maxItems": 8,
                        "description": "Visible event sequence numbers supporting the draft.",
                    },
                    "counter_case": {
                        "type": "string",
                        "description": "Strongest plausible alternative explanation.",
                    },
                    "plan": {
                        "type": "string",
                        "description": "What to verify or do after this action.",
                    },
                },
                "required": [
                    "choice",
                    "text",
                    "evidence_sequences",
                    "counter_case",
                    "plan",
                ],
                "additionalProperties": False,
            },
        ),
    )

    def __init__(
        self,
        view: PlayerView,
        request: ActionRequest | None = None,
    ) -> None:
        self.view = view
        self.request = request
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

    @property
    def evidence_specs(self) -> tuple[ToolSpec, ...]:
        """Return retrieval and analysis tools used before drafting an action."""
        return tuple(spec for spec in self.SPECS if spec.name != "review_action_draft")

    @property
    def review_spec(self) -> ToolSpec:
        """Return the single deterministic draft-review tool definition."""
        return next(spec for spec in self.SPECS if spec.name == "review_action_draft")

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
            elif name == "get_vote_analysis":
                self._reject_extra(arguments, set())
                result = self._vote_analysis()
            elif name == "get_claim_matrix":
                self._reject_extra(arguments, set())
                result = self._claim_matrix()
            elif name == "review_action_draft":
                result = self._review_action_draft(arguments)
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
        themed_role_names = (
            ("杀手", "警察", "平民", "幽灵", "水民")
            if self.view.language != "en"
            else ("Killer", "Police", "Civilian", "Ghost", "Water Civilian")
        )
        role_names = tuple(
            dict.fromkeys(
                (
                    *localized(ROLE_NAMES, self.view.language).values(),
                    self.view.adversary_name,
                    *themed_role_names,
                ),
            ),
        )
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
            "structured_strategy_state": self._strategy_state(),
        }

    def _strategy_state(self) -> dict[str, Any]:
        strategy = self.view.strategy
        return {
            "updated_day": strategy.day,
            "updated_phase": strategy.phase,
            "beliefs": [
                {
                    "player_id": belief.player_id,
                    "suspicion": belief.suspicion,
                    "confidence": belief.confidence,
                    "evidence_sequences": list(belief.evidence_sequences),
                    "rationale": self._bounded_text(belief.rationale, limit=220),
                }
                for belief in strategy.beliefs
            ],
            "open_questions": [
                self._bounded_text(question, limit=160)
                for question in strategy.open_questions
            ],
            "plan": self._bounded_text(strategy.plan, limit=240),
            "counter_case": self._bounded_text(strategy.counter_case, limit=240),
        }

    @staticmethod
    def _vote_pairs(text: str) -> list[dict[str, str]]:
        pairs: list[dict[str, str]] = []
        for segment in text.replace("。", "").split("；"):
            if "→" not in segment:
                continue
            voter, target = segment.rsplit("→", 1)
            voter = voter.split("：")[-1].split("Public votes:")[-1].strip()
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
        return event in {
            Visibility.WEREWOLF,
            Visibility.POLICE,
            Visibility.LOVERS,
        }

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

    def _player_id_for_reference(self, value: str) -> str | None:
        """Resolve only exact public identifiers, names, and rendered labels."""
        clean = value.replace("（后备）", "").replace(" (fallback)", "").strip()
        for player_id, player in self._players.items():
            if clean in {player_id, str(player["name"]), str(player["label"])}:
                return player_id
        return None

    def _vote_analysis(self) -> dict[str, Any]:
        rounds: list[dict[str, Any]] = []
        history: dict[str, list[str | None]] = {}
        for event in self.view.events:
            if event.visibility is not Visibility.PUBLIC or not (
                "公开投票结果" in event.text or "Public votes:" in event.text
            ):
                continue
            pairs: list[dict[str, Any]] = []
            coalitions: dict[str, list[str]] = {}
            for pair in self._vote_pairs(event.text):
                voter_id = self._player_id_for_reference(pair["voter"])
                raw_target = pair["target"].rstrip(".")
                target_id = self._player_id_for_reference(raw_target)
                if voter_id is None:
                    continue
                abstained = raw_target in {"弃权", "abstain"}
                normalized_target = None if abstained else target_id
                pairs.append(
                    {
                        "voter_id": voter_id,
                        "target_id": normalized_target,
                        "target_label": raw_target,
                    },
                )
                history.setdefault(voter_id, []).append(normalized_target)
                coalition_key = normalized_target or "abstain_or_unresolved"
                coalitions.setdefault(coalition_key, []).append(voter_id)
            rounds.append(
                {
                    "sequence": event.sequence,
                    "day": event.day,
                    "phase": event.phase,
                    "pairs": pairs,
                    "coalitions": coalitions,
                },
            )
        switches = {
            voter_id: sum(
                previous != current for previous, current in zip(targets, targets[1:])
            )
            for voter_id, targets in history.items()
        }
        return {
            "rounds": rounds[-8:],
            "vote_history_by_player": history,
            "target_switches_by_player": switches,
            "interpretation_warning": (
                "Vote alignment and switching are behavior, not confirmed faction evidence."
            ),
        }

    def _claim_matrix(self) -> dict[str, Any]:
        role_names = tuple(localized(ROLE_NAMES, self.view.language).values())
        claims: dict[str, list[dict[str, Any]]] = {}
        for event in self.view.events:
            if event.visibility is not Visibility.PUBLIC or not event.sender:
                continue
            player_id = self._player_id_for_reference(event.sender)
            if player_id is None:
                continue
            normalized = event.text.casefold()
            for role in role_names:
                role_text = role.casefold()
                if role_text not in normalized:
                    continue
                self_claim = any(
                    marker in normalized
                    for marker in (
                        f"我是{role_text}",
                        f"我才是{role_text}",
                        f"我就是{role_text}",
                        f"我跳{role_text}",
                        f"身份是{role_text}",
                        f"i am {role_text}",
                        f"i am the {role_text}",
                        f"i'm {role_text}",
                        f"my role is {role_text}",
                    )
                )
                denial = any(
                    marker in normalized
                    for marker in (
                        f"我不是{role_text}",
                        f"i am not {role_text}",
                        f"i'm not {role_text}",
                    )
                )
                claims.setdefault(player_id, []).append(
                    {
                        "sequence": event.sequence,
                        "day": event.day,
                        "phase": event.phase,
                        "role": role,
                        "self_claim": self_claim and not denial,
                        "explicit_denial": denial,
                        "text": self._bounded_text(event.text, limit=260),
                    },
                )
        return {
            "claims_are_unverified": True,
            "by_player": claims,
        }

    def _review_action_draft(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra(
            arguments,
            {"choice", "text", "evidence_sequences", "counter_case", "plan"},
        )
        if self.request is None:
            self._invalid("No current action request is available for review")
        choice = arguments.get("choice")
        text = arguments.get("text")
        sequences = arguments.get("evidence_sequences")
        counter_case = arguments.get("counter_case")
        plan = arguments.get("plan")
        if choice is not None and not isinstance(choice, str):
            self._invalid("choice must be a string or null")
        if not isinstance(text, str):
            self._invalid("text must be a string")
        if (
            not isinstance(sequences, list)
            or len(sequences) > 8
            or not all(
                isinstance(sequence, int) and not isinstance(sequence, bool)
                for sequence in sequences
            )
        ):
            self._invalid("evidence_sequences must contain at most 8 integers")
        if not isinstance(counter_case, str) or not isinstance(plan, str):
            self._invalid("counter_case and plan must be strings")
        legal = {option.value for option in self.request.options}
        issues: list[str] = []
        if choice is None:
            if self.request.options and not self.request.allow_abstain:
                issues.append(
                    "choice is null but this action does not allow abstention"
                )
        elif choice not in legal:
            issues.append("choice is not an exact legal option value")
        if self.request.requires_text and not text.strip():
            issues.append("text is required but empty")
        events = {event.sequence: event for event in self.view.events}
        missing = sorted({sequence for sequence in sequences if sequence not in events})
        if missing:
            issues.append(f"evidence sequences are not visible: {missing}")
        warnings: list[str] = []
        if self.view.events and not sequences:
            warnings.append("no visible evidence sequence supports the draft")
        if self.view.events and not counter_case.strip():
            warnings.append("counter_case is empty")
        if not plan.strip():
            warnings.append("follow-up plan is empty")
        return {
            "ready": not issues,
            "issues": issues,
            "warnings": warnings,
            "validated_evidence": [
                self._event(events[sequence], text_limit=240)
                for sequence in dict.fromkeys(sequences)
                if sequence in events
            ],
            "instruction": (
                "Fix every issue and consider warnings before returning final JSON."
            ),
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
