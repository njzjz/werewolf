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
    """Primary and conditional winning factions."""

    GOOD = "good"
    WEREWOLF = "werewolf"
    FOX = "fox"
    LOVERS = "lovers"


class Role(str, Enum):
    """Playable roles."""

    VILLAGER = "villager"
    WEREWOLF = "werewolf"
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"
    MEDIUM = "medium"
    BODYGUARD = "bodyguard"
    MADMAN = "madman"
    FOX = "fox"
    CUPID = "cupid"
    SHARED = "shared"

    @property
    def faction(self) -> Faction:
        """Return the faction that wins or loses together."""
        if self in {Role.WEREWOLF, Role.MADMAN}:
            return Faction.WEREWOLF
        if self is Role.FOX:
            return Faction.FOX
        if self is Role.CUPID:
            return Faction.LOVERS
        return Faction.GOOD

    @property
    def appears_werewolf(self) -> bool:
        """Return whether Seer/Medium information reports werewolf alignment."""
        return self is Role.WEREWOLF


ROLE_NAMES: dict[str, dict[Role, str]] = {
    "zh-CN": {
        Role.VILLAGER: "平民",
        Role.WEREWOLF: "狼人",
        Role.SEER: "预言家",
        Role.WITCH: "女巫",
        Role.HUNTER: "猎人",
        Role.MEDIUM: "灵媒师",
        Role.BODYGUARD: "保镖",
        Role.MADMAN: "狂人",
        Role.FOX: "妖狐",
        Role.CUPID: "丘比特",
        Role.SHARED: "共有者",
    },
    "en": {
        Role.VILLAGER: "Villager",
        Role.WEREWOLF: "Werewolf",
        Role.SEER: "Seer",
        Role.WITCH: "Witch",
        Role.HUNTER: "Hunter",
        Role.MEDIUM: "Medium",
        Role.BODYGUARD: "Bodyguard",
        Role.MADMAN: "Madman",
        Role.FOX: "Fox",
        Role.CUPID: "Cupid",
        Role.SHARED: "Shared Player",
    },
}

ROLE_DESCRIPTIONS: dict[str, dict[Role, str]] = {
    "zh-CN": {
        Role.VILLAGER: "白天分析发言与投票，找出全部狼人。",
        Role.WEREWOLF: "夜间与队友私聊并共同袭击一名好人，白天隐藏身份。",
        Role.SEER: "每晚查验一名存活玩家，得知其显示为狼人侧还是村人侧。",
        Role.WITCH: "整局各有一瓶解药和毒药；默认同一夜只能使用一瓶。",
        Role.HUNTER: "死亡时可开枪带走一名存活玩家，但被女巫毒死时不能开枪。",
        Role.MEDIUM: "每晚得知前一天被投票放逐者显示为狼人侧还是村人侧。",
        Role.BODYGUARD: "每晚保护一名其他玩家，使其免受当晚狼人袭击。",
        Role.MADMAN: "没有夜间能力且不进入狼聊；狼人达成胜利且你本人存活时获胜，查验显示村人侧。",
        Role.FOX: "狼人袭击无法杀死你；被预言家查验会死亡，基础游戏结束时存活则独自获胜。",
        Role.CUPID: "开局指定两名恋人；恋人均存活会触发独占结算，你本人也须存活才能获胜。",
        Role.SHARED: "开局得知另一名共有者；你们属于村人侧，没有额外夜间能力。",
    },
    "en": {
        Role.VILLAGER: "Analyze discussion and votes to eliminate every werewolf.",
        Role.WEREWOLF: "Chat privately and attack at night while hiding by day.",
        Role.SEER: "Inspect one living player each night to learn whether they appear werewolf-side or village-side.",
        Role.WITCH: "Has one antidote and one poison; only one may be used per night by default.",
        Role.HUNTER: "May shoot one living player when killed, except when poisoned by the Witch.",
        Role.MEDIUM: "Learns whether the previous day's exile appeared werewolf-side or village-side.",
        Role.BODYGUARD: "Protects one other player from the werewolf attack each night.",
        Role.MADMAN: "Has no night action or wolf chat; wins if alive when werewolves prevail and appears village-side.",
        Role.FOX: "Survives werewolf attacks, dies when inspected, and wins alone if alive at game end.",
        Role.CUPID: "Links two Lovers; their survival triggers the exclusive result, but Cupid must also survive to win.",
        Role.SHARED: "Knows the other Shared Player; belongs to the good faction with no night action.",
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
    LOVERS = "lovers"


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
    lover_id: str | None = None


@dataclass(frozen=True)
class PlayerView:
    """Sanitized controller input containing no other player's secret state."""

    player_id: str
    name: str
    role: Role
    role_name: str
    role_description: str
    faction: Faction
    lover: tuple[str, str] | None
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
    BODYGUARD_PROTECT = "bodyguard_protect"
    CUPID_LINK = "cupid_link"
    LOVER_CHAT = "lover_chat"


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
