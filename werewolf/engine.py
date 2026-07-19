"""Deterministic Werewolf judge and complete day/night game loop."""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from contextlib import suppress
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .agents import (
    BotController,
    Controller,
    HumanController,
    LLMController,
    OpenAICompatibleClient,
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
    PlayerState,
    PlayerView,
    Role,
    localized,
)
from .skills import resolve_player_skills

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .config import GameConfig


class DeathCause(str, Enum):
    """Internal cause labels used for role-skill resolution."""

    WOLF = "wolf"
    POISON = "poison"
    VOTE = "vote"
    HUNTER = "hunter"


@dataclass(frozen=True)
class GameResult:
    """Summary returned after the game ends."""

    winner: Faction | None
    days: int
    survivors: tuple[str, ...]
    reason: str


def role_deck(player_count: int) -> list[Role]:
    """Return a balanced classic deck for six to sixteen players.

    Six-player games omit the Hunter; larger games include all three special
    good roles. This is a compact no-Sheriff rule set suitable for terminal and
    agent play rather than a tournament-specific ruleset.
    """
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
    ) -> None:
        validate_config(config)
        self.config = config
        self.rng = random.Random(config.seed)  # noqa: S311 - game simulation, not security.
        self.terminal = terminal or Terminal(clear_screen=config.clear_screen)
        self.day = 0
        self.phase = "setup"
        self._antidote_available = True
        self._poison_available = True

        roles = self._roles()
        self.players: list[PlayerState] = []
        for index, (seat, role) in enumerate(zip(config.players, roles), start=1):
            player_id = f"p{index}"
            controller = self._controller_for(
                player_id,
                seat.name,
                seat.controller,
                seat.provider,
                seat.persona,
                controllers or {},
            )
            self.players.append(
                PlayerState(
                    player_id=player_id,
                    name=seat.name,
                    role=role,
                    controller=controller,
                    skills=resolve_player_skills(role, list(seat.skills)),
                ),
            )
        self._by_id = {player.player_id: player for player in self.players}
        self.boundary = InformationBoundary(self.players)

    def _roles(self) -> list[Role]:
        fixed = [player.fixed_role for player in self.config.players]
        if all(role is not None for role in fixed):
            roles = [role for role in fixed if role is not None]
            wolves = sum(role is Role.WEREWOLF for role in roles)
            if wolves < 1 or wolves >= len(roles):
                msg = "A fixed role set must contain both factions"
                raise ValueError(msg)
            return roles
        roles = role_deck(len(self.config.players))
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
            return HumanController(self.terminal)
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
        self._setup()
        for day in range(1, self.config.rules.max_days + 1):
            self.day = day
            self._night()
            winner = self._winner()
            if winner is not None:
                return self._finish(winner, "night_resolution")
            self._daytime()
            winner = self._winner()
            if winner is not None:
                return self._finish(winner, "day_vote")
        return self._finish(None, "max_days")

    def _setup(self) -> None:
        self.phase = "setup"
        seats = "、".join(player.name for player in self.players)
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
        wolf_names = "、".join(self._by_id[player_id].name for player_id in wolf_ids)
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
        # Reveal roles one human at a time; LLMs learn through isolated memory.
        for player in self.players:
            if isinstance(player.controller, HumanController):
                self.terminal.private_turn(self._view(player))
                if self.config.clear_screen:
                    with suppress(EOFError):
                        input(
                            self._t(
                                "记住身份后按回车交接终端……",
                                "Memorize your role, then press Enter...",
                            ),
                        )
                    self.terminal.clear()

    def _night(self) -> None:
        self.phase = "night"
        self._announce(
            self._t(
                f"第 {self.day} 夜，天黑请闭眼。",
                f"Night {self.day}. Everyone close your eyes.",
            ),
        )
        victim = self._werewolf_turn()
        self._seer_turn()
        saved, poisoned = self._witch_turn(victim)
        deaths: dict[str, set[DeathCause]] = {}
        if victim and not saved:
            deaths.setdefault(victim, set()).add(DeathCause.WOLF)
        if poisoned:
            deaths.setdefault(poisoned, set()).add(DeathCause.POISON)
        if deaths:
            names = "、".join(self._by_id[player_id].name for player_id in deaths)
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
                        text=f"{wolf.name}：{response.text.strip()}",
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
        target_name = self._by_id[target].name if target else self._t("无人", "nobody")
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

    def _seer_turn(self) -> None:
        seer = next(
            (player for player in self._alive() if player.role is Role.SEER),
            None,
        )
        if not seer:
            return
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
                self._t("狼人阵营", "werewolf faction")
                if target.role.faction is Faction.WEREWOLF
                else self._t("好人阵营", "good faction")
            )
            self.boundary.private(
                day=self.day,
                phase=self.phase,
                recipient=seer.player_id,
                text=self._t(
                    f"查验结果：{target.name} 属于【{faction}】。",
                    f"Inspection: {target.name} belongs to the {faction}.",
                ),
            )

    def _witch_turn(self, victim: str | None) -> tuple[bool, str | None]:
        witch = next(
            (player for player in self._alive() if player.role is Role.WITCH),
            None,
        )
        if not witch:
            return False, None
        victim_name = self._by_id[victim].name if victim else self._t("无人", "nobody")
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
        self._announce(
            self._t(
                f"第 {self.day} 天，开始公开讨论。",
                f"Day {self.day}. Public discussion begins.",
            ),
        )
        for player in list(self._alive()):
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
            self._say(player, speech)
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
            names = "、".join(self._by_id[player_id].name for player_id in leaders)
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
        target_name = self._by_id[target].name
        self._apply_deaths(
            {target: {DeathCause.VOTE}},
            self._t(
                f"投票结束，{target_name} 被放逐。",
                f"Voting ends. {target_name} is eliminated.",
            ),
        )

    def _collect_votes(
        self,
        candidate_ids: set[str] | None,
        *,
        runoff: bool = False,
    ) -> dict[str, str | None]:
        votes: dict[str, str | None] = {}
        alive = list(self._alive())
        for voter in alive:
            candidates = [
                player
                for player in alive
                if (candidate_ids is None or player.player_id in candidate_ids)
                and (self.config.rules.allow_self_vote or player is not voter)
            ]
            response = self._act(
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
            )
            votes[voter.player_id] = response.choice
        vote_text = "；".join(
            f"{self._by_id[voter].name}→{self._by_id[target].name if target else self._t('弃权', 'abstain')}"
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
        newly_dead: list[tuple[PlayerState, set[DeathCause]]] = []
        for player_id, causes in deaths.items():
            player = self._by_id[player_id]
            if player.alive:
                player.alive = False
                newly_dead.append((player, causes))
        if not newly_dead:
            return
        if self.config.rules.reveal_roles_on_death:
            role_names = localized(ROLE_NAMES, self.config.language)
            reveal = "；".join(
                f"{player.name}={role_names[player.role]}" for player, _ in newly_dead
            )
            announcement += self._t(f" 身份公开：{reveal}。", f" Roles: {reveal}.")
        self._announce(announcement)
        queue = list(newly_dead)
        while queue:
            player, causes = queue.pop(0)
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
                    self._say(player, response.text.strip())
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
                        self._t(
                            f"{player.name} 发动猎人技能，{victim.name} 被带走。",
                            f"{player.name} uses the Hunter skill and takes down {victim.name}.",
                        ),
                    )
                    queue.append((victim, {DeathCause.HUNTER}))

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
        if DeathCause.WOLF in causes or DeathCause.POISON in causes:
            return (
                rules.first_night_last_words
                if self.day == 1
                else rules.night_death_last_words
            )
        return False

    def _act(self, player: PlayerState, request: ActionRequest) -> AgentResponse:
        try:
            response = player.controller.act(self._view(player), request)
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - external controllers must not crash the judge.
            self.boundary.private(
                day=self.day,
                phase=self.phase,
                recipient=player.player_id,
                text=self._t(
                    f"控制器调用失败，法官启用本地后备动作：{type(exc).__name__}: {exc}",
                    f"Controller failed; judge used a local fallback: {type(exc).__name__}: {exc}",
                ),
            )
            response = BotController(self.rng).act(self._view(player), request)
        legal = {option.value for option in request.options}
        if (
            request.options
            and response.choice not in legal
            and (response.choice is not None or not request.allow_abstain)
        ):
            self.boundary.private(
                day=self.day,
                phase=self.phase,
                recipient=player.player_id,
                text=self._t(
                    "提交了非法选项，法官改用合法后备动作。",
                    "Illegal option; judge selected a legal fallback.",
                ),
            )
            fallback = BotController(self.rng).act(self._view(player), request)
            response = AgentResponse(
                choice=fallback.choice,
                text=response.text,
                thought=response.thought,
                note=response.note,
            )
        private_note = "\n".join(
            part for part in (response.thought, response.note) if part.strip()
        )
        player.memory.reflect(self.day, self.phase, private_note)
        return response

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
        )

    def _announce(self, text: str) -> None:
        self.boundary.public(day=self.day, phase=self.phase, text=text)
        self.terminal.announce(text)

    def _say(self, player: PlayerState, text: str) -> None:
        rendered = f"{player.name}：{text}"
        self.boundary.public(
            day=self.day,
            phase=self.phase,
            text=rendered,
            sender=player.name,
        )
        print(f"[{player.name}] {text}", flush=True)

    def _winner(self) -> Faction | None:
        wolves = sum(
            player.alive and player.role is Role.WEREWOLF for player in self.players
        )
        good = sum(
            player.alive and player.role is not Role.WEREWOLF for player in self.players
        )
        if wolves == 0:
            return Faction.GOOD
        if wolves >= good:
            return Faction.WEREWOLF
        return None

    def _finish(self, winner: Faction | None, reason: str) -> GameResult:
        self.phase = "finished"
        if winner is Faction.GOOD:
            outcome = self._t("好人阵营获胜！", "The good faction wins!")
        elif winner is Faction.WEREWOLF:
            outcome = self._t("狼人阵营获胜！", "The werewolf faction wins!")
        else:
            outcome = self._t(
                "达到最大天数，本局平局。",
                "Maximum days reached; the game is a draw.",
            )
        role_names = localized(ROLE_NAMES, self.config.language)
        reveal = "；".join(
            f"{player.name}={role_names[player.role]}" for player in self.players
        )
        self._announce(
            self._t(
                f"{outcome} 全部身份：{reveal}。",
                f"{outcome} All roles: {reveal}.",
            ),
        )
        if self.config.memory_directory:
            self.export_memories(self.config.memory_directory)
        return GameResult(
            winner=winner,
            days=self.day,
            survivors=tuple(player.name for player in self._alive()),
            reason=reason,
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

    @staticmethod
    def _options(players: Iterable[PlayerState]) -> tuple[ActionOption, ...]:
        return tuple(ActionOption(player.player_id, player.name) for player in players)

    def _plurality(self, votes: list[str]) -> str | None:
        if not votes:
            return None
        counts = Counter(votes)
        highest = max(counts.values())
        tied = sorted(target for target, count in counts.items() if count == highest)
        return self.rng.choice(tied)

    def _t(self, chinese: str, english: str) -> str:
        return english if self.config.language == "en" else chinese
