"""JSON configuration schema and validation."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import Role
from .skills import resolve_skills

SUPPORTED_LANGUAGES = {"zh-CN", "en"}
SUPPORTED_CONTROLLERS = {"human", "llm", "bot"}
SUPPORTED_WIRE_APIS = {"chat", "responses"}
ROLE_PRESET_SIZES: dict[str, int | None] = {
    "classic": None,
    "movie_basic": 10,
    "movie_crazy_fox": 12,
    "movie_prison_break": 12,
    "movie_lovers": 11,
    "movie_mad_land": 10,
}
MIN_PLAYERS = 6
MAX_PLAYERS = 16


@dataclass(frozen=True)
class LLMProviderConfig:
    """Connection details for an OpenAI-compatible chat-completions API."""

    base_url: str
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.7
    timeout: float = 120.0
    max_tokens: int = 700
    use_json_mode: bool = True
    wire_api: str = "chat"
    reasoning_effort: str | None = None
    force_ipv4: bool = False
    stream: bool = False
    extra_headers: dict[str, str] = field(default_factory=dict)

    def resolved_api_key(self) -> str | None:
        """Resolve an environment-backed key without mutating configuration."""
        if self.api_key_env:
            value = os.environ.get(self.api_key_env)
            if not value:
                msg = f"Environment variable {self.api_key_env!r} is not set"
                raise ValueError(msg)
            return value
        return self.api_key


@dataclass(frozen=True)
class PlayerConfig:
    """A seat and its controller-specific behavior settings."""

    name: str
    controller: str
    provider: str | None = None
    persona: str = ""
    skills: tuple[str, ...] = ("logic", "memory")
    fixed_role: Role | None = None


@dataclass(frozen=True)
class RuleConfig:
    """Supported house-rule switches."""

    max_days: int = 20
    wolf_chat_rounds: int = 1
    witch_can_self_save: bool = True
    witch_can_use_two_potions_same_night: bool = False
    reveal_roles_on_death: bool = False
    allow_self_vote: bool = False
    last_words: bool = True
    first_night_last_words: bool = True
    night_death_last_words: bool = False
    day_vote_last_words: bool = True
    hunter_shot_last_words: bool = False


@dataclass(frozen=True)
class GameConfig:
    """Complete application configuration."""

    language: str
    players: tuple[PlayerConfig, ...]
    providers: dict[str, LLMProviderConfig] = field(default_factory=dict)
    rules: RuleConfig = field(default_factory=RuleConfig)
    seed: int | None = None
    clear_screen: bool = True
    memory_directory: str | None = "game_memories"
    context_char_limit: int = 24000
    role_preset: str = "classic"
    spectator_progress: bool = False
    strict_controllers: bool = False
    controller_retries: int = 0
    public_transcript_path: str | None = None
    checkpoint_path: str | None = None


def _provider_from_dict(raw: dict[str, Any]) -> LLMProviderConfig:
    return LLMProviderConfig(
        base_url=str(raw["base_url"]),
        model=str(raw["model"]),
        api_key=raw.get("api_key"),
        api_key_env=raw.get("api_key_env"),
        temperature=float(raw.get("temperature", 0.7)),
        timeout=float(raw.get("timeout", 120.0)),
        max_tokens=int(raw.get("max_tokens", 700)),
        use_json_mode=bool(raw.get("use_json_mode", True)),
        wire_api=str(raw.get("wire_api", "chat")),
        reasoning_effort=raw.get("reasoning_effort"),
        force_ipv4=bool(raw.get("force_ipv4", False)),
        stream=bool(raw.get("stream", False)),
        extra_headers={str(k): str(v) for k, v in raw.get("extra_headers", {}).items()},
    )


def _player_from_dict(raw: dict[str, Any]) -> PlayerConfig:
    fixed_role = raw.get("fixed_role")
    return PlayerConfig(
        name=str(raw["name"]).strip(),
        controller=str(raw.get("controller", "llm")).lower(),
        provider=raw.get("provider"),
        persona=str(raw.get("persona", "")),
        skills=tuple(str(value) for value in raw.get("skills", ["logic", "memory"])),
        fixed_role=Role(fixed_role) if fixed_role else None,
    )


def load_config(path: str | Path) -> GameConfig:
    """Load and validate a UTF-8 JSON configuration file."""
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as file:
        raw = json.load(file)
    providers = {
        str(name): _provider_from_dict(value)
        for name, value in raw.get("providers", {}).items()
    }
    rules = RuleConfig(**raw.get("rules", {}))
    config = GameConfig(
        language=str(raw.get("language", "zh-CN")),
        players=tuple(_player_from_dict(player) for player in raw["players"]),
        providers=providers,
        rules=rules,
        seed=raw.get("seed"),
        clear_screen=bool(raw.get("clear_screen", True)),
        memory_directory=raw.get("memory_directory", "game_memories"),
        context_char_limit=int(raw.get("context_char_limit", 24000)),
        role_preset=str(raw.get("role_preset", "classic")),
        spectator_progress=bool(raw.get("spectator_progress", False)),
        strict_controllers=bool(raw.get("strict_controllers", False)),
        controller_retries=int(raw.get("controller_retries", 0)),
        public_transcript_path=(
            str(raw["public_transcript_path"])
            if raw.get("public_transcript_path") is not None
            else None
        ),
        checkpoint_path=(
            str(raw["checkpoint_path"])
            if raw.get("checkpoint_path") is not None
            else None
        ),
    )
    validate_config(config)
    return config


def validate_config(config: GameConfig) -> None:
    """Fail early on unsafe or ambiguous game configurations."""
    if config.language not in SUPPORTED_LANGUAGES:
        msg = f"Unsupported language {config.language!r}; choose one of {sorted(SUPPORTED_LANGUAGES)}"
        raise ValueError(msg)
    if config.role_preset not in ROLE_PRESET_SIZES:
        msg = f"Unsupported role_preset {config.role_preset!r}"
        raise ValueError(msg)
    if not MIN_PLAYERS <= len(config.players) <= MAX_PLAYERS:
        msg = f"The game supports {MIN_PLAYERS} to {MAX_PLAYERS} players"
        raise ValueError(msg)
    names = [player.name for player in config.players]
    if any(not name for name in names) or len(set(names)) != len(names):
        msg = "Player names must be non-empty and unique"
        raise ValueError(msg)
    fixed = [player.fixed_role for player in config.players if player.fixed_role]
    if fixed and len(fixed) != len(config.players):
        msg = "fixed_role must be specified for every player or for none of them"
        raise ValueError(msg)
    if not fixed:
        expected_size = ROLE_PRESET_SIZES[config.role_preset]
        if expected_size is not None and len(config.players) != expected_size:
            msg = f"role_preset {config.role_preset!r} requires {expected_size} players"
            raise ValueError(msg)
    else:
        if fixed.count(Role.SHARED) not in {0, 2}:
            msg = "A fixed role set must contain zero or two Shared Players"
            raise ValueError(msg)
        singleton_roles = {
            Role.SEER,
            Role.WITCH,
            Role.HUNTER,
            Role.MEDIUM,
            Role.BODYGUARD,
            Role.FOX,
            Role.CUPID,
        }
        duplicated = sorted(
            (role.value for role in singleton_roles if fixed.count(role) > 1),
        )
        if duplicated:
            msg = f"A fixed role set contains duplicate singleton roles: {', '.join(duplicated)}"
            raise ValueError(msg)
        if Role.FOX in fixed and Role.CUPID in fixed:
            msg = "Fox and Cupid endgames cannot be combined in one fixed role set"
            raise ValueError(msg)
    for player in config.players:
        if player.controller not in SUPPORTED_CONTROLLERS:
            msg = f"Unsupported controller {player.controller!r} for {player.name}"
            raise ValueError(msg)
        resolve_skills(list(player.skills))
        if player.controller == "llm" and (
            not player.provider or player.provider not in config.providers
        ):
            msg = f"LLM player {player.name!r} references an unknown provider"
            raise ValueError(msg)
    for name, provider in config.providers.items():
        if provider.wire_api not in SUPPORTED_WIRE_APIS:
            msg = f"Provider {name!r} uses unsupported wire_api {provider.wire_api!r}"
            raise ValueError(msg)
    if config.rules.max_days < 1 or config.rules.wolf_chat_rounds < 0:
        msg = "max_days must be positive and wolf_chat_rounds cannot be negative"
        raise ValueError(msg)
    if config.context_char_limit < 2000:
        msg = "context_char_limit must be at least 2000"
        raise ValueError(msg)
    if config.controller_retries < 0:
        msg = "controller_retries cannot be negative"
        raise ValueError(msg)


def example_config() -> dict[str, Any]:
    """Return a documented starting point with one human and seven LLMs."""
    players: list[dict[str, Any]] = [
        {
            "name": "你",
            "controller": "human",
            "persona": "认真但不失幽默的玩家",
            "skills": ["logic", "social", "memory"],
        },
    ]
    players.extend(
        {
            "name": f"智能体{index}",
            "controller": "llm",
            "provider": "default",
            "persona": "发言简洁、会根据局势动态调整判断",
            "skills": ["logic", "social", "memory"],
        }
        for index in range(1, 8)
    )
    return {
        "language": "zh-CN",
        "seed": None,
        "clear_screen": True,
        "memory_directory": "game_memories",
        "context_char_limit": 24000,
        "role_preset": "classic",
        "spectator_progress": False,
        "strict_controllers": False,
        "controller_retries": 0,
        "public_transcript_path": None,
        "checkpoint_path": None,
        "providers": {
            "default": {
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
                "model": "gpt-4.1-mini",
                "temperature": 0.7,
                "timeout": 120,
                "max_tokens": 700,
                "use_json_mode": True,
                "wire_api": "chat",
                "stream": False,
            },
        },
        "rules": asdict(RuleConfig()),
        "players": players,
    }


def write_example_config(path: str | Path, *, force: bool = False) -> Path:
    """Write an example without overwriting user data by default."""
    config_path = Path(path)
    if config_path.exists() and not force:
        msg = f"Configuration already exists: {config_path}"
        raise FileExistsError(msg)
    config_path.write_text(
        json.dumps(example_config(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return config_path


def demo_config(
    player_count: int = 8,
    seed: int | None = None,
    role_preset: str = "classic",
) -> GameConfig:
    """Build an offline all-bot configuration for smoke tests and demos."""
    players = tuple(
        PlayerConfig(
            name=f"玩家{index}",
            controller="bot",
            persona="本地规则机器人",
            skills=("logic", "memory"),
        )
        for index in range(1, player_count + 1)
    )
    config = GameConfig(
        language="zh-CN",
        players=players,
        seed=seed,
        clear_screen=False,
        memory_directory=None,
        role_preset=role_preset,
    )
    validate_config(config)
    return config
