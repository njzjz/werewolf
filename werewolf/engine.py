"""Deterministic Werewolf judge and complete day/night game loop."""

from __future__ import annotations

import json
import random
import re
import threading
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
from .skills import add_lover_skill, add_movie_survival_skill, resolve_player_skills

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .config import GameConfig


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
    ) -> None:
        validate_config(config)
        self.config = config
        self.rng = random.Random(config.seed)  # noqa: S311 - game simulation, not security.
        self.terminal = terminal or Terminal(
            clear_screen=config.clear_screen,
            transcript_path=config.public_transcript_path,
        )
        self.day = 0
        self.phase = "setup"
        self._antidote_available = True
        self._poison_available = True
        self._last_exiled_id: str | None = None

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
            skills = resolve_player_skills(role, list(seat.skills))
            if config.role_preset != "classic":
                skills = add_movie_survival_skill(skills)
            self.players.append(
                PlayerState(
                    player_id=player_id,
                    name=seat.name,
                    role=role,
                    controller=controller,
                    skills=skills,
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
                                "记住身份后按回车交接终端……",
                                "Memorize your role, then press Enter...",
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
                f"另一名共有者是 {second.name}。",
                f"The other Shared Player is {second.name}.",
            ),
        )
        self.boundary.private(
            day=0,
            phase=self.phase,
            recipient=second.player_id,
            text=self._t(
                f"另一名共有者是 {first.name}。",
                f"The other Shared Player is {first.name}.",
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
            f"{first.name}、{second.name}",
            f"{first.name} and {second.name}",
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
                f"灵媒结果：昨日被放逐的 {target.name} 显示为【{alignment}】。",
                f"Medium result: yesterday's exile {target.name} appears {alignment}.",
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
                    text=f"{lover.name}：{response.text.strip()}",
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
                    f"查验结果：{target.name} 属于【{faction}】。",
                    f"Inspection: {target.name} belongs to the {faction}.",
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
                            f"{player.name} 死亡，恋人 {partner.name} 随之殉情。",
                            f"{player.name} died; their Lover {partner.name} dies of heartbreak.",
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
                        self._with_role_reveal(
                            self._t(
                                f"{player.name} 发动猎人技能，{victim.name} 被带走。",
                                f"{player.name} uses the Hunter skill and takes down {victim.name}.",
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
            f"{player.name}={role_names[player.role]}" for player, _ in deaths
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
        heartbeat_stop = threading.Event()
        heartbeat: threading.Thread | None = None
        if self.config.spectator_progress:
            self.terminal.progress(self._spectator_action_text(player, request))
            if isinstance(player.controller, LLMController):
                heartbeat = threading.Thread(
                    target=self._spectator_heartbeat,
                    args=(heartbeat_stop,),
                    daemon=True,
                )
                heartbeat.start()
        try:
            response = player.controller.act(self._view(player), request)
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception as exc:
            if self.config.strict_controllers:
                msg = (
                    f"Controller failed for {player.name} during {request.kind.value}: "
                    f"{type(exc).__name__}: {exc}"
                )
                raise RuntimeError(msg) from exc
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
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1)
        legal = {option.value for option in request.options}
        if (
            request.options
            and response.choice not in legal
            and (response.choice is not None or not request.allow_abstain)
        ):
            if self.config.strict_controllers:
                msg = (
                    f"Controller returned an illegal choice for {player.name} "
                    f"during {request.kind.value}: {response.choice!r}"
                )
                raise RuntimeError(msg)
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

    def _spectator_heartbeat(self, stop: threading.Event) -> None:
        """Emit periodic safe liveness signals while one LLM request is pending."""
        while not stop.wait(12):
            self.terminal.progress(
                self._t(
                    "LLM 仍在进行 xhigh 推理……",
                    "The LLM is still reasoning at xhigh effort...",
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
                f"{player.name} 正在组织公开发言……",
                f"{player.name} is preparing a public statement...",
            )
        if request.kind is ActionKind.LAST_WORDS:
            return self._t(
                f"{player.name} 正在组织遗言……",
                f"{player.name} is preparing final words...",
            )
        if request.kind is ActionKind.VOTE:
            return self._t(
                f"{player.name} 正在提交公开投票……",
                f"{player.name} is submitting a public vote...",
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
        self.terminal.say(player.name, text)

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
            f"{player.name}={role_names[player.role]}" for player in self.players
        )
        lover_pair = [player.name for player in self.players if player.lover_id]
        lover_reveal = (
            self._t(
                f"恋人：{'、'.join(lover_pair)}。",
                f"Lovers: {' and '.join(lover_pair)}.",
            )
            if lover_pair
            else ""
        )
        winning_players = self._winning_players(winner)
        prize_shares = self._prize_shares(winning_players)
        winners_text = self._t(
            f"获胜玩家：{'、'.join(winning_players) if winning_players else '无'}。",
            f"Winning players: {', '.join(winning_players) if winning_players else 'none'}.",
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
        if self.config.memory_directory:
            self.export_memories(self.config.memory_directory)
        return GameResult(
            winner=winner,
            winning_players=winning_players,
            prize_shares=prize_shares,
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
