"""Core value objects shared by the rules engine and player controllers.

The classes in this module deliberately separate a player's safe view from the
authoritative game state. Controllers never receive ``PlayerState`` objects for
other players, which makes accidental role leakage much harder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

T = TypeVar("T")


class Faction(str, Enum):
    """The two factions used by the supported classic rule set."""

    GOOD = "good"
    WEREWOLF = "werewolf"


class Role(str, Enum):
    """Playable roles."""

    VILLAGER = "villager"
    WEREWOLF = "werewolf"
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"

    @property
    def faction(self) -> Faction:
        """Return the faction that wins or loses together."""
        if self is Role.WEREWOLF:
            return Faction.WEREWOLF
        return Faction.GOOD


ROLE_NAMES: dict[str, dict[Role, str]] = {
    "zh-CN": {
        Role.VILLAGER: "平民",
        Role.WEREWOLF: "狼人",
        Role.SEER: "预言家",
        Role.WITCH: "女巫",
        Role.HUNTER: "猎人",
    },
    "en": {
        Role.VILLAGER: "Villager",
        Role.WEREWOLF: "Werewolf",
        Role.SEER: "Seer",
        Role.WITCH: "Witch",
        Role.HUNTER: "Hunter",
    },
}

ROLE_DESCRIPTIONS: dict[str, dict[Role, str]] = {
    "zh-CN": {
        Role.VILLAGER: "白天分析发言与投票，找出全部狼人。",
        Role.WEREWOLF: "夜间与队友私聊并共同袭击一名好人，白天隐藏身份。",
        Role.SEER: "每晚查验一名存活玩家，得知其阵营。",
        Role.WITCH: "整局各有一瓶解药和毒药；默认同一夜只能使用一瓶。",
        Role.HUNTER: "死亡时可开枪带走一名存活玩家，但被女巫毒死时不能开枪。",
    },
    "en": {
        Role.VILLAGER: "Analyze discussion and votes to eliminate every werewolf.",
        Role.WEREWOLF: "Chat privately and attack at night while hiding by day.",
        Role.SEER: "Inspect one living player each night to learn their faction.",
        Role.WITCH: "Has one antidote and one poison; only one may be used per night by default.",
        Role.HUNTER: "May shoot one living player when killed, except when poisoned by the Witch.",
    },
}


def localized(mapping: dict[str, T], language: str) -> T:
    """Look up a language entry with Chinese as the stable fallback."""
    return mapping.get(language, mapping["zh-CN"])


class Visibility(str, Enum):
    """Visibility labels attached to every memory event."""

    PUBLIC = "public"
    PRIVATE = "private"
    WEREWOLF = "werewolf"


@dataclass(frozen=True)
class MemoryEvent:
    """An observation that has already passed the information boundary."""

    sequence: int
    day: int
    phase: str
    text: str
    visibility: Visibility
    sender: str | None = None


@dataclass(frozen=True)
class Thought:
    """A controller-authored private strategy note."""

    day: int
    phase: str
    text: str


@dataclass
class PlayerMemory:
    """Long-lived, per-player observations and private strategy notes."""

    events: list[MemoryEvent] = field(default_factory=list)
    thoughts: list[Thought] = field(default_factory=list)

    def remember(self, event: MemoryEvent) -> None:
        """Append an event that was explicitly delivered to this player."""
        self.events.append(event)

    def reflect(self, day: int, phase: str, text: str) -> None:
        """Store a private thought without publishing it to another player."""
        clean = text.strip()
        if clean:
            self.thoughts.append(Thought(day=day, phase=phase, text=clean))


@dataclass(frozen=True)
class Skill:
    """Reusable behavioral guidance supplied only to one controller."""

    name: str
    description: str
    instructions: str


@dataclass
class PlayerState:
    """Authoritative state owned exclusively by the deterministic judge."""

    player_id: str
    name: str
    role: Role
    controller: object
    skills: tuple[Skill, ...]
    memory: PlayerMemory = field(default_factory=PlayerMemory)
    alive: bool = True


@dataclass(frozen=True)
class PlayerView:
    """Sanitized controller input containing no other player's secret state."""

    player_id: str
    name: str
    role: Role
    role_name: str
    role_description: str
    faction: Faction
    alive_players: tuple[tuple[str, str], ...]
    dead_players: tuple[tuple[str, str], ...]
    events: tuple[MemoryEvent, ...]
    thoughts: tuple[Thought, ...]
    skills: tuple[Skill, ...]
    day: int
    phase: str
    language: str


class ActionKind(str, Enum):
    """All actions a controller may be asked to perform."""

    SPEAK = "speak"
    LAST_WORDS = "last_words"
    TEAM_CHAT = "team_chat"
    VOTE = "vote"
    WOLF_KILL = "wolf_kill"
    SEER_INSPECT = "seer_inspect"
    WITCH_SAVE = "witch_save"
    WITCH_POISON = "witch_poison"
    HUNTER_SHOOT = "hunter_shoot"


@dataclass(frozen=True)
class ActionOption:
    """A legal opaque value and the label shown to a controller."""

    value: str
    label: str


@dataclass(frozen=True)
class ActionRequest:
    """A prompt plus the complete set of choices accepted by the judge."""

    kind: ActionKind
    prompt: str
    options: tuple[ActionOption, ...] = ()
    allow_abstain: bool = False


@dataclass(frozen=True)
class AgentResponse:
    """Controller output. ``thought`` and ``note`` are always private."""

    choice: str | None = None
    text: str = ""
    thought: str = ""
    note: str = ""
