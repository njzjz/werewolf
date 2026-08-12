"""Offline self-play trajectories, rewards, and skill evaluation utilities.

Training data is deliberately produced by the judge after a controller action.
Each observation is serialized from that controller's already-sanitized
``PlayerView``; authoritative roles belonging to other players never enter the
observation. The terminal match summary may contain all roles for offline
reward computation and must therefore be treated as private data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .config import GameConfig, SkillOverrideConfig, validate_config
from .models import Skill

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .models import (
        ActionRequest,
        AgentResponse,
        Faction,
        PlayerState,
        PlayerView,
    )

TRAJECTORY_SCHEMA_VERSION = 1
RETRY_PENALTY = -0.05
FALLBACK_PENALTY = -0.10
SKILL_IMPROVEMENT_SCHEMA_VERSION = 1

_RANKING_CLAIM_TERMS = ("同序", "排序雷同", "完全一致的排序", "几乎一致的排序")
_PUBLIC_TEMPLATE_TERMS = (
    "首选和备选",
    "首选、备选",
    "可核验依据",
    "反方解释",
    "两条线都",
    "还没人拆",
    "框架高度雷同",
    "投票阶段第一个",
)
_HIGH_VALUE_GOOD_ROLES = {
    "seer",
    "witch",
    "hunter",
    "medium",
    "bodyguard",
    "police",
}
_ROLE_CLAIM_TERMS = {
    "seer": "预言家",
    "witch": "女巫",
    "hunter": "猎人",
    "medium": "灵媒",
    "bodyguard": "守卫",
    "werewolf": "狼人",
}


def claims_own_role(text: str, role_term: str) -> bool:
    """Detect an explicit self-claim without treating discussion of others as one."""
    return any(
        marker in text
        for marker in (
            f"我是{role_term}",
            f"我确实是{role_term}",
            f"我真的是{role_term}",
            f"我就是{role_term}",
            f"我作为{role_term}",
            f"我的身份是{role_term}",
            f"I am the {role_term}",
            f"I really am the {role_term}",
        )
    )


def _claims_role_or_unique_action(text: str, role: str) -> bool:
    """Detect an explicit role claim or an unmistakable first-person ability claim."""
    role_term = _ROLE_CLAIM_TERMS.get(role)
    if role_term is not None and claims_own_role(text, role_term):
        return True
    compact = text.replace(" ", "")
    if role == "witch":
        return "我" in compact and (
            ("解药" in compact and any(term in compact for term in ("用", "救")))
            or ("毒药" in compact and any(term in compact for term in ("用", "留")))
        )
    if role == "seer":
        return any(
            marker in compact for marker in ("我查验了", "我验了", "我的查验结果")
        )
    return False


def skill_fingerprint(skill: Skill) -> str:
    """Return a stable content identity for one prompt skill."""
    payload = json.dumps(
        {
            "name": skill.name,
            "version": skill.version,
            "description": skill.description,
            "instructions": skill.instructions,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def skill_snapshot(skill: Skill) -> dict[str, str]:
    """Serialize skill metadata and full content for reproducible evaluation."""
    return {
        "name": skill.name,
        "version": skill.version,
        "fingerprint": skill_fingerprint(skill),
        "description": skill.description,
        "instructions": skill.instructions,
    }


def _strategy_snapshot(strategy: object) -> dict[str, Any]:
    """Serialize a bounded strategy dataclass without relying on JSON hooks."""
    return asdict(cast("Any", strategy))


def observation_snapshot(view: PlayerView) -> dict[str, Any]:
    """Serialize exactly one controller's authorized observation."""
    seats = {
        player_id: {"seat": seat, "name": name}
        for player_id, seat, name in view.seat_players
    }

    def player_ref(item: tuple[str, str]) -> dict[str, Any]:
        player_id, name = item
        seat = seats.get(player_id, {}).get("seat")
        return {"player_id": player_id, "seat": seat, "name": name}

    return {
        "player_id": view.player_id,
        "public_label": view.own_label,
        "name": view.name,
        "seat_number": view.seat_number,
        "role": view.role.value,
        "role_name": view.role_name,
        "role_description": view.role_description,
        "faction": view.faction.value,
        "lover": (
            {"player_id": view.lover[0], "name": view.lover[1]}
            if view.lover is not None
            else None
        ),
        "alive_players": [player_ref(item) for item in view.alive_players],
        "dead_players": [player_ref(item) for item in view.dead_players],
        "seat_players": [
            {"player_id": player_id, "seat": seat, "name": name}
            for player_id, seat, name in view.seat_players
        ],
        "events": [
            {
                "sequence": event.sequence,
                "day": event.day,
                "phase": event.phase,
                "text": event.text,
                "visibility": event.visibility.value,
                "sender": event.sender,
            }
            for event in view.events
        ],
        "thoughts": [asdict(thought) for thought in view.thoughts],
        "strategy": _strategy_snapshot(view.strategy),
        "skills": [skill_snapshot(skill) for skill in view.skills],
        "day": view.day,
        "phase": view.phase,
        "language": view.language,
        "mechanical_context": view.mechanical_context,
        "game_name": view.game_name,
        "adversary_name": view.adversary_name,
    }


def request_snapshot(request: ActionRequest) -> dict[str, Any]:
    """Serialize the legal action space presented by the judge."""
    return {
        "kind": request.kind.value,
        "prompt": request.prompt,
        "options": [asdict(option) for option in request.options],
        "allow_abstain": request.allow_abstain,
        "requires_text": request.requires_text,
        "returns_private_result": request.returns_private_result,
        "retry_feedback": request.retry_feedback,
    }


