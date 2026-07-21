"""Deterministic Werewolf judge and complete day/night game loop."""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .agents import (
    BotController,
    Controller,
    HumanController,
    LLMController,
    OpenAICompatibleClient,
    SafeFallbackController,
    Terminal,
)
from .boundary import InformationBoundary
from .config import validate_config
from .models import (
    ROLE_DESCRIPTIONS,
    ROLE_NAMES,
    ActionKind,
    ActionOption,
    ActionRequest,
    AgentResponse,
    Faction,
    MemoryEvent,
    PlayerState,
    PlayerView,
    Role,
    Thought,
    Visibility,
    localized,
)
from .skills import (
    add_lover_skill,
    add_movie_survival_skill,
    add_preset_skill,
    resolve_player_skills,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .config import GameConfig, PlayerConfig


class DeathCause(str, Enum):
    """Internal cause labels used for role-skill resolution."""

    WOLF = "wolf"
    POISON = "poison"
    VOTE = "vote"
    HUNTER = "hunter"
    DIVINATION = "divination"
    HEARTBREAK = "heartbreak"


@dataclass(frozen=True)
class GameResult:
    """Summary returned after the game ends."""

    winner: Faction | None
    winning_players: tuple[str, ...]
    prize_shares: tuple[tuple[str, float], ...]
    days: int
    survivors: tuple[str, ...]
    reason: str
    duration_seconds: float
    controller_actions: int
    controller_attempts: int
    controller_failures: int
    controller_retries: int
    controller_fallbacks: int
    seat_labels: tuple[tuple[str, str], ...]


@dataclass
class ControllerMetrics:
    """Aggregate LLM-controller reliability counters for one recoverable match."""

    actions: int = 0
    attempts: int = 0
    failures: int = 0
    retries: int = 0
    fallbacks: int = 0


@dataclass(frozen=True)
class FallbackRecord:
    """One controller fallback retained for transparent end-of-game reporting."""

    day: int
    phase: str
    player_name: str
    action_kind: str
    error: str


MOVIE_ROLE_DECKS: dict[str, tuple[Role, ...]] = {
    "movie_basic": (
        *([Role.WEREWOLF] * 2),
        *([Role.VILLAGER] * 6),
        Role.SEER,
        Role.BODYGUARD,
    ),
    "movie_crazy_fox": (
        *([Role.WEREWOLF] * 3),
        *([Role.VILLAGER] * 5),
        Role.SEER,
        Role.MEDIUM,
        Role.BODYGUARD,
        Role.FOX,
    ),
    "movie_prison_break": (
        *([Role.WEREWOLF] * 3),
        *([Role.VILLAGER] * 3),
        Role.SEER,
        Role.MEDIUM,
        Role.BODYGUARD,
        Role.SHARED,
        Role.SHARED,
        Role.MADMAN,
    ),
    "movie_lovers": (
        *([Role.WEREWOLF] * 2),
        *([Role.VILLAGER] * 5),
        Role.SEER,
        Role.MEDIUM,
        Role.BODYGUARD,
        Role.CUPID,
    ),
    "movie_mad_land": (
        Role.WEREWOLF,
        *([Role.MADMAN] * 7),
        Role.SEER,
        Role.BODYGUARD,
    ),
}


def role_deck(player_count: int, preset: str = "classic") -> list[Role]:
    """Return a classic deck or an exact movie-series role composition.

    Six-player games omit the Hunter; larger games include all three special
    good roles. This is a compact no-Sheriff rule set suitable for terminal and
    agent play rather than a tournament-specific ruleset. Movie presets use
    fixed cast sizes and reproduce the supported film variants.
    """
    if preset != "classic":
        try:
            deck = list(MOVIE_ROLE_DECKS[preset])
        except KeyError:
            msg = f"Unknown role preset: {preset}"
            raise ValueError(msg) from None
        if len(deck) != player_count:
            msg = f"role preset {preset!r} requires {len(deck)} players"
            raise ValueError(msg)
        return deck
    if not 6 <= player_count <= 16:
        msg = "role_deck supports 6 to 16 players"
        raise ValueError(msg)
    if player_count <= 8:
        wolf_count = 2
    elif player_count <= 11:
        wolf_count = 3
    elif player_count <= 14:
        wolf_count = 4
    else:
        wolf_count = 5
    special = [Role.SEER, Role.WITCH]
    if player_count >= 7:
        special.append(Role.HUNTER)
    villagers = player_count - wolf_count - len(special)
    return [Role.WEREWOLF] * wolf_count + special + [Role.VILLAGER] * villagers


class Game:
    """Run a complete match while acting as a non-LLM judge."""

    def __init__(
        self,
        config: GameConfig,
        *,
        controllers: dict[str, Controller] | None = None,
        terminal: Terminal | None = None,
        resume_checkpoint: str | Path | None = None,
    ) -> None:
        validate_config(config)
        self.config = config
        self.rng = random.Random(config.seed)  # noqa: S311 - game simulation, not security.
        # Public turn scheduling must not share state with controllers and
        # private night resolution. Otherwise a secret action path can shift
        # the publicly observed discussion order even when nobody dies.
        discussion_seed = (
            None if config.seed is None else f"werewolf-discussion:{config.seed}"
        )
        self._discussion_rng = random.Random(discussion_seed)  # noqa: S311
        self.terminal = terminal or Terminal(
            clear_screen=config.clear_screen,
            transcript_path=config.public_transcript_path,
            reset_transcript=resume_checkpoint is None,
        )
        self._checkpoint_path = (
            Path(resume_checkpoint or config.checkpoint_path)
            if resume_checkpoint or config.checkpoint_path
            else None
        )
        self._resume_day: int | None = None
        self._resume_step: str | None = None
        self._checkpoint_base_payload: dict[str, object] | None = None
        self._action_journal: list[dict[str, object]] = []
        self._action_cursor = 0
        self._started_at = time.monotonic()
        self._controller_metrics = ControllerMetrics()
        self._state_lock = threading.RLock()
        self._fallback_records: list[FallbackRecord] = []
        self._last_nonterminal_snapshot: dict[str, object] | None = None
        self.day = 0
        self.phase = "setup"
        self._antidote_available = True
        self._poison_available = True
        self._last_exiled_id: str | None = None

        seats = self._ordered_seats(resume_checkpoint)
        self._seat_configs = tuple(seats)
        self._human_count = sum(seat.controller == "human" for seat in seats)
        self._controller_kinds: dict[str, str] = {}
        roles = self._roles(seats)
        self.players: list[PlayerState] = []
        for index, (seat, role) in enumerate(zip(seats, roles), start=1):
            player_id = f"p{index}"
            controller = self._controller_for(
                player_id,
                seat.name,
                seat.controller,
                seat.provider,
                seat.persona,
                controllers or {},
            )
            skills = resolve_player_skills(role, list(seat.skills))
            self._controller_kinds[player_id] = seat.controller
            if config.role_preset != "classic":
                skills = add_movie_survival_skill(skills)
            skills = add_preset_skill(skills, config.role_preset)
            self.players.append(
                PlayerState(
                    player_id=player_id,
                    name=seat.name,
                    role=role,
                    controller=controller,
                    skills=skills,
                    seat_number=index,
                ),
            )
        self._by_id = {player.player_id: player for player in self.players}
        self.boundary = InformationBoundary(self.players)
        if resume_checkpoint is not None:
            self._load_checkpoint(Path(resume_checkpoint))

    def _config_signature(self) -> dict[str, object]:
        """Return non-secret configuration fields that must match on resume."""
        return {
            "language": self.config.language,
            "role_preset": self.config.role_preset,
            "rules": asdict(self.config.rules),
            "players": [
                {
                    "name": seat.name,
                    "controller": seat.controller,
                    "provider": seat.provider,
                }
                for seat in self.config.players
            ],
        }

    def _checkpoint_payload(
        self,
        *,
        next_day: int,
        next_step: str,
    ) -> dict[str, object]:
        """Serialize one safe phase boundary without provider credentials."""
        rng_state = self.rng.getstate()
        discussion_rng_state = self._discussion_rng.getstate()
        transcript_path = (
            str(self.terminal.transcript_path.resolve())
            if self.terminal.transcript_path is not None
            else None
        )
        return {
            "version": 1,
            "config_signature": self._config_signature(),
            "next_day": next_day,
            "next_step": next_step,
            "day": self.day,
            "phase": self.phase,
            "antidote_available": self._antidote_available,
            "poison_available": self._poison_available,
            "last_exiled_id": self._last_exiled_id,
            "elapsed_seconds": time.monotonic() - self._started_at,
            "controller_metrics": asdict(self._controller_metrics),
            "fallback_records": [asdict(item) for item in self._fallback_records],
            "last_nonterminal_snapshot": self._last_nonterminal_snapshot,
            "rng_state": [rng_state[0], list(rng_state[1]), rng_state[2]],
            "discussion_rng_state": [
                discussion_rng_state[0],
                list(discussion_rng_state[1]),
                discussion_rng_state[2],
            ],
            "transcript": {
                "path": transcript_path,
                "size": self.terminal.transcript_size(),
            },
            "players": [
                {
                    "player_id": player.player_id,
                    "name": player.name,
                    "role": player.role.value,
                    "alive": player.alive,
                    "lover_id": player.lover_id,
                    "events": [
                        {
                            **asdict(event),
                            "visibility": event.visibility.value,
                        }
                        for event in player.memory.events
                    ],
                    "thoughts": [asdict(thought) for thought in player.memory.thoughts],
                }
                for player in self.players
            ],
            "action_journal": [],
        }

    def _save_checkpoint(self, *, next_day: int, next_step: str) -> None:
        """Start a recoverable phase and clear its completed-action journal."""
        if self._checkpoint_path is None:
            return
        payload = self._checkpoint_payload(next_day=next_day, next_step=next_step)
        self._checkpoint_base_payload = payload
        self._action_journal = []
        self._action_cursor = 0
        self._write_checkpoint(payload)

    def _write_checkpoint(self, payload: dict[str, object]) -> None:
        """Atomically persist a private checkpoint with restrictive permissions."""
        if self._checkpoint_path is None:
            return
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._checkpoint_path.with_name(
            f".{self._checkpoint_path.name}.tmp",
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self._checkpoint_path)
        self._checkpoint_path.chmod(0o600)

    def _load_checkpoint(self, path: Path) -> None:
        """Restore a safe phase boundary and its per-action response journal."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") != 1:
            msg = f"Unsupported checkpoint version in {path}"
            raise ValueError(msg)
        if raw.get("config_signature") != self._config_signature():
            msg = "Checkpoint does not match the supplied game configuration"
            raise ValueError(msg)
        player_data = raw.get("players")
        if not isinstance(player_data, list) or len(player_data) != len(self.players):
            msg = "Checkpoint player list is malformed"
            raise ValueError(msg)
        max_sequence = 0
        for index, (player, saved) in enumerate(
            zip(self.players, player_data),
        ):
            if not isinstance(saved, dict):
                msg = "Checkpoint player entry is malformed"
                raise TypeError(msg)
            if (
                saved.get("player_id") != player.player_id
                or saved.get("name") != player.name
            ):
                msg = "Checkpoint player order does not match the configuration"
                raise ValueError(msg)
            player.role = Role(str(saved["role"]))
            player.alive = bool(saved["alive"])
            player.lover_id = saved.get("lover_id")
            skills = resolve_player_skills(
                player.role,
                list(self._seat_configs[index].skills),
            )
            if self.config.role_preset != "classic":
                skills = add_movie_survival_skill(skills)
            if player.lover_id:
                skills = add_lover_skill(skills)
            player.skills = skills
            player.memory.events = [
                MemoryEvent(
                    sequence=int(event["sequence"]),
                    day=int(event["day"]),
                    phase=str(event["phase"]),
                    text=str(event["text"]),
                    visibility=Visibility(str(event["visibility"])),
                    sender=event.get("sender"),
                )
                for event in saved.get("events", [])
            ]
            player.memory.thoughts = [
                Thought(
                    day=int(thought["day"]),
                    phase=str(thought["phase"]),
                    text=str(thought["text"]),
                )
                for thought in saved.get("thoughts", [])
            ]
            max_sequence = max(
                [max_sequence, *(event.sequence for event in player.memory.events)],
            )
        self.boundary.continue_after(max_sequence)
        self.day = int(raw["day"])
        self.phase = str(raw["phase"])
        self._antidote_available = bool(raw["antidote_available"])
        self._poison_available = bool(raw["poison_available"])
        self._last_exiled_id = raw.get("last_exiled_id")
        self._started_at = time.monotonic() - float(raw.get("elapsed_seconds", 0.0))
        metrics = raw.get("controller_metrics", {})
        if not isinstance(metrics, dict):
            msg = "Checkpoint controller metrics are malformed"
            raise TypeError(msg)
        self._controller_metrics = ControllerMetrics(
            actions=int(metrics.get("actions", 0)),
            attempts=int(metrics.get("attempts", 0)),
            failures=int(metrics.get("failures", 0)),
            retries=int(metrics.get("retries", 0)),
            fallbacks=int(metrics.get("fallbacks", 0)),
        )
        fallback_records = raw.get("fallback_records", [])
        if not isinstance(fallback_records, list) or not all(
            isinstance(item, dict) for item in fallback_records
        ):
            msg = "Checkpoint fallback records are malformed"
            raise ValueError(msg)
        self._fallback_records = [
            FallbackRecord(
                day=int(item["day"]),
                phase=str(item["phase"]),
                player_name=str(item["player_name"]),
                action_kind=str(item["action_kind"]),
                error=str(item["error"]),
            )
            for item in fallback_records
        ]
        snapshot = raw.get("last_nonterminal_snapshot")
        self._last_nonterminal_snapshot = (
            snapshot if isinstance(snapshot, dict) else None
        )
        rng_state = raw["rng_state"]
        self.rng.setstate((int(rng_state[0]), tuple(rng_state[1]), rng_state[2]))
        # Version-1 checkpoints written before discussion RNG isolation do not
        # contain this field. Starting from the saved main RNG state preserves
        # their next discussion draw while isolating all later scheduling.
        discussion_rng_state = raw.get("discussion_rng_state", rng_state)
        self._discussion_rng.setstate(
            (
                int(discussion_rng_state[0]),
                tuple(discussion_rng_state[1]),
                discussion_rng_state[2],
            ),
        )
        transcript = raw.get("transcript", {})
        expected_path = transcript.get("path")
        actual_path = (
            str(self.terminal.transcript_path.resolve())
            if self.terminal.transcript_path is not None
            else None
        )
        if expected_path != actual_path:
            msg = "Checkpoint transcript path does not match the supplied configuration"
            raise ValueError(msg)
        self.terminal.truncate_transcript(transcript.get("size"))
        self._resume_day = int(raw["next_day"])
        self._resume_step = str(raw["next_step"])
        self._action_journal = list(raw.get("action_journal", []))
        self._action_cursor = 0
        self._checkpoint_base_payload = raw
        if self.config.spectator_progress:
            self.terminal.progress(
                self._t(
                    f"已从恢复点加载：第 {self._resume_day} 天，下一阶段 {self._resume_step}。",
                    f"Checkpoint restored: day {self._resume_day}, next phase {self._resume_step}.",
                ),
            )

    def _clear_checkpoint(self) -> None:
        """Remove a checkpoint after the match reaches a terminal result."""
        if self._checkpoint_path is not None:
            self._checkpoint_path.unlink(missing_ok=True)

    def _ordered_seats(
        self,
        resume_checkpoint: str | Path | None,
    ) -> list[PlayerConfig]:
        """Randomize fresh seats or restore their exact checkpoint ordering."""
        seats = list(self.config.players)
        if resume_checkpoint is not None:
            raw = json.loads(Path(resume_checkpoint).read_text(encoding="utf-8"))
            saved_players = raw.get("players", [])
            names = [
                saved.get("name") for saved in saved_players if isinstance(saved, dict)
            ]
            by_name = {seat.name: seat for seat in seats}
            if len(names) != len(seats) or set(names) != by_name.keys():
                msg = "Checkpoint seat order does not match the supplied players"
                raise ValueError(msg)
            return [by_name[str(name)] for name in names]
        if self.config.rules.randomize_seating:
            self.rng.shuffle(seats)
        return seats

    def _roles(self, seats: list[PlayerConfig]) -> list[Role]:
        fixed = [player.fixed_role for player in seats]
        if all(role is not None for role in fixed):
            roles = [role for role in fixed if role is not None]
            wolves = sum(role is Role.WEREWOLF for role in roles)
            if wolves < 1 or wolves >= len(roles):
                msg = "A fixed role set must contain both factions"
                raise ValueError(msg)
            return roles
        roles = role_deck(len(self.config.players), self.config.role_preset)
        self.rng.shuffle(roles)
        return roles

    def _controller_for(
        self,
        player_id: str,
        name: str,
        kind: str,
        provider_name: str | None,
        persona: str,
        overrides: dict[str, Controller],
    ) -> Controller:
        if name in overrides:
            return overrides[name]
        if player_id in overrides:
            return overrides[player_id]
        if kind == "human":
            return HumanController(
                self.terminal,
                require_handoff=self._human_count > 1,
                ask_strategy_note=self.config.human_strategy_notes,
                confirm_critical_actions=self.config.confirm_critical_actions,
            )
        if kind == "bot":
            return BotController(self.rng)
        if not provider_name:
            msg = f"LLM player {name!r} has no provider"
            raise ValueError(msg)
        provider = self.config.providers[provider_name]
        return LLMController(
            OpenAICompatibleClient(provider),
            persona=persona,
            context_char_limit=self.config.context_char_limit,
        )

    def run(self) -> GameResult:
        """Run until one faction wins or the configured day cap is reached."""
        self._show_preflight_notices()
        if self._resume_step is None:
            next_day = 0
            next_step = "setup"
            self._save_checkpoint(next_day=next_day, next_step=next_step)
        else:
            next_day = self._resume_day if self._resume_day is not None else 1
            next_step = self._resume_step
        if next_step == "setup":
            self.day = 0
            self._setup()
            self._record_nonterminal_snapshot("setup")
            next_day = 1
            next_step = "night"
            self._save_checkpoint(next_day=next_day, next_step=next_step)
        for day in range(next_day, self.config.rules.max_days + 1):
            self.day = day
            if next_step == "night":
                self._night()
                winner = self._winner()
                if winner is not None:
                    return self._finish(winner, "night_resolution")
                self._record_nonterminal_snapshot("night_resolution")
                next_step = "daytime"
                self._save_checkpoint(next_day=day, next_step=next_step)
            if next_step == "daytime":
                self._daytime()
                winner = self._winner()
                if winner is not None:
                    return self._finish(winner, "day_vote")
                self._record_nonterminal_snapshot("day_vote")
                next_step = "night"
                self._save_checkpoint(next_day=day + 1, next_step=next_step)
        return self._finish(None, "max_days")

    def _show_preflight_notices(self) -> None:
        """Warn about configurations that made live testing opaque or unrecoverable."""
        if not any(kind == "llm" for kind in self._controller_kinds.values()):
            return
        notices: list[str] = []
        if not self.config.spectator_progress:
            notices.append(
                self._t(
                    "未开启安全进度；模型调用期间终端可能长时间无输出。",
                    "Safe progress is disabled; the terminal may be silent during model calls.",
                ),
            )
        if not self.config.strict_controllers:
            notices.append(
                self._t(
                    "已允许系统安全后备；本局若发生降级将不计为完整 LLM 对局。",
                    "System safe fallback is enabled; any degraded action makes this an incomplete LLM match.",
                ),
            )
        if self._checkpoint_path is None:
            notices.append(
                self._t(
                    "未配置私密恢复点；中止后无法继续当前对局。",
                    "No private checkpoint is configured; an interrupted match cannot be resumed.",
                ),
            )
        providers = [
            self.config.providers[seat.provider]
            for seat in self._seat_configs
            if seat.controller == "llm" and seat.provider in self.config.providers
        ]
        if any(provider.reasoning_effort == "xhigh" for provider in providers):
            notices.append(
                self._t(
                    "检测到 xhigh 推理强度；实时对局延迟可能显著增加，通常建议使用 high。",
                    "xhigh reasoning is configured; live-game latency may be high, and high is usually preferable.",
                ),
            )
        if any(provider.max_tokens > 5000 for provider in providers):
            notices.append(
                self._t(
                    "检测到单次输出上限超过 5000 token；狼人杀动作通常无需如此大的预算。",
                    "A per-action output limit above 5000 tokens was detected; Werewolf actions rarely need that budget.",
                ),
            )
        label = self._t("提示", "Notice")
        for notice in notices:
            self.terminal.notice(notice, label=label)

    def _setup(self) -> None:
        self.phase = "setup"
        seats = "、".join(self._player_label(player) for player in self.players)
        composition = Counter(player.role for player in self.players)
        role_names = localized(ROLE_NAMES, self.config.language)
        role_summary = "，".join(
            f"{role_names[role]}×{count}" for role, count in composition.items()
        )
        death_rule = (
            self._t("死亡翻牌", "roles are revealed on death")
            if self.config.rules.reveal_roles_on_death
            else self._t("死亡不翻牌", "roles stay hidden on death")
        )
        self._announce(
            self._t(
                f"游戏开始。玩家：{seats}。本局身份配置：{role_summary}。{death_rule}。",
                f"Game begins. Players: {seats}. Role deck: {role_summary}. {death_rule}.",
            ),
        )
        wolf_ids = self._wolf_ids(alive_only=False)
        wolf_names = "、".join(
            self._player_label(self._by_id[player_id]) for player_id in wolf_ids
        )
        for player in self.players:
            role_name = role_names[player.role]
            description = localized(ROLE_DESCRIPTIONS, self.config.language)[
                player.role
            ]
            self.boundary.private(
                day=0,
                phase=self.phase,
                recipient=player.player_id,
                text=self._t(
                    f"你的身份是【{role_name}】。{description}",
                    f"Your role is [{role_name}]. {description}",
                ),
            )
        self.boundary.werewolves(
            day=0,
            phase=self.phase,
            recipients=wolf_ids,
            text=self._t(
                f"你的狼人队友名单：{wolf_names}。",
                f"Werewolf roster: {wolf_names}.",
            ),
        )
        self._setup_shared_players()
        self._setup_lovers()
        # Reveal roles one human at a time; LLMs learn through isolated memory.
        for player in self.players:
            if isinstance(player.controller, HumanController):
                self.terminal.private_turn(self._view(player))
                if self.config.clear_screen:
                    with suppress(EOFError):
                        input(
                            self._t(
                                "记住身份后按回车交接终端……"
                                if self._human_count > 1
                                else "记住身份后按回车开始游戏……",
                                "Memorize your role, then pass the terminal..."
                                if self._human_count > 1
                                else "Memorize your role, then press Enter to begin...",
                            ),
                        )
                    self.terminal.clear()

    def _setup_shared_players(self) -> None:
        """Reveal the two Shared Players only to each other."""
        shared = [player for player in self.players if player.role is Role.SHARED]
        if not shared:
            return
        if len(shared) != 2:
            msg = "A game must contain exactly two Shared Players"
            raise ValueError(msg)
        first, second = shared
        self.boundary.private(
            day=0,
            phase=self.phase,
            recipient=first.player_id,
            text=self._t(
                f"另一名共有者是 {self._player_label(second)}。",
                f"The other Shared Player is {self._player_label(second)}.",
            ),
        )
        self.boundary.private(
            day=0,
            phase=self.phase,
            recipient=second.player_id,
            text=self._t(
                f"另一名共有者是 {self._player_label(first)}。",
                f"The other Shared Player is {self._player_label(first)}.",
            ),
        )

    def _setup_lovers(self) -> None:
        """Let Cupid select two distinct players and install the Lover subrole."""
        cupid = next(
            (player for player in self.players if player.role is Role.CUPID), None
        )
        if cupid is None:
            return
        first_response = self._act(
            cupid,
            ActionRequest(
                ActionKind.CUPID_LINK,
                self._t(
                    "选择第一名恋人（丘比特可以选择自己）。",
                    "Choose the first Lover (Cupid may choose themself).",
                ),
                self._options(self.players),
            ),
        )
        first_id = first_response.choice
        # Non-abstaining requests normally receive a legal fallback; keep the
        # invariant explicit in case a custom controller adapter is introduced.
        if first_id is None:
            msg = "Cupid did not choose the first Lover"
            raise RuntimeError(msg)
        second_candidates = [
            player for player in self.players if player.player_id != first_id
        ]
        second_response = self._act(
            cupid,
            ActionRequest(
                ActionKind.CUPID_LINK,
                self._t("选择第二名恋人。", "Choose the second Lover."),
                self._options(second_candidates),
            ),
        )
        second_id = second_response.choice
        if second_id is None:
            msg = "Cupid did not choose the second Lover"
            raise RuntimeError(msg)
        first = self._by_id[first_id]
        second = self._by_id[second_id]
        first.lover_id = second.player_id
        second.lover_id = first.player_id
        first.skills = add_lover_skill(first.skills)
        second.skills = add_lover_skill(second.skills)
        lover_names = self._t(
            f"{self._player_label(first)}、{self._player_label(second)}",
            f"{self._player_label(first)} and {self._player_label(second)}",
        )
        self.boundary.private(
            day=0,
            phase=self.phase,
            recipient=cupid.player_id,
            text=self._t(
                f"你指定的恋人是：{lover_names}。",
                f"You linked these Lovers: {lover_names}.",
            ),
        )
        self.boundary.lovers(
            day=0,
            phase=self.phase,
            recipients=(first.player_id, second.player_id),
            text=self._t(
                f"恋人关系成立：{lover_names}。你们保留原身份；一人死亡，另一人立即殉情。",
                f"Lover link formed: {lover_names}. You keep your original roles; if one dies, the other immediately dies of heartbreak.",
            ),
        )

    def _night(self) -> None:
        self.phase = "night"
        self._announce(
            self._t(
                f"第 {self.day} 夜，天黑请闭眼。",
                f"Night {self.day}. Everyone close your eyes.",
            ),
        )
        self._medium_turn()
        self._lover_turn()
        victim = self._werewolf_turn()
        protected = self._bodyguard_turn()
        divined = self._seer_turn()
        saved, poisoned = self._witch_turn(victim)
        deaths: dict[str, set[DeathCause]] = {}
        if (
            victim
            and not saved
            and victim != protected
            and self._by_id[victim].role is not Role.FOX
        ):
            deaths.setdefault(victim, set()).add(DeathCause.WOLF)
        if divined:
            deaths.setdefault(divined, set()).add(DeathCause.DIVINATION)
        if poisoned:
            deaths.setdefault(poisoned, set()).add(DeathCause.POISON)
        if deaths:
            names = "、".join(
                self._player_label(self._by_id[player_id]) for player_id in deaths
            )
            self._apply_deaths(
                deaths,
                self._t(
                    f"天亮了，昨夜死亡：{names}。",
                    f"Dawn breaks. Last night's deaths: {names}.",
                ),
            )
        else:
            self._announce(
                self._t(
                    "天亮了，昨夜是平安夜。",
                    "Dawn breaks. Nobody died last night.",
                ),
            )

    def _medium_turn(self) -> None:
        """Tell the living Medium how the previous exile appears to divination."""
        medium = next(
            (player for player in self._alive() if player.role is Role.MEDIUM),
            None,
        )
        if medium is None or self._last_exiled_id is None:
            return
        target = self._by_id[self._last_exiled_id]
        alignment = (
            self._t("狼人侧", "werewolf-side")
            if target.role.appears_werewolf
            else self._t("村人侧", "village-side")
        )
        self.boundary.private(
            day=self.day,
            phase=self.phase,
            recipient=medium.player_id,
            text=self._t(
                f"灵媒结果：昨日被放逐的 {self._player_label(target)} 显示为【{alignment}】。",
                f"Medium result: yesterday's exile {self._player_label(target)} appears {alignment}.",
            ),
        )

    def _lover_turn(self) -> None:
        """Allow a living Lover pair one private message each night."""
        pair = [player for player in self._alive() if player.lover_id is not None]
        if len(pair) != 2:
            return
        recipients = tuple(player.player_id for player in pair)
        for lover in pair:
            response = self._act(
                lover,
                ActionRequest(
                    ActionKind.LOVER_CHAT,
                    self._t(
                        "请给恋人发送一条私密消息。",
                        "Send one private message to your Lover.",
                    ),
                ),
            )
            if response.text.strip():
                self.boundary.lovers(
                    day=self.day,
                    phase=self.phase,
                    recipients=recipients,
                    sender=lover.name,
                    text=f"{self._player_label(lover)}：{response.text.strip()}",
                )

    def _bodyguard_turn(self) -> str | None:
        """Return the player protected from the current night's wolf attack."""
        bodyguard = next(
            (player for player in self._alive() if player.role is Role.BODYGUARD),
            None,
        )
        if bodyguard is None:
            return None
        candidates = [player for player in self._alive() if player is not bodyguard]
        response = self._act(
            bodyguard,
            ActionRequest(
                ActionKind.BODYGUARD_PROTECT,
                self._t(
                    "选择今晚要保护的一名其他玩家。",
                    "Choose one other player to protect tonight.",
                ),
                self._options(candidates),
            ),
        )
        return response.choice

    def _werewolf_turn(self) -> str | None:
        wolves = [
            self._by_id[player_id] for player_id in self._wolf_ids(alive_only=True)
        ]
        if not wolves:
            return None
        recipients = [player.player_id for player in wolves]
        for _ in range(self.config.rules.wolf_chat_rounds):
            for wolf in wolves:
                response = self._act(
                    wolf,
                    ActionRequest(
                        ActionKind.TEAM_CHAT,
                        self._t(
                            "请给狼人队友发送一条私密消息。",
                            "Send one private message to your werewolf team.",
                        ),
                    ),
                )
                if response.text.strip():
                    self.boundary.werewolves(
                        day=self.day,
                        phase=self.phase,
                        recipients=recipients,
                        sender=wolf.name,
                        text=f"{self._player_label(wolf)}：{response.text.strip()}",
                    )
        candidates = [
            player for player in self._alive() if player.role is not Role.WEREWOLF
        ]
        options = self._options(candidates)
        votes: list[str] = []
        for wolf in wolves:
            response = self._act(
                wolf,
                ActionRequest(
                    ActionKind.WOLF_KILL,
                    self._t(
                        "选择今晚要袭击的玩家。",
                        "Choose tonight's attack target.",
                    ),
                    options,
                    allow_abstain=True,
                ),
            )
            if response.choice:
                votes.append(response.choice)
        target = self._plurality(votes)
        target_name = (
            self._player_label(self._by_id[target])
            if target
            else self._t("无人", "nobody")
        )
        self.boundary.werewolves(
            day=self.day,
            phase=self.phase,
            recipients=recipients,
            text=self._t(
                f"狼队最终袭击目标：{target_name}。",
                f"Final attack target: {target_name}.",
            ),
        )
        return target

    def _seer_turn(self) -> str | None:
        """Resolve inspection and return a Fox killed by divination, if any."""
        seer = next(
            (player for player in self._alive() if player.role is Role.SEER),
            None,
        )
        if not seer:
            return None
        candidates = [player for player in self._alive() if player is not seer]
        response = self._act(
            seer,
            ActionRequest(
                ActionKind.SEER_INSPECT,
                self._t("选择今晚要查验的玩家。", "Choose one player to inspect."),
                self._options(candidates),
            ),
        )
        if response.choice:
            target = self._by_id[response.choice]
            faction = (
                self._t("狼人侧", "werewolf-side")
                if target.role.appears_werewolf
                else self._t("村人侧", "village-side")
            )
            self.boundary.private(
                day=self.day,
                phase=self.phase,
                recipient=seer.player_id,
                text=self._t(
                    f"查验结果：{self._player_label(target)} 属于【{faction}】。",
                    f"Inspection: {self._player_label(target)} belongs to the {faction}.",
                ),
            )
            if target.role is Role.FOX:
                return target.player_id
        return None

    def _witch_turn(self, victim: str | None) -> tuple[bool, str | None]:
        witch = next(
            (player for player in self._alive() if player.role is Role.WITCH),
            None,
        )
        if not witch:
            return False, None
        victim_name = (
            self._player_label(self._by_id[victim])
            if victim
            else self._t("无人", "nobody")
        )
        self.boundary.private(
            day=self.day,
            phase=self.phase,
            recipient=witch.player_id,
            text=self._t(
                f"今晚狼队的袭击目标：{victim_name}。",
                f"Tonight's attack target: {victim_name}.",
            ),
        )
        can_save = bool(
            victim
            and self._antidote_available
            and (victim != witch.player_id or self.config.rules.witch_can_self_save),
        )
        saved = False
        if can_save:
            response = self._act(
                witch,
                ActionRequest(
                    ActionKind.WITCH_SAVE,
                    self._t(
                        f"是否使用解药救下 {victim_name}？",
                        f"Use the antidote to save {victim_name}?",
                    ),
                    (ActionOption("save", self._t("使用解药", "Use antidote")),),
                    allow_abstain=True,
                ),
            )
            saved = response.choice == "save"
            if saved:
                self._antidote_available = False
        may_poison = self._poison_available and (
            not saved or self.config.rules.witch_can_use_two_potions_same_night
        )
        poisoned: str | None = None
        if may_poison:
            candidates = [player for player in self._alive() if player is not witch]
            response = self._act(
                witch,
                ActionRequest(
                    ActionKind.WITCH_POISON,
                    self._t(
                        "是否使用毒药？可选择一名玩家或不使用。",
                        "Use poison on a player, or abstain.",
                    ),
                    self._options(candidates),
                    allow_abstain=True,
                ),
            )
            poisoned = response.choice
            if poisoned:
                self._poison_available = False
        return saved, poisoned

    def _daytime(self) -> None:
        self.phase = "discussion"
        speakers = self._discussion_order()
        start_name = (
            self._player_label(speakers[0]) if speakers else self._t("无人", "nobody")
        )
        self._announce(
            (
                self._t(
                    f"第 {self.day} 天，开始公开讨论。本日随机从 {start_name} 起按座位顺序发言。",
                    f"Day {self.day}. Public discussion randomly begins with {start_name} and proceeds in seat order.",
                )
                if self.config.rules.randomize_discussion_start
                else self._t(
                    f"第 {self.day} 天，开始公开讨论。本日从 {start_name} 起按固定座位顺序发言。",
                    f"Day {self.day}. Public discussion begins with {start_name} in fixed seat order.",
                )
            ),
        )
        for player in speakers:
            response = self._act(
                player,
                ActionRequest(
                    ActionKind.SPEAK,
                    self._t("请发表本轮公开发言。", "Give your public statement."),
                ),
            )
            speech = response.text.strip() or self._t(
                "（保持沉默）",
                "(remains silent)",
            )
            self._say(player, speech, fallback=response.used_fallback)
        self.phase = "vote"
        votes = self._collect_votes(None)
        leaders = self._vote_leaders(votes)
        if not leaders:
            self._announce(
                self._t(
                    "本轮无人获得有效票，白天无人出局。",
                    "No valid votes; nobody is eliminated.",
                ),
            )
            return
        if len(leaders) > 1:
            names = "、".join(
                self._player_label(self._by_id[player_id]) for player_id in leaders
            )
            self._announce(
                self._t(
                    f"出现平票：{names}。平票玩家依次辩解。",
                    f"Tie: {names}. Tied players may defend themselves.",
                ),
            )
            for player_id in leaders:
                player = self._by_id[player_id]
                response = self._act(
                    player,
                    ActionRequest(
                        ActionKind.SPEAK,
                        self._t(
                            "你进入平票，请发表辩解。",
                            "You are tied; give a defense.",
                        ),
                    ),
                )
                self._say(
                    player,
                    response.text.strip() or self._t("（放弃辩解）", "(no defense)"),
                    fallback=response.used_fallback,
                )
            votes = self._collect_votes(set(leaders), runoff=True)
            leaders = self._vote_leaders(votes)
            if len(leaders) != 1:
                self._announce(
                    self._t(
                        "再次平票，本日无人出局。",
                        "The runoff is tied; nobody is eliminated.",
                    ),
                )
                return
        target = leaders[0]
        target_name = self._player_label(self._by_id[target])
        # The Medium reports only the player directly exiled by the vote; a
        # Lover who follows by heartbreak was not the day's exile.
        self._last_exiled_id = target
        self._apply_deaths(
            {target: {DeathCause.VOTE}},
            self._t(
                f"投票结束，{target_name} 被放逐。",
                f"Voting ends. {target_name} is eliminated.",
            ),
        )

    def _discussion_order(self) -> list[PlayerState]:
        """Rotate living seats using randomness isolated from private actions."""
        alive = list(self._alive())
        if len(alive) < 2 or not self.config.rules.randomize_discussion_start:
            return alive
        start = self._discussion_rng.randrange(len(alive))
        return [*alive[start:], *alive[:start]]

    def _collect_votes(
        self,
        candidate_ids: set[str] | None,
        *,
        runoff: bool = False,
    ) -> dict[str, str | None]:
        votes: dict[str, str | None] = {}
        fallback_voters: set[str] = set()
        alive = list(self._alive())
        actions: list[tuple[PlayerState, ActionRequest]] = []
        for voter in alive:
            candidates = [
                player
                for player in alive
                if (candidate_ids is None or player.player_id in candidate_ids)
                and (self.config.rules.allow_self_vote or player is not voter)
            ]
            actions.append(
                (
                    voter,
                    ActionRequest(
                        ActionKind.VOTE,
                        self._t(
                            "平票重投：请选择放逐对象。"
                            if runoff
                            else "请选择今天要放逐的玩家。",
                            "Runoff: choose whom to eliminate."
                            if runoff
                            else "Choose whom to eliminate today.",
                        ),
                        self._options(candidates),
                        allow_abstain=True,
                    ),
                ),
            )
        responses = self._act_independent(actions)
        for (voter, _), response in zip(actions, responses):
            votes[voter.player_id] = response.choice
            if response.used_fallback:
                fallback_voters.add(voter.player_id)
        vote_text = "；".join(
            f"{self._player_label(self._by_id[voter])}{self._t('（后备）', ' (fallback)') if voter in fallback_voters else ''}"
            f"→{self._player_label(self._by_id[target]) if target else self._t('弃权', 'abstain')}"
            for voter, target in votes.items()
        )
        self._announce(
            self._t(f"公开投票结果：{vote_text}。", f"Public votes: {vote_text}."),
        )
        return votes

    @staticmethod
    def _vote_leaders(votes: dict[str, str | None]) -> list[str]:
        counts = Counter(target for target in votes.values() if target)
        if not counts:
            return []
        highest = max(counts.values())
        return [target for target, count in counts.items() if count == highest]

    def _apply_deaths(
        self,
        deaths: dict[str, set[DeathCause]],
        announcement: str,
    ) -> None:
        """Resolve simultaneous deaths, Lover heartbreak, and Hunter chains."""
        newly_dead: list[tuple[PlayerState, set[DeathCause]]] = []
        for player_id, causes in deaths.items():
            player = self._by_id[player_id]
            if player.alive:
                player.alive = False
                newly_dead.append((player, causes))
        if not newly_dead:
            return
        self._announce(self._with_role_reveal(announcement, newly_dead))
        queue = list(newly_dead)
        while queue:
            player, causes = queue.pop(0)
            partner = self._by_id.get(player.lover_id) if player.lover_id else None
            if partner is not None and partner.alive:
                partner.alive = False
                heartbreak_causes = {DeathCause.HEARTBREAK}
                self._announce(
                    self._with_role_reveal(
                        self._t(
                            f"{self._player_label(player)} 死亡，恋人 {self._player_label(partner)} 随之殉情。",
                            f"{self._player_label(player)} died; their Lover {self._player_label(partner)} dies of heartbreak.",
                        ),
                        [(partner, heartbreak_causes)],
                    ),
                )
                queue.append((partner, heartbreak_causes))
            if self._allows_last_words(causes):
                response = self._act(
                    player,
                    ActionRequest(
                        ActionKind.LAST_WORDS,
                        self._t(
                            "你已死亡，请留下公开遗言。",
                            "You have died. Give public final words.",
                        ),
                    ),
                )
                if response.text.strip():
                    self._say(
                        player,
                        response.text.strip(),
                        fallback=response.used_fallback,
                    )
            if player.role is not Role.HUNTER or DeathCause.POISON in causes:
                continue
            candidates = list(self._alive())
            response = self._act(
                player,
                ActionRequest(
                    ActionKind.HUNTER_SHOOT,
                    self._t(
                        "你可以发动猎人技能带走一名玩家，也可以不开枪。",
                        "You may shoot one living player, or abstain.",
                    ),
                    self._options(candidates),
                    allow_abstain=True,
                ),
            )
            if response.choice:
                victim = self._by_id[response.choice]
                if victim.alive:
                    victim.alive = False
                    self._announce(
                        self._with_role_reveal(
                            self._t(
                                f"{self._player_label(player)} 发动猎人技能，{self._player_label(victim)} 被带走。",
                                f"{self._player_label(player)} uses the Hunter skill and takes down {self._player_label(victim)}.",
                            ),
                            [(victim, {DeathCause.HUNTER})],
                        ),
                    )
                    queue.append((victim, {DeathCause.HUNTER}))

    def _with_role_reveal(
        self,
        announcement: str,
        deaths: list[tuple[PlayerState, set[DeathCause]]],
    ) -> str:
        """Append role reveals for exactly the newly dead players when enabled."""
        if not self.config.rules.reveal_roles_on_death:
            return announcement
        role_names = localized(ROLE_NAMES, self.config.language)
        reveal = "；".join(
            f"{self._player_label(player)}={role_names[player.role]}"
            for player, _ in deaths
        )
        return announcement + self._t(f" 身份公开：{reveal}。", f" Roles: {reveal}.")

    def _allows_last_words(self, causes: set[DeathCause]) -> bool:
        """Apply last-word rules by death timing and cause.

        Common tables allow first-night deaths and daytime exiles to speak,
        while later night deaths and players shot by the Hunter leave silently.
        Each category remains configurable as a house rule.
        """
        rules = self.config.rules
        if not rules.last_words:
            return False
        if DeathCause.VOTE in causes:
            return rules.day_vote_last_words
        if DeathCause.HUNTER in causes:
            return rules.hunter_shot_last_words
        if causes & {DeathCause.WOLF, DeathCause.POISON, DeathCause.DIVINATION}:
            return (
                rules.first_night_last_words
                if self.day == 1
                else rules.night_death_last_words
            )
        return False

    def _act(self, player: PlayerState, request: ActionRequest) -> AgentResponse:
        replayed = self._replay_action(player, request)
        if replayed is not None:
            response = replayed
            private_note = "\n".join(
                part for part in (response.thought, response.note) if part.strip()
            )
            player.memory.reflect(self.day, self.phase, private_note)
            return response
        heartbeat_stop = threading.Event()
        heartbeat: threading.Thread | None = None
        if self.config.spectator_progress:
            self.terminal.progress(self._spectator_action_text(player, request))
            if isinstance(player.controller, LLMController):
                effort = player.controller.client.config.reasoning_effort or "default"
                heartbeat = threading.Thread(
                    target=self._spectator_heartbeat,
                    args=(heartbeat_stop, effort),
                    daemon=True,
                )
                heartbeat.start()
        try:
            response = self._controller_action(player, request)
            self._record_action(player, request, response)
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1)
                self.terminal.clear_transient_progress()
        private_note = "\n".join(
            part for part in (response.thought, response.note) if part.strip()
        )
        player.memory.reflect(self.day, self.phase, private_note)
        return response

    def _act_independent(
        self,
        actions: list[tuple[PlayerState, ActionRequest]],
    ) -> list[AgentResponse]:
        """Run mutually invisible LLM choices concurrently and journal in seat order.

        Public votes are revealed only after every choice is collected, so LLM
        voters cannot observe one another and may safely run in parallel. Human
        and local-bot inputs are collected before network work begins to avoid
        progress output interrupting an interactive prompt.
        """
        if not self.config.parallel_llm_votes:
            return [self._act(player, request) for player, request in actions]

        responses: list[AgentResponse | None] = [None] * len(actions)
        replayed: set[int] = set()
        pending: list[int] = []
        for index, (player, request) in enumerate(actions):
            response = self._replay_action(player, request)
            if response is not None:
                responses[index] = response
                replayed.add(index)
            elif self._controller_kinds.get(player.player_id) == "llm":
                pending.append(index)
            else:
                responses[index] = self._controller_action(player, request)

        futures: dict[int, Future[AgentResponse]] = {}
        heartbeat_stop = threading.Event()
        heartbeat: threading.Thread | None = None
        if pending:
            if self.config.spectator_progress:
                self.terminal.progress(
                    self._t(
                        f"正在并行收集 {len(pending)} 个互不可见的 LLM 投票……",
                        f"Collecting {len(pending)} mutually invisible LLM votes in parallel...",
                    ),
                )
            executor = ThreadPoolExecutor(max_workers=len(pending))
            for index in pending:
                player, request = actions[index]
                futures[index] = executor.submit(
                    self._controller_action,
                    player,
                    request,
                )
            if self.config.spectator_progress:
                heartbeat = threading.Thread(
                    target=self._parallel_vote_heartbeat,
                    args=(heartbeat_stop, tuple(futures.values())),
                    daemon=True,
                )
                heartbeat.start()
        else:
            executor = None

        try:
            for index, (player, request) in enumerate(actions):
                if index in futures:
                    responses[index] = futures[index].result()
                response = responses[index]
                if response is None:
                    msg = "Independent controller batch produced no response"
                    raise RuntimeError(msg)
                if index not in replayed:
                    self._record_action(player, request, response)
                private_note = "\n".join(
                    part for part in (response.thought, response.note) if part.strip()
                )
                player.memory.reflect(self.day, self.phase, private_note)
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1)
                self.terminal.clear_transient_progress()
            if executor is not None:
                executor.shutdown(wait=True)
        return [response for response in responses if response is not None]

    def _parallel_vote_heartbeat(
        self,
        stop: threading.Event,
        futures: tuple[Future[AgentResponse], ...],
    ) -> None:
        """Show aggregate parallel-vote progress without exposing choices."""
        started = time.monotonic()
        while not stop.wait(1):
            completed = sum(future.done() for future in futures)
            elapsed = int(time.monotonic() - started)
            self.terminal.transient_progress(
                self._t(
                    f"并行投票处理中：{completed}/{len(futures)} 完成，已用 {elapsed} 秒",
                    f"Parallel votes: {completed}/{len(futures)} complete after {elapsed}s",
                ),
            )

    def _action_signature(
        self,
        player: PlayerState,
        request: ActionRequest,
    ) -> dict[str, object]:
        """Identify one deterministic controller call within a recoverable phase."""
        return {
            "day": self.day,
            "phase": self.phase,
            "player_id": player.player_id,
            "kind": request.kind.value,
            "prompt": request.prompt,
            "options": [option.value for option in request.options],
            "allow_abstain": request.allow_abstain,
        }

    def _replay_action(
        self,
        player: PlayerState,
        request: ActionRequest,
    ) -> AgentResponse | None:
        """Replay a completed response from the per-call recovery journal."""
        if self._action_cursor >= len(self._action_journal):
            return None
        entry = self._action_journal[self._action_cursor]
        signature = self._action_signature(player, request)
        for key, value in signature.items():
            if entry.get(key) != value:
                msg = (
                    "Checkpoint action journal diverged at index "
                    f"{self._action_cursor}: expected {signature!r}, got {entry!r}"
                )
                raise RuntimeError(msg)
        response = entry.get("response")
        if not isinstance(response, dict):
            msg = "Checkpoint action response is malformed"
            raise TypeError(msg)
        self._action_cursor += 1
        return AgentResponse(
            choice=response.get("choice"),
            text=str(response.get("text", "")),
            thought=str(response.get("thought", "")),
            note=str(response.get("note", "")),
            used_fallback=bool(response.get("used_fallback", False)),
            fallback_error=str(response.get("fallback_error", "")),
            attempts=int(response.get("attempts", 1)),
        )

    def _record_action(
        self,
        player: PlayerState,
        request: ActionRequest,
        response: AgentResponse,
    ) -> None:
        """Persist one successful controller response before applying its effects."""
        if self._checkpoint_base_payload is None or self._checkpoint_path is None:
            return
        entry = {
            **self._action_signature(player, request),
            "response": asdict(response),
        }
        self._action_journal.append(entry)
        self._action_cursor = len(self._action_journal)
        payload = {
            **self._checkpoint_base_payload,
            "action_journal": self._action_journal,
            "controller_metrics": asdict(self._controller_metrics),
            "fallback_records": [asdict(item) for item in self._fallback_records],
            "last_nonterminal_snapshot": self._last_nonterminal_snapshot,
            "elapsed_seconds": time.monotonic() - self._started_at,
        }
        self._checkpoint_base_payload = payload
        self._write_checkpoint(payload)

    def _controller_action(
        self,
        player: PlayerState,
        request: ActionRequest,
    ) -> AgentResponse:
        """Call one controller with bounded retries and validated choices."""
        is_llm = self._controller_kinds.get(player.player_id) == "llm"
        if is_llm:
            self._increment_metric("actions")
        last_error = ""
        for attempt in range(self.config.controller_retries + 1):
            if is_llm:
                self._increment_metric("attempts")
            try:
                response = player.controller.act(self._view(player), request)
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception as exc:
                last_error = self._short_error(f"{type(exc).__name__}: {exc}")
                if is_llm:
                    self._increment_metric("failures")
                if attempt < self.config.controller_retries:
                    if is_llm:
                        self._increment_metric("retries")
                    self._announce_controller_retry(attempt + 1)
                    continue
                if self.config.strict_controllers:
                    msg = self._strict_controller_error(
                        player,
                        request,
                        last_error,
                    )
                    raise RuntimeError(msg) from exc
                with self._state_lock:
                    self.boundary.private(
                        day=self.day,
                        phase=self.phase,
                        recipient=player.player_id,
                        text=self._t(
                            f"控制器调用失败，法官启用系统安全后备：{last_error}",
                            f"Controller failed; judge used the system safe fallback: {last_error}",
                        ),
                    )
                return self._safe_fallback(player, request, last_error, attempt + 1)

            legal = {option.value for option in request.options}
            illegal = bool(
                request.options
                and response.choice not in legal
                and (response.choice is not None or not request.allow_abstain)
            )
            if not illegal:
                return replace(response, attempts=attempt + 1)
            last_error = self._short_error(
                f"Illegal choice for {request.kind.value}: {response.choice!r}",
            )
            if is_llm:
                self._increment_metric("failures")
            if attempt < self.config.controller_retries:
                if is_llm:
                    self._increment_metric("retries")
                self._announce_controller_retry(attempt + 1)
                continue
            if self.config.strict_controllers:
                msg = self._strict_controller_error(
                    player,
                    request,
                    f"illegal choice {response.choice!r}",
                )
                raise RuntimeError(msg)
            with self._state_lock:
                self.boundary.private(
                    day=self.day,
                    phase=self.phase,
                    recipient=player.player_id,
                    text=self._t(
                        "提交了非法选项，法官改用系统安全后备。",
                        "Illegal option; judge selected the system safe fallback.",
                    ),
                )
            return self._safe_fallback(player, request, last_error, attempt + 1)
        msg = "Controller retry loop ended without a response"
        raise RuntimeError(msg)

    def _strict_controller_error(
        self,
        player: PlayerState,
        request: ActionRequest,
        detail: str,
    ) -> str:
        """Keep private actors and abilities out of resumable terminal errors."""
        public_kinds = {ActionKind.SPEAK, ActionKind.LAST_WORDS, ActionKind.VOTE}
        if request.kind in public_kinds:
            return (
                f"Controller failed for {self._player_label(player)} during "
                f"{request.kind.value}: {detail}"
            )
        category = (
            "invalid response"
            if detail.lower().startswith("illegal choice")
            else detail.split(":", maxsplit=1)[0]
        )
        return f"Controller failed during a private action ({category}); private details were not printed."

    @staticmethod
    def _short_error(detail: str, limit: int = 500) -> str:
        """Bound provider diagnostics before persisting or printing them."""
        clean = " ".join(detail.split())
        return clean if len(clean) <= limit else clean[: limit - 1] + "…"

    def _safe_fallback(
        self,
        player: PlayerState,
        request: ActionRequest,
        error: str,
        attempts: int,
    ) -> AgentResponse:
        """Apply a conservative, visible fallback only in explicitly casual games."""
        if self._controller_kinds.get(player.player_id) == "llm":
            self._increment_metric("fallbacks")
        record = FallbackRecord(
            day=self.day,
            phase=self.phase,
            player_name=self._player_label(player),
            action_kind=request.kind.value,
            error=error,
        )
        with self._state_lock:
            self._fallback_records.append(record)
        self._announce_controller_fallback(player, request)
        fallback = SafeFallbackController().act(self._view(player), request)
        return replace(
            fallback,
            used_fallback=True,
            fallback_error=error,
            attempts=attempts,
        )

    def _increment_metric(self, name: str) -> None:
        """Update one controller metric safely during parallel vote requests."""
        with self._state_lock:
            value = getattr(self._controller_metrics, name)
            setattr(self._controller_metrics, name, value + 1)

    def _announce_controller_fallback(
        self,
        player: PlayerState,
        request: ActionRequest,
    ) -> None:
        """Expose degradation without leaking the actor behind a private action."""
        if not self.config.spectator_progress:
            return
        public_kinds = {ActionKind.SPEAK, ActionKind.LAST_WORDS, ActionKind.VOTE}
        if request.kind in public_kinds:
            text = self._t(
                f"{self._player_label(player)} 的公开动作已使用系统安全后备。",
                f"{self._player_label(player)} used the system safe fallback for a public action.",
            )
        else:
            text = self._t(
                "一项私密行动的控制器不可用，已使用系统安全后备；具体参与者将在终局披露。",
                "A private controller action used the system safe fallback; the actor will be disclosed after the game.",
            )
        self.terminal.progress(text)

    def _announce_controller_retry(self, retry_number: int) -> None:
        """Expose a technical retry without identifying a private actor."""
        if not self.config.spectator_progress:
            return
        self.terminal.progress(
            self._t(
                f"LLM 调用未成功，正在进行第 {retry_number}/{self.config.controller_retries} 次重试……",
                f"The LLM call failed; retry {retry_number}/{self.config.controller_retries} is starting...",
            ),
        )

    def _spectator_heartbeat(self, stop: threading.Event, effort: str) -> None:
        """Update one transient elapsed-time status while an LLM call is pending."""
        started = time.monotonic()
        while not stop.wait(1):
            elapsed = int(time.monotonic() - started)
            self.terminal.transient_progress(
                self._t(
                    f"LLM {effort} 推理中：{elapsed} 秒",
                    f"LLM {effort} reasoning: {elapsed} seconds",
                ),
            )

    def _spectator_action_text(
        self,
        player: PlayerState,
        request: ActionRequest,
    ) -> str:
        """Describe action progress without revealing role identities or targets."""
        if request.kind is ActionKind.SPEAK:
            return self._t(
                f"{self._player_label(player)} 正在组织公开发言……",
                f"{self._player_label(player)} is preparing a public statement...",
            )
        if request.kind is ActionKind.LAST_WORDS:
            return self._t(
                f"{self._player_label(player)} 正在组织遗言……",
                f"{self._player_label(player)} is preparing final words...",
            )
        if request.kind is ActionKind.VOTE:
            return self._t(
                f"{self._player_label(player)} 正在提交公开投票……",
                f"{self._player_label(player)} is submitting a public vote...",
            )
        if self.phase == "setup":
            return self._t(
                "开局私密能力正在处理中……",
                "A private setup ability is being resolved...",
            )
        if self.phase == "night":
            return self._t(
                "一项夜间私密行动正在处理中……",
                "A private night action is being resolved...",
            )
        return self._t(
            "一项私密结算正在处理中……",
            "A private resolution is in progress...",
        )

    def _view(self, player: PlayerState) -> PlayerView:
        role_names = localized(ROLE_NAMES, self.config.language)
        role_descriptions = localized(ROLE_DESCRIPTIONS, self.config.language)
        return PlayerView(
            player_id=player.player_id,
            name=player.name,
            role=player.role,
            role_name=role_names[player.role],
            role_description=role_descriptions[player.role],
            faction=player.role.faction,
            lover=(
                (player.lover_id, self._by_id[player.lover_id].name)
                if player.lover_id
                else None
            ),
            alive_players=tuple((item.player_id, item.name) for item in self._alive()),
            dead_players=tuple(
                (item.player_id, item.name) for item in self.players if not item.alive
            ),
            events=tuple(player.memory.events),
            thoughts=tuple(player.memory.thoughts),
            skills=player.skills,
            day=self.day,
            phase=self.phase,
            language=self.config.language,
            seat_number=player.seat_number,
            seat_players=tuple(
                (item.player_id, item.seat_number, item.name) for item in self.players
            ),
            mechanical_context=self._mechanical_context(),
        )

    def _record_nonterminal_snapshot(self, reason: str) -> None:
        """Remember the latest public state that passed an immediate win check."""
        alive = self._alive()
        self._last_nonterminal_snapshot = {
            "day": self.day,
            "phase": self.phase,
            "reason": reason,
            "alive_count": len(alive),
            "alive_ids": [player.player_id for player in alive],
            "max_wolves": max(0, (len(alive) - 1) // 2),
        }

    def _mechanical_context(self) -> str:
        """Render a public parity constraint from the last completed win check."""
        snapshot = self._last_nonterminal_snapshot
        if not snapshot:
            return ""
        alive_count = int(snapshot["alive_count"])
        max_wolves = int(snapshot["max_wolves"])
        day = int(snapshot["day"])
        alive_ids = snapshot.get("alive_ids", [])
        alive_labels = [
            self._player_label(self._by_id[player_id])
            for player_id in alive_ids
            if isinstance(player_id, str) and player_id in self._by_id
        ]
        english_roster = f" ({', '.join(alive_labels)})" if alive_labels else ""
        chinese_roster = f"（{'、'.join(alive_labels)}）" if alive_labels else ""
        if self.config.language == "en":
            return (
                f"Last completed win check: Day {day}, {alive_count} players were alive "
                f"and the game continued, so at most {max_wolves} living werewolves "
                f"were possible in that exact snapshot{english_roster}. "
                "Any claimed role world must "
                "respect this deterministic parity fact."
            )
        return (
            f"最近一次已完成的胜负检查：第 {day} 天有 {alive_count} 人存活且游戏继续，"
            f"因此该时点至多有 {max_wolves} 名存活狼人{chinese_roster}。"
            "任何身份组合都必须符合这条"
            "确定性的即时胜负约束。"
        )

    def _announce(self, text: str) -> None:
        self.boundary.public(day=self.day, phase=self.phase, text=text)
        self.terminal.announce(text)

    def _say(
        self,
        player: PlayerState,
        text: str,
        *,
        fallback: bool = False,
    ) -> None:
        label = self._player_label(player)
        fallback_marker = self._t("【系统安全后备】", "[system safe fallback]")
        rendered = f"{label}{fallback_marker if fallback else ''}：{text}"
        self.boundary.public(
            day=self.day,
            phase=self.phase,
            text=rendered,
            sender=label,
        )
        self.terminal.say(
            label,
            text,
            fallback_label=(
                self._t("系统安全后备", "system safe fallback") if fallback else None
            ),
        )

    def _winner(self) -> Faction | None:
        """Return the winning faction after applying film third-party priority."""
        wolves = sum(
            player.alive and player.role is Role.WEREWOLF for player in self.players
        )
        non_wolves = sum(
            player.alive and player.role is not Role.WEREWOLF for player in self.players
        )
        base_winner: Faction | None = None
        if wolves == 0:
            base_winner = Faction.GOOD
        elif wolves >= non_wolves:
            base_winner = Faction.WEREWOLF
        if base_winner is None:
            return None
        if any(player.alive and player.role is Role.FOX for player in self.players):
            return Faction.FOX
        lovers = [player for player in self.players if player.lover_id is not None]
        if len(lovers) == 2 and all(player.alive for player in lovers):
            return Faction.LOVERS
        return base_winner

    def _winning_players(self, winner: Faction | None) -> tuple[str, ...]:
        """List seats that satisfy faction and mode-specific survival conditions."""
        if winner is None:
            return ()
        if winner is Faction.FOX:
            winners = [
                player
                for player in self.players
                if player.alive and player.role is Role.FOX
            ]
        elif winner is Faction.LOVERS:
            winners = [
                player
                for player in self.players
                if player.role is Role.CUPID or player.lover_id is not None
            ]
        else:
            winners = [
                player
                for player in self.players
                if player.role.faction is winner and player.lover_id is None
            ]
        if self.config.role_preset != "classic":
            winners = [player for player in winners if player.alive]
        return tuple(player.name for player in winners)

    def _prize_shares(
        self,
        winning_players: tuple[str, ...],
    ) -> tuple[tuple[str, float], ...]:
        """Split a normalized movie prize pool equally among surviving winners."""
        if self.config.role_preset == "classic" or not winning_players:
            return ()
        share = 1 / len(winning_players)
        return tuple((name, share) for name in winning_players)

    def _finish(self, winner: Faction | None, reason: str) -> GameResult:
        self.phase = "finished"
        if winner is Faction.GOOD:
            outcome = self._t("好人阵营获胜！", "The good faction wins!")
        elif winner is Faction.WEREWOLF:
            outcome = self._t("狼人阵营获胜！", "The werewolf faction wins!")
        elif winner is Faction.FOX:
            outcome = self._t("妖狐独自获胜！", "The Fox wins alone!")
        elif winner is Faction.LOVERS:
            outcome = self._t(
                "恋人阵营触发独占结算！",
                "The Lovers trigger the exclusive outcome!",
            )
        else:
            outcome = self._t(
                "达到最大天数，本局平局。",
                "Maximum days reached; the game is a draw.",
            )
        role_names = localized(ROLE_NAMES, self.config.language)
        reveal = "；".join(
            f"{self._player_label(player)}={role_names[player.role]}"
            for player in self.players
        )
        lover_pair = [
            self._player_label(player) for player in self.players if player.lover_id
        ]
        lover_reveal = (
            self._t(
                f"恋人：{'、'.join(lover_pair)}。",
                f"Lovers: {' and '.join(lover_pair)}.",
            )
            if lover_pair
            else ""
        )
        winning_players = self._winning_players(winner)
        winner_names = set(winning_players)
        winning_labels = [
            self._player_label(player)
            for player in self.players
            if player.name in winner_names
        ]
        prize_shares = self._prize_shares(winning_players)
        winners_text = self._t(
            f"获胜玩家：{'、'.join(winning_labels) if winning_labels else '无'}。",
            f"Winning players: {', '.join(winning_labels) if winning_labels else 'none'}.",
        )
        if prize_shares:
            share_percent = f"{prize_shares[0][1] * 100:.2f}".rstrip("0").rstrip(".")
            prize_text = self._t(
                f"奖金分配：{len(prize_shares)} 名存活获胜者均分奖金池，每人 {share_percent}%。",
                f"Prize split: {len(prize_shares)} surviving winners receive {share_percent}% each.",
            )
        elif self.config.role_preset != "classic" and winner is not None:
            prize_text = self._t(
                "阵营终局条件已经达成，但没有符合生存条件的获胜者，奖金无人领取。",
                "A faction end condition was reached, but no eligible survivor can claim the prize.",
            )
        else:
            prize_text = ""
        roles_text = self._t(
            f"全部身份：{reveal}。",
            f"All roles: {reveal}.",
        )
        self._announce(
            " ".join(
                part
                for part in (
                    outcome,
                    winners_text,
                    prize_text,
                    roles_text,
                    lover_reveal,
                )
                if part
            ),
        )
        token_usage = self._llm_token_usage_text()
        metric_label = self._t("统计", "Metrics")
        if token_usage:
            self.terminal.metric(token_usage, label=metric_label)
        reliability = self._controller_reliability_text()
        if reliability:
            self.terminal.metric(reliability, label=metric_label)
        for record in self._fallback_records:
            self.terminal.metric(
                self._t(
                    f"后备记录：第 {record.day} 天/{record.phase}，{record.player_name}，"
                    f"动作 {record.action_kind}，原因 {record.error}",
                    f"Fallback: day {record.day}/{record.phase}, {record.player_name}, "
                    f"action {record.action_kind}, reason {record.error}",
                ),
                label=metric_label,
            )
        if self.config.memory_directory:
            self.export_memories(self.config.memory_directory)
        self._clear_checkpoint()
        return GameResult(
            winner=winner,
            winning_players=winning_players,
            prize_shares=prize_shares,
            days=self.day,
            survivors=tuple(player.name for player in self._alive()),
            reason=reason,
            duration_seconds=time.monotonic() - self._started_at,
            controller_actions=self._controller_metrics.actions,
            controller_attempts=self._controller_metrics.attempts,
            controller_failures=self._controller_metrics.failures,
            controller_retries=self._controller_metrics.retries,
            controller_fallbacks=self._controller_metrics.fallbacks,
            seat_labels=tuple(
                (player.name, self._player_label(player)) for player in self.players
            ),
        )

    def _controller_reliability_text(self) -> str:
        """Summarize LLM reliability without exposing in-game secrets mid-match."""
        metrics = self._controller_metrics
        if metrics.actions == 0:
            return ""
        clean = metrics.fallbacks == 0
        return self._t(
            "LLM 控制器："
            f"动作 {metrics.actions}，请求尝试 {metrics.attempts}，失败 {metrics.failures}，"
            f"重试 {metrics.retries}，安全后备 {metrics.fallbacks}。"
            f"本局{'满足' if clean else '不满足'}完整 LLM 对局标准。",
            "LLM controllers: "
            f"{metrics.actions} actions, {metrics.attempts} attempts, {metrics.failures} failures, "
            f"{metrics.retries} retries, {metrics.fallbacks} safe fallbacks. "
            f"This match {'meets' if clean else 'does not meet'} the complete-LLM standard.",
        )

    def _llm_token_usage_text(self) -> str:
        """Summarize provider-reported cache usage without exposing player data."""
        clients = {
            id(player.controller.client): player.controller.client
            for player in self.players
            if isinstance(player.controller, LLMController)
        }.values()
        input_tokens = sum(client.observed_input_tokens for client in clients)
        cached_tokens = sum(client.observed_cached_tokens for client in clients)
        output_tokens = sum(client.observed_output_tokens for client in clients)
        usage_responses = sum(client.observed_usage_responses for client in clients)
        if input_tokens <= 0:
            return ""
        cache_rate = cached_tokens / input_tokens
        return self._t(
            "本进程已观测到的 LLM token："
            f"输入 {input_tokens}，缓存命中 {cached_tokens}（{cache_rate:.1%}），"
            f"输出 {output_tokens}；provider 返回 usage 的响应共 {usage_responses} 次。",
            "LLM tokens observed in this process: "
            f"{input_tokens} input, {cached_tokens} cached ({cache_rate:.1%}), "
            f"{output_tokens} output across {usage_responses} responses with usage.",
        )

    def export_memories(self, directory: str | Path) -> list[Path]:
        """Persist one privacy-filtered transcript per player as UTF-8 JSON."""
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for player in self.players:
            safe_name = (
                re.sub(r"[^\w.-]+", "_", player.name, flags=re.UNICODE).strip("_")
                or player.player_id
            )
            path = output / f"{player.player_id}_{safe_name}.json"
            payload = {
                "player_id": player.player_id,
                "name": player.name,
                "role": player.role.value,
                "lover_id": player.lover_id,
                "lover_name": (
                    self._by_id[player.lover_id].name if player.lover_id else None
                ),
                "skills": [asdict(skill) for skill in player.skills],
                "events": [asdict(event) for event in player.memory.events],
                "thoughts": [asdict(thought) for thought in player.memory.thoughts],
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            written.append(path)
        return written

    def _alive(self) -> list[PlayerState]:
        return [player for player in self.players if player.alive]

    def _wolf_ids(self, *, alive_only: bool) -> list[str]:
        return [
            player.player_id
            for player in self.players
            if player.role is Role.WEREWOLF and (player.alive or not alive_only)
        ]

    def _options(self, players: Iterable[PlayerState]) -> tuple[ActionOption, ...]:
        """Build legal options with stable seat numbers for human and LLM users."""
        return tuple(
            ActionOption(player.player_id, self._player_label(player))
            for player in players
        )

    def _player_label(self, player: PlayerState) -> str:
        """Return a localized stable seat label without mutating configured names."""
        if self.config.language == "en":
            return f"Seat {player.seat_number} {player.name}"
        return f"{player.seat_number}号 {player.name}"

    def _plurality(self, votes: list[str]) -> str | None:
        if not votes:
            return None
        counts = Counter(votes)
        highest = max(counts.values())
        tied = sorted(target for target, count in counts.items() if count == highest)
        return self.rng.choice(tied)

    def _t(self, chinese: str, english: str) -> str:
        return english if self.config.language == "en" else chinese