def response_snapshot(response: AgentResponse) -> dict[str, Any]:
    """Serialize the accepted action, including private learning signals."""
    return asdict(response)


class TrajectoryRecorder:
    """Collect a resumable private trajectory and append it at match end."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.match_id = uuid.uuid4().hex
        self.stage_sequence = 0
        self.steps: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def advance_stage(self) -> None:
        """Start a new judge checkpoint stage for locally numbered actions."""
        with self._lock:
            self.stage_sequence += 1

    def record(
        self,
        view: PlayerView,
        request: ActionRequest,
        response: AgentResponse,
        *,
        controller: str,
        provider: str | None,
        model: str | None,
        persona: str,
        action_index: int,
        private_result: str = "",
    ) -> None:
        """Append one accepted decision before its game side effects occur."""
        shaping_reward = RETRY_PENALTY * max(response.attempts - 1, 0)
        if response.used_fallback:
            shaping_reward += FALLBACK_PENALTY
        with self._lock:
            if any(
                int(step.get("stage_sequence", -1)) == self.stage_sequence
                and int(step.get("action_index", -1)) == action_index
                for step in self.steps
            ):
                return
            self.steps.append(
                {
                    "stage_sequence": self.stage_sequence,
                    "action_index": action_index,
                    "player_id": view.player_id,
                    "policy": {
                        "controller": controller,
                        "provider": provider,
                        "model": model,
                        "persona": persona,
                    },
                    "observation": observation_snapshot(view),
                    "request": request_snapshot(request),
                    "response": response_snapshot(response),
                    "transition": {"private_result": private_result},
                    "reward": {
                        "shaping": round(shaping_reward, 6),
                        "terminal": 0.0,
                        "total": round(shaping_reward, 6),
                    },
                },
            )

    def checkpoint_value(self) -> dict[str, Any]:
        """Return the private state required to resume without duplicate steps."""
        with self._lock:
            return {
                "match_id": self.match_id,
                "stage_sequence": self.stage_sequence,
                "steps": list(self.steps),
            }

    def restore(self, value: object) -> None:
        """Restore a recorder from a validated private checkpoint payload."""
        if not isinstance(value, dict):
            msg = "Checkpoint training trajectory is malformed"
            raise TypeError(msg)
        match_id = value.get("match_id")
        stage_sequence = value.get("stage_sequence", 0)
        steps = value.get("steps")
        if (
            not isinstance(match_id, str)
            or isinstance(stage_sequence, bool)
            or not isinstance(stage_sequence, int)
            or stage_sequence < 0
            or not isinstance(steps, list)
            or not all(isinstance(step, dict) for step in steps)
        ):
            msg = "Checkpoint training trajectory is malformed"
            raise TypeError(msg)
        with self._lock:
            self.match_id = match_id
            self.stage_sequence = stage_sequence
            self.steps = list(steps)

    def finish(
        self,
        *,
        language: str,
        role_preset: str,
        seed: int | None,
        rules: object,
        winner: Faction | None,
        winning_players: tuple[str, ...],
        prize_shares: tuple[tuple[str, float], ...],
        days: int,
        reason: str,
        duration_seconds: float,
        players: Iterable[PlayerState],
        seat_policies: dict[str, dict[str, str | None]],
        controller_metrics: object,
    ) -> dict[str, Any]:
        """Assign delayed rewards, append one JSONL episode, and return it."""
        player_list = list(players)
        winning_names = set(winning_players)
        prizes = dict(prize_shares)
        terminal_rewards = {
            player.player_id: (
                0.0
                if winner is None
                else (1.0 if player.name in winning_names else -1.0)
                + prizes.get(player.name, 0.0)
            )
            for player in player_list
        }
        with self._lock:
            steps = sorted(
                json.loads(json.dumps(self.steps, ensure_ascii=False)),
                key=lambda step: (
                    int(step["stage_sequence"]),
                    int(step["action_index"]),
                ),
            )
        for sequence, step in enumerate(steps):
            step["step_sequence"] = sequence
        last_step: dict[str, int] = {}
        for index, step in enumerate(steps):
            last_step[str(step["player_id"])] = index
        for player_id, index in last_step.items():
            reward = steps[index]["reward"]
            terminal = terminal_rewards.get(player_id, 0.0)
            reward["terminal"] = round(terminal, 6)
            reward["total"] = round(float(reward["shaping"]) + terminal, 6)

        grouped_steps: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for step in steps:
            grouped_steps[str(step["player_id"])].append(step)
        player_summaries: list[dict[str, Any]] = []
        for player in player_list:
            player_steps = grouped_steps[player.player_id]
            shaping = sum(float(step["reward"]["shaping"]) for step in player_steps)
            skills_used: dict[tuple[str, str, str], dict[str, str]] = {}
            for step in player_steps:
                for skill in step["observation"]["skills"]:
                    key = (skill["name"], skill["version"], skill["fingerprint"])
                    skills_used[key] = skill
            policy = seat_policies.get(player.player_id, {})
            player_summaries.append(
                {
                    "player_id": player.player_id,
                    "name": player.name,
                    "seat_number": player.seat_number,
                    "role": player.role.value,
                    "faction": player.role.faction.value,
                    "alive": player.alive,
                    "winner": player.name in winning_names,
                    "prize_share": prizes.get(player.name, 0.0),
                    "actions": len(player_steps),
                    "attempts": sum(
                        int(step["response"].get("attempts", 1))
                        for step in player_steps
                    ),
                    "fallbacks": sum(
                        bool(step["response"].get("used_fallback", False))
                        for step in player_steps
                    ),
                    "outcome_reward": round(terminal_rewards[player.player_id], 6),
                    "shaping_reward": round(shaping, 6),
                    "return": round(terminal_rewards[player.player_id] + shaping, 6),
                    "policy": policy,
                    "skills_used": sorted(
                        skills_used.values(),
                        key=lambda item: (item["name"], item["version"]),
                    ),
                },
            )

        episode = {
            "schema_version": TRAJECTORY_SCHEMA_VERSION,
            "match_id": self.match_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "game": {
                "language": language,
                "role_preset": role_preset,
                "seed": seed,
                "rules": asdict(cast("Any", rules)),
                "winner": winner.value if winner is not None else None,
                "days": days,
                "reason": reason,
                "duration_seconds": duration_seconds,
                "controller_metrics": asdict(cast("Any", controller_metrics)),
            },
            "reward_spec": {
                "winner": 1.0,
                "loser": -1.0,
                "draw": 0.0,
                "prize_share_bonus": 1.0,
                "retry_penalty": RETRY_PENALTY,
                "fallback_penalty": FALLBACK_PENALTY,
                "terminal_assignment": "last action by each player",
            },
            "players": player_summaries,
            "steps": steps,
        }
        self._append_episode(episode)
        return episode

    def _append_episode(self, episode: dict[str, Any]) -> None:
        """Append one owner-only JSONL record without rewriting prior matches."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with self.path.open(encoding="utf-8") as file:
                for line in file:
                    if not line.strip():
                        continue
                    with_match_id = json.loads(line)
                    if with_match_id.get("match_id") == self.match_id:
                        # A crash may occur after the terminal append but before
                        # the private checkpoint is cleared. Resuming that match
                        # must not count the same episode twice.
                        return
        encoded = (
            json.dumps(
                episode, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@dataclass
class SkillAggregate:
    """Cross-match metrics for one exact skill-content variant."""

    name: str
    version: str
    fingerprint: str
    samples: int = 0
    wins: int = 0
    survivals: int = 0
    actions: int = 0
    fallbacks: int = 0
    return_sum: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Return derived metrics suitable for CLI and machine consumption."""
        samples = max(self.samples, 1)
        return {
            "name": self.name,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "samples": self.samples,
            "win_rate": self.wins / samples,
            "survival_rate": self.survivals / samples,
            "mean_return": self.return_sum / samples,
            "actions": self.actions,
            "fallbacks": self.fallbacks,
        }


@dataclass(frozen=True)
class SkillCandidate:
    """One reward-guided prompt candidate derived from exact trajectory evidence."""

    name: str
    version: str
    fingerprint: str
    description: str
    instructions: str
    source_version: str
    source_fingerprint: str
    samples: int
    win_rate: float
    survival_rate: float
    mean_return: float
    evidence: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Serialize the candidate without exposing trajectory conversations."""
        return asdict(self)


def _load_episodes(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate complete training episodes from one JSONL corpus."""
    episodes: list[dict[str, Any]] = []
    trajectory_path = Path(path)
    with trajectory_path.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                episode = json.loads(line)
            except json.JSONDecodeError as exc:
                msg = f"Malformed trajectory JSON on line {line_number}: {exc.msg}"
                raise ValueError(msg) from exc
            if episode.get("schema_version") != TRAJECTORY_SCHEMA_VERSION:
                msg = f"Unsupported trajectory schema on line {line_number}"
                raise ValueError(msg)
            if not isinstance(episode.get("players"), list):
                msg = f"Malformed trajectory episode on line {line_number}"
                raise TypeError(msg)
            episodes.append(episode)
    if not episodes:
        msg = "Training trajectory contains no completed episodes"
        raise ValueError(msg)
    return episodes


def _variant_instances(
    episodes: list[dict[str, Any]],
    requested_names: set[str] | None,
) -> dict[
    tuple[str, str, str],
    list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
]:
    """Index player episodes by exact skill content identity."""
    variants: dict[
        tuple[str, str, str],
        list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ] = defaultdict(list)
    for episode in episodes:
        for player in episode["players"]:
            if not isinstance(player, dict):
                continue
            for skill in player.get("skills_used", []):
                if not isinstance(skill, dict):
                    continue
                name = str(skill.get("name", ""))
                if requested_names is None:
                    if not name.startswith("role_"):
                        continue
                elif name not in requested_names:
                    continue
                version = str(skill.get("version", ""))
                fingerprint = str(skill.get("fingerprint", ""))
                instructions = skill.get("instructions")
                description = skill.get("description")
                if not all(
                    (
                        name,
                        version,
                        fingerprint,
                        isinstance(instructions, str) and instructions,
                        isinstance(description, str) and description,
                    )
                ):
                    continue
                variants[(name, version, fingerprint)].append((episode, player, skill))
    return variants


def _selected_variants(
    variants: dict[
        tuple[str, str, str],
        list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ],
    *,
    selection: str,
) -> dict[
    str,
    tuple[
        tuple[str, str, str],
        list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ],
]:
    """Choose one exact source variant per skill using evaluation metrics."""
    if selection not in {"mean", "ucb"}:
        msg = "selection must be 'mean' or 'ucb'"
        raise ValueError(msg)
    by_name: dict[
        str,
        list[
            tuple[
                tuple[str, str, str],
                list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
            ]
        ],
    ] = defaultdict(list)
    for key, instances in variants.items():
        by_name[key[0]].append((key, instances))
    selected = {}
    for name, choices in by_name.items():
        total = sum(len(instances) for _, instances in choices)

        def score(
            item: tuple[
                tuple[str, str, str],
                list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
            ],
            total_samples: int = total,
        ) -> tuple[float, int, str, str]:
            """Rank exploitation first, optionally adding bounded exploration."""
            key, instances = item
            samples = len(instances)
            mean_return = (
                sum(float(row[1].get("return", 0.0)) for row in instances) / samples
            )
            selected_score = mean_return
            if selection == "ucb":
                selected_score += math.sqrt(
                    2 * math.log(max(total_samples, 1)) / samples
                )
            return selected_score, samples, key[1], key[2]

        selected[name] = max(
            choices,
            key=score,
        )
    return selected


def _player_steps(
    episode: dict[str, Any],
    player: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return one player's recorded decisions in judge order."""
    player_id = player.get("player_id")
    return [
        step
        for step in episode["steps"]
        if isinstance(step, dict) and step.get("player_id") == player_id
    ]


def _truthful_good_claims_before_votes(
    episode: dict[str, Any],
) -> dict[int, set[str]]:
    """Index truthful public power-role claims visible before each vote.

    The evaluator may use terminal roles because it runs offline on private
    trajectories. Only bounded counters derived from this index enter a
    manifest; hidden identities and verbatim speech never do.
    """
    role_by_player = {
        str(row.get("player_id")): str(row.get("role", ""))
        for row in episode["players"]
        if isinstance(row, dict)
    }
    visible_claims: dict[int, set[str]] = {}
    truthful_claims: set[str] = set()
    for step in episode.get("steps", []):
        if not isinstance(step, dict):
            continue
        request = step.get("request", {})
        response = step.get("response", {})
        player_id = str(step.get("player_id", ""))
        kind = request.get("kind")
        if kind == "speak":
            role = role_by_player.get(player_id, "")
            text = str(response.get("text") or "")
            if role in _HIGH_VALUE_GOOD_ROLES and _claims_role_or_unique_action(
                text,
                role,
            ):
                truthful_claims.add(player_id)
        elif kind == "vote":
            visible_claims[id(step)] = set(truthful_claims)
    return visible_claims


def _normalized_public_text(text: str) -> str:
    """Remove presentation noise before measuring obvious public copying."""
    return "".join(character for character in text if character.isalpha())


def _has_high_public_overlap(
    episode: dict[str, Any],
    current_step: dict[str, Any],
    text: str,
) -> bool:
    """Return whether a speech closely repeats an earlier same-day speech."""
    normalized = _normalized_public_text(text)
    if len(normalized) < 40:
        return False
    day = int(current_step.get("observation", {}).get("day", 0))
    for step in episode.get("steps", []):
        if step is current_step:
            break
        if not isinstance(step, dict):
            continue
        if step.get("request", {}).get("kind") != "speak":
            continue
        if int(step.get("observation", {}).get("day", 0)) != day:
            continue
        earlier = _normalized_public_text(
            str(step.get("response", {}).get("text") or "")
        )
        if len(earlier) < 40:
            continue
        if SequenceMatcher(None, normalized, earlier).ratio() >= 0.42:
            return True
    return False


def _candidate_signals(
    name: str,
    instances: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Extract bounded behavioral signals without copying private model prose."""
    wins = sum(bool(player.get("winner", False)) for _, player, _ in instances)
    survivals = sum(bool(player.get("alive", False)) for _, player, _ in instances)
    return_sum = sum(float(player.get("return", 0.0)) for _, player, _ in instances)
    votes = 0
    abstentions = 0
    repeated_votes = 0
    first_day_claims = 0
    claim_then_abstain = 0
    ranking_claims = 0
    hunter_passes = 0
    teammate_votes = 0
    high_value_kills = 0
    public_role_claims = 0
    concealment_conflicts = 0
    wolf_self_admissions = 0
    wolf_teammate_disclosures = 0
    public_template_uses = 0
    public_speech_overlaps = 0
    votes_against_true_claims = 0
    claim_then_eliminated = 0
    wolf_side_checks = 0
    concealed_wolf_side_checks = 0
    repeated_inspections = 0
    retries = 0
    actions = 0
    own_role_term = {
        "role_seer": "预言家",
        "role_witch": "女巫",
        "role_hunter": "猎人",
        "role_medium": "灵媒",
        "role_bodyguard": "守卫",
        "role_werewolf": "狼人",
    }.get(name)
    for episode, player, _ in instances:
        steps = _player_steps(episode, player)
        truthful_claims_before_votes = _truthful_good_claims_before_votes(episode)
        actions += len(steps)
        retries += sum(
            max(int(step.get("response", {}).get("attempts", 1)) - 1, 0)
            for step in steps
        )
        claim_days: set[int] = set()
        last_words_days: set[int] = set()
        vote_rows: list[tuple[int, str | None]] = []
        inspection_targets: list[str] = []
        pending_wolf_check = False
        role_by_player = {
            str(row.get("player_id")): str(row.get("role", ""))
            for row in episode["players"]
            if isinstance(row, dict)
        }
        faction_by_player = {
            str(row.get("player_id")): str(row.get("faction", ""))
            for row in episode["players"]
            if isinstance(row, dict)
        }
        for step in steps:
            request = step.get("request", {})
            response = step.get("response", {})
            observation = step.get("observation", {})
            kind = request.get("kind")
            day = int(observation.get("day", 0))
            text = str(response.get("text") or "")
            choice = response.get("choice")
            if kind == "speak":
                role_claim = _claims_role_or_unique_action(
                    text,
                    str(player.get("role", "")),
                )
                if role_claim:
                    claim_days.add(day)
                    first_day_claims += day == 1
                    if name == "role_seer":
                        pending_wolf_check = False
                ranking_claims += any(term in text for term in _RANKING_CLAIM_TERMS)
                public_template_uses += any(
                    term in text for term in _PUBLIC_TEMPLATE_TERMS
                )
                public_speech_overlaps += _has_high_public_overlap(
                    episode,
                    step,
                    text,
                )
            if kind in {"speak", "last_words"} and own_role_term is not None:
                own_claim = _claims_role_or_unique_action(
                    text,
                    str(player.get("role", "")),
                )
                public_role_claims += own_claim
                concealment_conflicts += own_claim and any(
                    marker in text
                    for marker in ("不会暴露", "继续隐藏", "不会公开", "保持隐藏")
                )
                if name == "role_werewolf" and kind == "last_words":
                    wolf_self_admissions += own_claim
                    wolf_teammate_disclosures += own_claim and any(
                        marker in text for marker in ("队友", "狼队友", "teammate")
                    )
                if kind == "last_words":
                    last_words_days.add(day)
            if kind == "vote":
                votes += 1
                normalized_choice = str(choice) if choice is not None else None
                vote_rows.append((day, normalized_choice))
                abstentions += choice is None
                if choice is not None and name != "role_werewolf":
                    votes_against_true_claims += str(choice) in (
                        truthful_claims_before_votes.get(id(step), set())
                    )
                if name == "role_werewolf" and choice is not None:
                    teammate_votes += faction_by_player.get(str(choice)) == "werewolf"
            elif kind == "seer_inspect":
                normalized_choice = str(choice) if choice is not None else ""
                repeated_inspections += normalized_choice in inspection_targets
                inspection_targets.append(normalized_choice)
                private_result = str(
                    step.get("transition", {}).get("private_result") or ""
                )
                if (
                    "狼人侧" in private_result
                    or "werewolf side" in private_result.lower()
                ):
                    wolf_side_checks += 1
                    pending_wolf_check = True
            elif kind == "hunter_shoot" and choice is None:
                hunter_passes += 1
            elif kind == "wolf_kill" and choice is not None:
                high_value_kills += role_by_player.get(str(choice)) in (
                    _HIGH_VALUE_GOOD_ROLES
                )
        if name == "role_seer" and pending_wolf_check:
            concealed_wolf_side_checks += 1
        abstain_days = {day for day, choice in vote_rows if choice is None}
        claim_then_abstain += bool(claim_days & abstain_days)
        claim_then_eliminated += bool(claim_days & last_words_days)
        non_null_votes = [choice for _, choice in vote_rows if choice is not None]
        repeated_votes += sum(
            current == previous
            for previous, current in zip(non_null_votes, non_null_votes[1:])
        )
    samples = len(instances)
    return {
        "samples": samples,
        "wins": wins,
        "survivals": survivals,
        "win_rate": wins / samples,
        "survival_rate": survivals / samples,
        "mean_return": return_sum / samples,
        "actions": actions,
        "retries": retries,
        "votes": votes,
        "abstentions": abstentions,
        "repeated_votes": repeated_votes,
        "first_day_claims": first_day_claims,
        "claim_then_abstain": claim_then_abstain,
        "ranking_claims": ranking_claims,
        "hunter_passes": hunter_passes,
        "teammate_votes": teammate_votes,
        "high_value_kills": high_value_kills,
        "public_role_claims": public_role_claims,
        "concealment_conflicts": concealment_conflicts,
        "wolf_self_admissions": wolf_self_admissions,
        "wolf_teammate_disclosures": wolf_teammate_disclosures,
        "public_template_uses": public_template_uses,
        "public_speech_overlaps": public_speech_overlaps,
        "votes_against_true_claims": votes_against_true_claims,
        "claim_then_eliminated": claim_then_eliminated,
        "wolf_side_checks": wolf_side_checks,
        "concealed_wolf_side_checks": concealed_wolf_side_checks,
        "repeated_inspections": repeated_inspections,
    }


def _candidate_rules(name: str, signals: dict[str, Any]) -> tuple[str, ...]:
    """Turn reward and action signals into conservative, reusable policy edits."""
    losing = float(signals["mean_return"]) < 0
    rules: list[str] = []
    if losing and signals["ranking_claims"]:
        rules.append(
            "引用关系链、票型或他人排序前，逐项核对法官公开记录中的座位、轮次、原始顺序与实际票向；"
            "相同目标集合不等于相同排序，夜间死亡也不会自动证明死者的判断。"
        )
    if signals["public_template_uses"] or signals["public_speech_overlaps"]:
        rules.append(
            "策略清单只用于私下检查，不要把“可核验依据、首选备选、反方解释”等提示词原样复述成公开"
            "模板；每轮只引用本局新出现的一至两条具体事实，并明确它们相对上一轮改变了什么判断。"
        )
    if name != "role_werewolf" and signals["votes_against_true_claims"]:
        rules.append(
            "有人公开声明神职且当场无人对跳时，不能仅以“身份无法核验”为由将其放逐；先核对其公开"
            "信息是否与规则和时间线一致，再比较误出真神与暂留待验的终局代价。"
        )
    if name == "role_werewolf":
        rules.append(
            "被放逐后的遗言仍是公开策略：除非规则已经公开身份，绝不能承认狼人身份、确认队友、验证"
            "预言家的隐藏信息或宣布狼队团灭；只使用公开事实留下能保护存活队友的反方叙事。"
        )
        if signals["teammate_votes"]:
            rules.append(
                "队友成为多数票焦点且强保会暴露关系链时，允许一名狼人按其公开逻辑切割；其余队友不要"
                "机械同票或复述同一理由，要优先保留一名关系较浅、能进入终局的狼人。"
            )
        if signals["high_value_kills"]:
            rules.append(
                "夜间袭击优先打断已经公开或高度可信的信息链；同时比较药物、开枪等反制风险，以及目标"
                "死亡后能否支持次日叙事，不能只按角色价值机械下刀。"
            )
        rules.append(
            "共同制造一次关键票型后，下一轮必须在私聊中分配不同的公开立场、怀疑目标和投票角色；"
            "每名狼人独立核对公开事实，避免整队复制同一个事实错误。"
        )
    elif name == "role_seer":
        if signals["first_day_claims"]:
            rules.append(
                "首夜只有村人侧结果、无人对跳且自己尚未进入明确放逐位时，默认继续隐藏；起跳前必须"
                "比较暴露后被刀风险与当前查验对票型的实际区分度。"
            )
        if signals["claim_then_abstain"]:
            rules.append(
                "一旦公开预言家身份，必须给出基于查验和公开记录的首选、备选与当轮票向；不要在投入"
                "身份信用后弃权，否则信息链无法转化为保护或放逐收益。"
            )
        if signals["wolf_side_checks"]:
            rules.append(
                "拿到狼人侧查验后，下一次白天发言默认立即公开身份、目标和真实结果，并把票投向查杀；"
                "只有能说明更高即时收益的极少数局面才继续隐藏，不能用编造的行为理由代替法官查验。"
            )
        if signals["repeated_inspections"]:
            rules.append(
                "不要重复查验同一名仍存活的玩家：法官阵营结果不会因复验增强，复验只会浪费扩展信息面"
                "的夜晚；记录已有结果并把下一验用于未验候选。"
            )
        rules.append(
            "查验只确认阵营显示；他人的女巫、守卫或刀口声明仍是待核验信息，不能与村人侧结果相加成"
            "所谓双重身份确认。"
        )
    elif name == "role_witch":
        if signals["first_day_claims"]:
            rules.append(
                "首夜救人只提高刀口为好人的概率，不足以单独支持首日公开。若选择公开，必须先明确公开"
                "能改变的当前票型、需要保护的信息链和自己的当轮首选，不能只报药物状态后弃权。"
            )
        if signals["claim_then_eliminated"]:
            rules.append(
                "公开女巫后若自己进入放逐焦点，优先完成生存辩护：要求潜在对跳当场给出药物与刀口"
                "时间线，解释自身行动的可验证边界，并明确投向最矛盾者；不要继续围绕被救者制造无关焦点。"
            )
        rules.append(
            "当可信信息角色已经出局、自己又已公开暴露时，毒药窗口价值会快速上升；在存在至少两条独立"
            "公开证据的目标时，明确比较当夜使用与继续保留的胜率代价，而不是默认不用药。"
        )
    elif name == "role_hunter":
        if signals["public_role_claims"]:
            rules.append(
                "普通讨论中不要把私密底牌或“准备继续隐藏”的计划写进公开发言；提交前检查文本是否出现"
                "自己的真实角色名。只有进入明确放逐位、对跳能改变票型或开枪规则必须说明时才公开。"
            )
        if signals["concealment_conflicts"]:
            rules.append(
                "发言内容与私密计划必须一致：若计划是隐藏，就删除所有直接或间接的猎人自报；若决定"
                "公开，则同时说明可核验依据、枪口条件和公开能改变的当轮票型。"
            )
        if signals["hunter_passes"]:
            rules.append(
                "死亡开枪前先计算剩余狼人上限、不开枪后好人的容错轮次，以及目标是否有两条独立证据；"
                "有可靠候选且不开枪会直接丢失追轮次时应开枪，证据仍单一或互相矛盾时才保留不开枪。"
            )
        if signals["abstentions"]:
            rules.append(
                "当唯一公开神职面临放逐且无人对跳时，不要用弃权回避身份判断；必须比较暂留待验与误出"
                "真神的代价，并用实际票型保护更高信息价值的一方。"
            )
        rules.append(
            "公开身份或遗言不能只用于自证；应把枪口条件写成可执行规则，让存活好人知道哪些新信息会"
            "使你改变首选，避免枪威慑与白天票型脱节。"
        )
    elif name == "role_villager":
        if signals["abstentions"]:
            rules.append(
                "最迟从第二个白天开始给出明确首选和备选；信息不足可以降低置信度并保留反方解释，但不"
                "能用连续弃权代替判断，关键轮次投票必须与公开排序一致。"
            )
        if signals["repeated_votes"]:
            rules.append(
                "连续两轮投同一目标前，列出自上轮以来新增的独立证据；若只有旧叙事被重复传播，必须"
                "重新开放至少两个候选，防止锚定效应被狼人利用。"
            )
        rules.append(
            "四人局或错误放逐即可能终局时，先做存活狼人数量约束，再逐个比较候选的历史票向、错误的"
            "受益者和当前投票收益；主动核对前位玩家的事实错误，不能因多人复述就提升其可信度。"
        )
    else:
        rules.append(
            "将终局回报用于纠正可复现的决策偏差：每次关键行动前写出首选、备选、反方解释和会触发"
            "改票的新证据，避免仅凭发言风格或多数共识行动。"
        )
    if signals["retries"]:
        rules.append(
            "形成决策后用简洁、完整的动作字段提交结果，避免冗长推演挤占最终答案；重试不会提供新的"
            "游戏证据，不得借重试改变已无新信息支持的选择。"
        )
    return tuple(dict.fromkeys(rules))


def _merge_candidate_instructions(
    source_instructions: str,
    *,
    version: str,
    new_rules: tuple[str, ...],
) -> str:
    """Retain learned rules while replacing the generated appendix in place."""
    marker = "\n\n【自博弈强化候选 "
    base, separator, generated = source_instructions.partition(marker)
    prior_rules: tuple[str, ...] = ()
    if separator:
        prior_rules = tuple(
            line.removeprefix("- ").strip()
            for line in generated.splitlines()
            if line.startswith("- ") and line.removeprefix("- ").strip()
        )
    rules = tuple(dict.fromkeys((*prior_rules, *new_rules)))
    return (
        base.rstrip()
        + f"\n\n【自博弈强化候选 {version}】"
        + "".join(f"\n- {rule}" for rule in rules)
    )


def _candidate_description(source_description: str) -> str:
    """Append the candidate label exactly once across repeated generations."""
    suffix = "（自博弈强化候选）"
    base = source_description.rstrip()
    while base.endswith(suffix):
        base = base[: -len(suffix)].rstrip()
    return f"{base}{suffix}"


def build_skill_improvement_manifest(
    path: str | Path,
    *,
    skill_names: Iterable[str] | None = None,
    version_prefix: str = "selfplay-v1",
    selection: str = "mean",
) -> dict[str, Any]:
    """Build versioned reward-guided role-skill candidates from trajectories.

    The updater is deliberately offline and auditable. It reads private actions
    to compute bounded counters, but candidate evidence never contains player
    names, private chat, hidden thoughts, or verbatim model responses.
    """
    requested_names = set(skill_names) if skill_names is not None else None
    if requested_names is not None and (
        not requested_names or any(not name for name in requested_names)
    ):
        msg = "skill_names must contain at least one non-empty skill name"
        raise ValueError(msg)
    if not version_prefix.strip():
        msg = "version_prefix must be non-empty"
        raise ValueError(msg)
    episodes = _load_episodes(path)
    if any(not isinstance(episode.get("steps"), list) for episode in episodes):
        msg = "Skill improvement requires full trajectory episodes with steps"
        raise TypeError(msg)
    variants = _variant_instances(episodes, requested_names)
    selected = _selected_variants(variants, selection=selection)
    if requested_names is not None:
        missing = sorted(requested_names - selected.keys())
        if missing:
            msg = f"No trajectory samples found for skills: {', '.join(missing)}"
            raise ValueError(msg)
    if not selected:
        msg = "No role skill samples found in training trajectory"
        raise ValueError(msg)
    match_ids = sorted(str(episode.get("match_id", "")) for episode in episodes)
    digest_payload = json.dumps(
        {
            "matches": match_ids,
            "sources": sorted(key for key, _ in selected.values()),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    corpus_fingerprint = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()[:16]
    version = f"{version_prefix.strip()}-{corpus_fingerprint[:8]}"
    candidates: list[SkillCandidate] = []
    for name in sorted(selected):
        (source_name, source_version, source_fingerprint), parent_instances = selected[
            name
        ]
        source = parent_instances[0][2]
        evidence_instances = [
            instance
            for key, instances in variants.items()
            if key[0] == name
            for instance in instances
        ]
        signals = _candidate_signals(name, evidence_instances)
        rules = _candidate_rules(name, signals)
        instructions = _merge_candidate_instructions(
            str(source["instructions"]),
            version=version,
            new_rules=rules,
        )
        description = _candidate_description(str(source["description"]))
        candidate_skill = Skill(
            name=source_name,
            version=version,
            description=description,
            instructions=instructions,
        )
        evidence = (
            f"samples={signals['samples']}",
            f"wins={signals['wins']}",
            f"survivals={signals['survivals']}",
            f"mean_return={signals['mean_return']:.6f}",
            f"votes={signals['votes']}",
            f"abstentions={signals['abstentions']}",
            f"repeated_votes={signals['repeated_votes']}",
            f"first_day_claims={signals['first_day_claims']}",
            f"claim_then_abstain={signals['claim_then_abstain']}",
            f"teammate_votes={signals['teammate_votes']}",
            f"high_value_kills={signals['high_value_kills']}",
            f"public_role_claims={signals['public_role_claims']}",
            f"concealment_conflicts={signals['concealment_conflicts']}",
            f"wolf_self_admissions={signals['wolf_self_admissions']}",
            f"wolf_teammate_disclosures={signals['wolf_teammate_disclosures']}",
            f"public_template_uses={signals['public_template_uses']}",
            f"public_speech_overlaps={signals['public_speech_overlaps']}",
            f"votes_against_true_claims={signals['votes_against_true_claims']}",
            f"claim_then_eliminated={signals['claim_then_eliminated']}",
            f"wolf_side_checks={signals['wolf_side_checks']}",
            f"concealed_wolf_side_checks={signals['concealed_wolf_side_checks']}",
            f"repeated_inspections={signals['repeated_inspections']}",
            f"retries={signals['retries']}",
        )
        candidates.append(
            SkillCandidate(
                name=source_name,
                version=version,
                fingerprint=skill_fingerprint(candidate_skill),
                description=description,
                instructions=instructions,
                source_version=source_version,
                source_fingerprint=source_fingerprint,
                samples=signals["samples"],
                win_rate=signals["win_rate"],
                survival_rate=signals["survival_rate"],
                mean_return=signals["mean_return"],
                evidence=evidence,
            )
        )
    return {
        "schema_version": SKILL_IMPROVEMENT_SCHEMA_VERSION,
        "trajectory_schema_version": TRAJECTORY_SCHEMA_VERSION,
        "corpus_fingerprint": corpus_fingerprint,
        "episode_count": len(episodes),
        "selection": selection,
        "source_match_ids": match_ids,
        "candidates": [candidate.as_dict() for candidate in candidates],
    }


def apply_skill_improvement_manifest(
    config: GameConfig,
    manifest: dict[str, Any],
) -> GameConfig:
    """Attach every candidate to every seat, where inactive role skills stay dormant."""
    if manifest.get("schema_version") != SKILL_IMPROVEMENT_SCHEMA_VERSION:
        msg = "Unsupported skill improvement manifest schema"
        raise ValueError(msg)
    raw_candidates = manifest.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        msg = "Skill improvement manifest contains no candidates"
        raise ValueError(msg)
    candidates: list[SkillOverrideConfig] = []
    fingerprints: dict[str, str] = {}
    for index, raw in enumerate(raw_candidates):
        if not isinstance(raw, dict):
            msg = f"Malformed skill candidate at index {index}"
            raise TypeError(msg)
        try:
            candidate = SkillOverrideConfig(
                name=str(raw["name"]),
                version=str(raw["version"]),
                description=str(raw["description"]),
                instructions=str(raw["instructions"]),
            )
        except KeyError as exc:
            msg = f"Malformed skill candidate at index {index}: missing {exc.args[0]}"
            raise ValueError(msg) from exc
        if not candidate.name or not candidate.version or not candidate.instructions:
            msg = f"Malformed skill candidate at index {index}"
            raise ValueError(msg)
        if candidate.name in fingerprints:
            msg = f"Duplicate skill candidate {candidate.name!r}"
            raise ValueError(msg)
        actual_fingerprint = skill_fingerprint(
            Skill(
                name=candidate.name,
                version=candidate.version,
                description=candidate.description or "",
                instructions=candidate.instructions,
            )
        )
        expected_fingerprint = str(raw.get("fingerprint", ""))
        if not expected_fingerprint or actual_fingerprint != expected_fingerprint:
            msg = f"Skill candidate {candidate.name!r} fingerprint mismatch"
            raise ValueError(msg)
        fingerprints[candidate.name] = actual_fingerprint
        candidates.append(candidate)
    candidate_names = {candidate.name for candidate in candidates}
    updated_players = []
    for player in config.players:
        retained = tuple(
            override
            for override in player.skill_overrides
            if override.name not in candidate_names
        )
        updated_players.append(
            replace(
                player,
                skill_overrides=(*retained, *candidates),
            )
        )
    updated = replace(
        config,
        players=tuple(updated_players),
    )
    validate_config(updated)
    return updated


def write_skill_improvement_manifest(
    manifest: dict[str, Any],
    path: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write an owner-only, auditable candidate manifest."""
    destination = Path(path)
    if destination.exists() and not overwrite:
        msg = f"Skill improvement manifest already exists: {destination}"
        raise FileExistsError(msg)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        temporary_path.chmod(0o600)
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return destination


def load_skill_leaderboard(
    path: str | Path,
    *,
    skill_name: str | None = None,
    selection: str = "mean",
) -> list[dict[str, Any]]:
    """Aggregate exact variants and rank by exploitation or UCB exploration."""
    if selection not in {"mean", "ucb"}:
        msg = "selection must be 'mean' or 'ucb'"
        raise ValueError(msg)
    aggregates: dict[tuple[str, str, str], SkillAggregate] = {}
    for episode in _load_episodes(path):
        for player in episode.get("players", []):
            if not isinstance(player, dict):
                continue
            for skill in player.get("skills_used", []):
                if not isinstance(skill, dict):
                    continue
                name = str(skill.get("name", ""))
                if skill_name is not None and name != skill_name:
                    continue
                version = str(skill.get("version", ""))
                fingerprint = str(skill.get("fingerprint", ""))
                key = (name, version, fingerprint)
                aggregate = aggregates.setdefault(
                    key,
                    SkillAggregate(name, version, fingerprint),
                )
                aggregate.samples += 1
                aggregate.wins += bool(player.get("winner", False))
                aggregate.survivals += bool(player.get("alive", False))
                aggregate.actions += int(player.get("actions", 0))
                aggregate.fallbacks += int(player.get("fallbacks", 0))
                aggregate.return_sum += float(player.get("return", 0.0))
    rows = [aggregate.as_dict() for aggregate in aggregates.values()]
    totals: dict[str, int] = defaultdict(int)
    for row in rows:
        totals[str(row["name"])] += int(row["samples"])
    for row in rows:
        samples = max(int(row["samples"]), 1)
        total = max(totals[str(row["name"])], 1)
        row["ucb_score"] = float(row["mean_return"]) + math.sqrt(
            2 * math.log(total) / samples,
        )
    primary = "ucb_score" if selection == "ucb" else "mean_return"
    return sorted(
        rows,
        key=lambda item: (
            -float(item[primary]),
            -float(item["win_rate"]),
            -int(item["samples"]),
            str(item["name"]),
            str(item["version"]),
        ),
    )
