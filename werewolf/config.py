"""JSON configuration schema and validation."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import Role
from .rendering import (
    MAX_PLAYER_NAME_BYTES,
    MAX_PLAYER_NAME_CHARS,
    contains_terminal_control,
)
from .skills import resolve_skills

SUPPORTED_LANGUAGES = {"zh-CN", "en"}
SUPPORTED_CONTROLLERS = {"human", "llm", "bot"}
SUPPORTED_WIRE_APIS = {"chat", "responses"}
SUPPORTED_PROMPT_CACHE_RETENTIONS = {"in-memory", "24h"}
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
RECOMMENDED_PUBLIC_TRANSCRIPT_PATH = "game_runs/public.log"
RECOMMENDED_CHECKPOINT_PATH = "game_runs/private.checkpoint.json"


@dataclass(frozen=True)
class LLMProviderConfig:
    """Connection details for an OpenAI-compatible inference API."""

    base_url: str
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.7
    timeout: float = 120.0
    max_tokens: int = 2000
    use_json_mode: bool = True
    wire_api: str = "chat"
    reasoning_effort: str | None = None
    force_ipv4: bool = False
    stream: bool = True
    prompt_cache: bool = False
    prompt_cache_retention: str | None = None
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
    randomize_discussion_start: bool = True
    randomize_seating: bool = True


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
    roles: tuple[Role, ...] | None = None
    spectator_progress: bool = True
    strict_controllers: bool = True
    controller_retries: int = 2
    public_transcript_path: str | None = None
    checkpoint_path: str | None = None
    human_strategy_notes: bool = False
    confirm_critical_actions: bool = True
    parallel_llm_votes: bool = True
    max_parallel_llm_requests: int = 4


def _object(value: object, path: str) -> dict[str, Any]:
    """Require one JSON object and retain its path for actionable errors."""
    if not isinstance(value, dict):
        msg = f"{path} must be a JSON object"
        raise TypeError(msg)
    return value


def _reject_unknown(raw: dict[str, Any], allowed: set[str], path: str) -> None:
    """Reject misspelled fields instead of silently falling back to defaults."""
    unknown = sorted(set(raw) - allowed)
    if unknown:
        field_path = f"{path}.{unknown[0]}" if path else unknown[0]
        msg = f"Unknown configuration field: {field_path}"
        raise ValueError(msg)


def _string(value: object, path: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        msg = f"{path} must be a string{' or null' if allow_none else ''}"
        raise TypeError(msg)
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        msg = f"{path} must be a JSON boolean"
        raise TypeError(msg)
    return value


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{path} must be an integer"
        raise TypeError(msg)
    return value


def _number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{path} must be a finite number"
        raise TypeError(msg)
    number = float(value)
    if not math.isfinite(number):
        msg = f"{path} must be a finite number"
        raise ValueError(msg)
    return number


def _required(raw: dict[str, Any], field_name: str, path: str) -> object:
    if field_name not in raw:
        msg = f"{path}.{field_name} is required"
        raise ValueError(msg)
    return raw[field_name]


def _provider_from_dict(raw: object, *, path: str) -> LLMProviderConfig:
    raw = _object(raw, path)
    _reject_unknown(raw, set(LLMProviderConfig.__dataclass_fields__), path)
    extra_headers = _object(raw.get("extra_headers", {}), f"{path}.extra_headers")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in extra_headers.items()
    ):
        msg = f"{path}.extra_headers keys and values must be strings"
        raise TypeError(msg)
    return LLMProviderConfig(
        base_url=_string(_required(raw, "base_url", path), f"{path}.base_url") or "",
        model=_string(_required(raw, "model", path), f"{path}.model") or "",
        api_key=_string(raw.get("api_key"), f"{path}.api_key", allow_none=True),
        api_key_env=_string(
            raw.get("api_key_env"),
            f"{path}.api_key_env",
            allow_none=True,
        ),
        temperature=_number(raw.get("temperature", 0.7), f"{path}.temperature"),
        timeout=_number(raw.get("timeout", 120.0), f"{path}.timeout"),
        max_tokens=_integer(raw.get("max_tokens", 2000), f"{path}.max_tokens"),
        use_json_mode=_boolean(
            raw.get("use_json_mode", True),
            f"{path}.use_json_mode",
        ),
        wire_api=_string(raw.get("wire_api", "chat"), f"{path}.wire_api") or "",
        reasoning_effort=_string(
            raw.get("reasoning_effort"),
            f"{path}.reasoning_effort",
            allow_none=True,
        ),
        force_ipv4=_boolean(raw.get("force_ipv4", False), f"{path}.force_ipv4"),
        stream=_boolean(raw.get("stream", True), f"{path}.stream"),
        prompt_cache=_boolean(
            raw.get("prompt_cache", False),
            f"{path}.prompt_cache",
        ),
        prompt_cache_retention=_string(
            raw.get("prompt_cache_retention"),
            f"{path}.prompt_cache_retention",
            allow_none=True,
        ),
        extra_headers=dict(extra_headers),
    )


def _player_from_dict(
    raw: dict[str, Any],
    *,
    default_provider: str | None,
    path: str,
) -> PlayerConfig:
    _reject_unknown(raw, set(PlayerConfig.__dataclass_fields__), path)
    fixed_role = raw.get("fixed_role")
    if fixed_role is not None and not isinstance(fixed_role, str):
        msg = f"{path}.fixed_role must be a role string or null"
        raise TypeError(msg)
    controller_value = _string(
        raw.get("controller", "llm"),
        f"{path}.controller",
    )
    controller = (controller_value or "").lower()
    provider = _string(raw.get("provider"), f"{path}.provider", allow_none=True)
    if controller == "llm" and provider is None:
        provider = default_provider
    skills = raw.get("skills", ["logic", "memory"])
    if not isinstance(skills, list) or not all(
        isinstance(value, str) for value in skills
    ):
        msg = f"{path}.skills must be an array of strings"
        raise TypeError(msg)
    return PlayerConfig(
        name=(_string(_required(raw, "name", path), f"{path}.name") or "").strip(),
        controller=controller,
        provider=provider,
        persona=_string(raw.get("persona", ""), f"{path}.persona") or "",
        skills=tuple(skills),
        fixed_role=Role(fixed_role) if fixed_role else None,
    )


def _player_from_value(
    raw: object,
    *,
    default_provider: str | None,
    path: str,
) -> PlayerConfig:
    """Expand a player name shorthand or parse a full player object."""
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        msg = f"{path} must be a name string or a JSON object"
        raise TypeError(msg)
    return _player_from_dict(raw, default_provider=default_provider, path=path)


def _rules_from_value(raw: object) -> RuleConfig:
    path = "rules"
    values = _object(raw, path)
    _reject_unknown(values, set(RuleConfig.__dataclass_fields__), path)
    defaults = RuleConfig()
    return RuleConfig(
        max_days=_integer(values.get("max_days", defaults.max_days), "rules.max_days"),
        wolf_chat_rounds=_integer(
            values.get("wolf_chat_rounds", defaults.wolf_chat_rounds),
            "rules.wolf_chat_rounds",
        ),
        witch_can_self_save=_boolean(
            values.get("witch_can_self_save", defaults.witch_can_self_save),
            "rules.witch_can_self_save",
        ),
        witch_can_use_two_potions_same_night=_boolean(
            values.get(
                "witch_can_use_two_potions_same_night",
                defaults.witch_can_use_two_potions_same_night,
            ),
            "rules.witch_can_use_two_potions_same_night",
        ),
        reveal_roles_on_death=_boolean(
            values.get("reveal_roles_on_death", defaults.reveal_roles_on_death),
            "rules.reveal_roles_on_death",
        ),
        allow_self_vote=_boolean(
            values.get("allow_self_vote", defaults.allow_self_vote),
            "rules.allow_self_vote",
        ),
        last_words=_boolean(
            values.get("last_words", defaults.last_words),
            "rules.last_words",
        ),
        first_night_last_words=_boolean(
            values.get("first_night_last_words", defaults.first_night_last_words),
            "rules.first_night_last_words",
        ),
        night_death_last_words=_boolean(
            values.get("night_death_last_words", defaults.night_death_last_words),
            "rules.night_death_last_words",
        ),
        day_vote_last_words=_boolean(
            values.get("day_vote_last_words", defaults.day_vote_last_words),
            "rules.day_vote_last_words",
        ),
        hunter_shot_last_words=_boolean(
            values.get("hunter_shot_last_words", defaults.hunter_shot_last_words),
            "rules.hunter_shot_last_words",
        ),
        randomize_discussion_start=_boolean(
            values.get(
                "randomize_discussion_start",
                defaults.randomize_discussion_start,
            ),
            "rules.randomize_discussion_start",
        ),
        randomize_seating=_boolean(
            values.get("randomize_seating", defaults.randomize_seating),
            "rules.randomize_seating",
        ),
    )


def _roles_from_value(raw: object) -> tuple[Role, ...] | None:
    """Parse an optional custom shuffled deck from role counts or a role list."""
    if raw is None:
        return None
    if isinstance(raw, list):
        if not all(isinstance(value, str) for value in raw):
            msg = "roles list values must be role-name strings"
            raise TypeError(msg)
        return tuple(Role(value) for value in raw)
    if not isinstance(raw, dict):
        msg = "roles must be an object of role counts or a list of role names"
        raise TypeError(msg)
    unknown = sorted(set(raw) - {role.value for role in Role})
    if unknown:
        msg = f"Unknown roles: {', '.join(str(value) for value in unknown)}"
        raise ValueError(msg)
    roles: list[Role] = []
    for role in Role:
        count = raw.get(role.value, 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            msg = f"Role count for {role.value!r} must be a non-negative integer"
            raise ValueError(msg)
        roles.extend([role] * count)
    return tuple(roles)


def _default_provider_name(
    providers: dict[str, LLMProviderConfig],
) -> str | None:
    """Choose the conventional or sole provider for concise player entries."""
    if "default" in providers:
        return "default"
    if len(providers) == 1:
        return next(iter(providers))
    return None


def load_config(path: str | Path) -> GameConfig:
    """Load and validate a UTF-8 JSON configuration file."""
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as file:
        raw = json.load(file)
    raw = _object(raw, "config")
    _reject_unknown(raw, set(GameConfig.__dataclass_fields__), "")
    providers_raw = _object(raw.get("providers", {}), "providers")
    providers = {
        name: _provider_from_dict(value, path=f"providers.{name}")
        for name, value in providers_raw.items()
    }
    default_provider = _default_provider_name(providers)
    rules = _rules_from_value(raw.get("rules", {}))
    players_raw = raw.get("players")
    if not isinstance(players_raw, list):
        msg = "players must be a JSON array"
        raise TypeError(msg)
    public_transcript_path = raw.get(
        "public_transcript_path",
        RECOMMENDED_PUBLIC_TRANSCRIPT_PATH,
    )
    checkpoint_path = raw.get("checkpoint_path", RECOMMENDED_CHECKPOINT_PATH)
    seed = raw.get("seed")
    if seed is not None:
        seed = _integer(seed, "seed")
    config = GameConfig(
        language=_string(raw.get("language", "zh-CN"), "language") or "",
        players=tuple(
            _player_from_value(
                player,
                default_provider=default_provider,
                path=f"players[{index}]",
            )
            for index, player in enumerate(players_raw)
        ),
        providers=providers,
        rules=rules,
        seed=seed,
        clear_screen=_boolean(raw.get("clear_screen", True), "clear_screen"),
        memory_directory=_string(
            raw.get("memory_directory", "game_memories"),
            "memory_directory",
            allow_none=True,
        ),
        context_char_limit=_integer(
            raw.get("context_char_limit", 24000),
            "context_char_limit",
        ),
        role_preset=_string(raw.get("role_preset", "classic"), "role_preset") or "",
        roles=_roles_from_value(raw.get("roles")),
        spectator_progress=_boolean(
            raw.get("spectator_progress", True),
            "spectator_progress",
        ),
        strict_controllers=_boolean(
            raw.get("strict_controllers", True),
            "strict_controllers",
        ),
        controller_retries=_integer(
            raw.get("controller_retries", 2),
            "controller_retries",
        ),
        public_transcript_path=_string(
            public_transcript_path,
            "public_transcript_path",
            allow_none=True,
        ),
        checkpoint_path=_string(
            checkpoint_path,
            "checkpoint_path",
            allow_none=True,
        ),
        human_strategy_notes=_boolean(
            raw.get("human_strategy_notes", False),
            "human_strategy_notes",
        ),
        confirm_critical_actions=_boolean(
            raw.get("confirm_critical_actions", True),
            "confirm_critical_actions",
        ),
        parallel_llm_votes=_boolean(
            raw.get("parallel_llm_votes", True),
            "parallel_llm_votes",
        ),
        max_parallel_llm_requests=_integer(
            raw.get("max_parallel_llm_requests", 4),
            "max_parallel_llm_requests",
        ),
    )
    validate_config(config)
    return config


def validate_config(config: GameConfig) -> None:
    """Fail early on unsafe or ambiguous game configurations."""
    _validate_runtime_schema(config)
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
    if any(contains_terminal_control(name) for name in names):
        msg = "Player names cannot contain terminal control characters or whitespace controls"
        raise ValueError(msg)
    if any(
        len(name) > MAX_PLAYER_NAME_CHARS
        or len(name.encode("utf-8")) > MAX_PLAYER_NAME_BYTES
        for name in names
    ):
        msg = (
            f"Player names must be at most {MAX_PLAYER_NAME_CHARS} characters and "
            f"{MAX_PLAYER_NAME_BYTES} UTF-8 bytes"
        )
        raise ValueError(msg)
    fixed = [player.fixed_role for player in config.players if player.fixed_role]
    if config.roles is not None:
        if len(config.roles) != len(config.players):
            msg = "The custom roles deck must match the number of players"
            raise ValueError(msg)
        _validate_role_set(config.roles, label="custom roles deck")
        available = Counter(config.roles)
        for role in fixed:
            available[role] -= 1
            if available[role] < 0:
                msg = f"fixed_role {role.value!r} exceeds the custom roles deck"
                raise ValueError(msg)
    elif len(fixed) != len(config.players):
        expected_size = ROLE_PRESET_SIZES[config.role_preset]
        if expected_size is not None and len(config.players) != expected_size:
            msg = f"role_preset {config.role_preset!r} requires {expected_size} players"
            raise ValueError(msg)
    else:
        _validate_role_set(tuple(fixed), label="fixed role set")
    if fixed:
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
        if provider.prompt_cache and provider.wire_api != "responses":
            msg = (
                f"Provider {name!r} enables prompt_cache, which currently requires "
                "wire_api='responses'"
            )
            raise ValueError(msg)
        if (
            provider.prompt_cache_retention is not None
            and provider.prompt_cache_retention not in SUPPORTED_PROMPT_CACHE_RETENTIONS
        ):
            msg = (
                f"Provider {name!r} uses unsupported prompt_cache_retention "
                f"{provider.prompt_cache_retention!r}"
            )
            raise ValueError(msg)
        if provider.prompt_cache_retention and not provider.prompt_cache:
            msg = (
                f"Provider {name!r} sets prompt_cache_retention without enabling "
                "prompt_cache"
            )
            raise ValueError(msg)
        if not provider.base_url or not provider.model:
            msg = f"Provider {name!r} requires non-empty base_url and model"
            raise ValueError(msg)
        if provider.temperature < 0:
            msg = f"Provider {name!r} temperature cannot be negative"
            raise ValueError(msg)
        if provider.timeout <= 0 or provider.max_tokens <= 0:
            msg = f"Provider {name!r} timeout and max_tokens must be positive"
            raise ValueError(msg)
    if config.rules.max_days < 1 or config.rules.wolf_chat_rounds < 0:
        msg = "max_days must be positive and wolf_chat_rounds cannot be negative"
        raise ValueError(msg)


def _validate_runtime_schema(config: GameConfig) -> None:
    """Apply the JSON schema guarantees to programmatically built configs too."""
    if not isinstance(config, GameConfig):
        msg = "config must be a GameConfig"
        raise TypeError(msg)
    _string(config.language, "language")
    _string(config.role_preset, "role_preset")
    for field_name in (
        "clear_screen",
        "spectator_progress",
        "strict_controllers",
        "human_strategy_notes",
        "confirm_critical_actions",
        "parallel_llm_votes",
    ):
        _boolean(getattr(config, field_name), field_name)
    _integer(config.context_char_limit, "context_char_limit")
    _integer(config.controller_retries, "controller_retries")
    _integer(config.max_parallel_llm_requests, "max_parallel_llm_requests")
    if config.seed is not None:
        _integer(config.seed, "seed")
    for field_name in (
        "memory_directory",
        "public_transcript_path",
        "checkpoint_path",
    ):
        _string(getattr(config, field_name), field_name, allow_none=True)
    if not isinstance(config.players, tuple) or not all(
        isinstance(player, PlayerConfig) for player in config.players
    ):
        msg = "players must be a tuple of PlayerConfig values"
        raise TypeError(msg)
    if not isinstance(config.providers, dict) or not all(
        isinstance(name, str) and isinstance(provider, LLMProviderConfig)
        for name, provider in config.providers.items()
    ):
        msg = "providers must map strings to LLMProviderConfig values"
        raise TypeError(msg)
    if not isinstance(config.rules, RuleConfig):
        msg = "rules must be a RuleConfig"
        raise TypeError(msg)
    if config.roles is not None and (
        not isinstance(config.roles, tuple)
        or not all(isinstance(role, Role) for role in config.roles)
    ):
        msg = "roles must be a tuple of Role values or null"
        raise TypeError(msg)
    for index, player in enumerate(config.players):
        path = f"players[{index}]"
        _string(player.name, f"{path}.name")
        _string(player.controller, f"{path}.controller")
        _string(player.provider, f"{path}.provider", allow_none=True)
        _string(player.persona, f"{path}.persona")
        if not isinstance(player.skills, tuple) or not all(
            isinstance(skill, str) for skill in player.skills
        ):
            msg = f"{path}.skills must be a tuple of strings"
            raise TypeError(msg)
        if player.fixed_role is not None and not isinstance(player.fixed_role, Role):
            msg = f"{path}.fixed_role must be a Role or null"
            raise TypeError(msg)
    for name, provider in config.providers.items():
        path = f"providers.{name}"
        for field_name in ("base_url", "model", "wire_api"):
            _string(getattr(provider, field_name), f"{path}.{field_name}")
        for field_name in (
            "api_key",
            "api_key_env",
            "reasoning_effort",
            "prompt_cache_retention",
        ):
            _string(
                getattr(provider, field_name), f"{path}.{field_name}", allow_none=True
            )
        _number(provider.temperature, f"{path}.temperature")
        _number(provider.timeout, f"{path}.timeout")
        _integer(provider.max_tokens, f"{path}.max_tokens")
        for field_name in (
            "use_json_mode",
            "force_ipv4",
            "stream",
            "prompt_cache",
        ):
            _boolean(getattr(provider, field_name), f"{path}.{field_name}")
        if not isinstance(provider.extra_headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in provider.extra_headers.items()
        ):
            msg = f"{path}.extra_headers must map strings to strings"
            raise TypeError(msg)
    for field_name in RuleConfig.__dataclass_fields__:
        value = getattr(config.rules, field_name)
        if field_name in {"max_days", "wolf_chat_rounds"}:
            _integer(value, f"rules.{field_name}")
        else:
            _boolean(value, f"rules.{field_name}")
    if config.context_char_limit < 2000:
        msg = "context_char_limit must be at least 2000"
        raise ValueError(msg)
    if config.controller_retries < 0:
        msg = "controller_retries cannot be negative"
        raise ValueError(msg)
    if not 1 <= config.max_parallel_llm_requests <= MAX_PLAYERS:
        msg = f"max_parallel_llm_requests must be between 1 and {MAX_PLAYERS}"
        raise ValueError(msg)
    outputs = {
        label: Path(value).resolve()
        for label, value in (
            ("checkpoint_path", config.checkpoint_path),
            ("public_transcript_path", config.public_transcript_path),
            ("memory_directory", config.memory_directory),
        )
        if value is not None
    }
    if len(set(outputs.values())) != len(outputs):
        collisions = ", ".join(f"{label}={path}" for label, path in outputs.items())
        msg = f"Game output paths must be distinct after resolution: {collisions}"
        raise ValueError(msg)


def _validate_role_set(roles: tuple[Role, ...], *, label: str) -> None:
    """Validate invariants that apply to a complete custom or fixed deck."""
    wolves = roles.count(Role.WEREWOLF)
    if wolves < 1 or wolves >= len(roles):
        msg = f"The {label} must contain at least one Werewolf and one non-Werewolf"
        raise ValueError(msg)
    if roles.count(Role.SHARED) not in {0, 2}:
        msg = f"The {label} must contain zero or two Shared Players"
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
    duplicated = sorted(role.value for role in singleton_roles if roles.count(role) > 1)
    if duplicated:
        msg = f"The {label} contains duplicate singleton roles: {', '.join(duplicated)}"
        raise ValueError(msg)
    if Role.FOX in roles and Role.CUPID in roles:
        msg = f"The {label} cannot combine Fox and Cupid endgames"
        raise ValueError(msg)


def recommended_config() -> dict[str, Any]:
    """Return the concise configuration written by the default init command."""
    return {
        "providers": {
            "default": {
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
                "model": "your-model-id",
                "wire_api": "responses",
                "reasoning_effort": "high",
            },
        },
        "players": [
            {"name": "真人玩家", "controller": "human"},
            *(f"智能体{index}" for index in range(1, 8)),
        ],
    }


def example_config() -> dict[str, Any]:
    """Return the exhaustive reference template retained for advanced users."""
    players: list[dict[str, Any]] = [
        {
            "name": "真人玩家",
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
        "roles": None,
        "spectator_progress": True,
        "strict_controllers": True,
        "controller_retries": 2,
        "public_transcript_path": "game_runs/public.log",
        "checkpoint_path": "game_runs/private.checkpoint.json",
        "human_strategy_notes": False,
        "confirm_critical_actions": True,
        "parallel_llm_votes": True,
        "max_parallel_llm_requests": 4,
        "providers": {
            "default": {
                "base_url": "https://api.openai.com/v1",
                "api_key": None,
                "api_key_env": "OPENAI_API_KEY",
                "model": "your-model-id",
                "temperature": 0.7,
                "timeout": 120,
                "max_tokens": 2000,
                "use_json_mode": True,
                "wire_api": "responses",
                "reasoning_effort": "high",
                "force_ipv4": False,
                "stream": True,
                "prompt_cache": False,
                "prompt_cache_retention": None,
                "extra_headers": {},
            },
        },
        "rules": asdict(RuleConfig()),
        "players": players,
    }


def write_example_config(
    path: str | Path,
    *,
    force: bool = False,
    full: bool = False,
) -> Path:
    """Write an example without overwriting user data by default."""
    config_path = Path(path)
    if config_path.exists() and not force:
        msg = f"Configuration already exists: {config_path}"
        raise FileExistsError(msg)
    payload = example_config() if full else recommended_config()
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return config_path


def config_to_dict(config: GameConfig) -> dict[str, Any]:
    """Return a complete JSON-compatible representation of a validated config.

    The interactive configurator deliberately writes the complete schema.  This
    makes its output predictable to inspect by hand while preserving advanced
    values loaded from an existing file, including per-seat personas and skills.
    """
    validate_config(config)
    roles = None
    if config.roles is not None:
        roles = [role.value for role in config.roles]
    return {
        "language": config.language,
        "seed": config.seed,
        "clear_screen": config.clear_screen,
        "memory_directory": config.memory_directory,
        "context_char_limit": config.context_char_limit,
        "role_preset": config.role_preset,
        "roles": roles,
        "spectator_progress": config.spectator_progress,
        "strict_controllers": config.strict_controllers,
        "controller_retries": config.controller_retries,
        "public_transcript_path": config.public_transcript_path,
        "checkpoint_path": config.checkpoint_path,
        "human_strategy_notes": config.human_strategy_notes,
        "confirm_critical_actions": config.confirm_critical_actions,
        "parallel_llm_votes": config.parallel_llm_votes,
        "max_parallel_llm_requests": config.max_parallel_llm_requests,
        "providers": {
            name: asdict(provider) for name, provider in config.providers.items()
        },
        "rules": asdict(config.rules),
        "players": [
            {
                "name": player.name,
                "controller": player.controller,
                "provider": player.provider,
                "persona": player.persona,
                "skills": list(player.skills),
                "fixed_role": (
                    player.fixed_role.value if player.fixed_role is not None else None
                ),
            }
            for player in config.players
        ],
    }


def write_config(
    config: GameConfig,
    path: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically persist a validated configuration as private UTF-8 JSON.

    Configuration files can contain provider headers or legacy inline keys, so
    newly written files use owner-only permissions.  Replacing the destination
    only after a complete temporary write also prevents a terminal interruption
    from leaving behind truncated JSON.
    """
    config_path = Path(path)
    if config_path.exists() and not overwrite:
        msg = f"Configuration already exists: {config_path}"
        raise FileExistsError(msg)
    payload = (
        json.dumps(
            config_to_dict(config),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        temporary_path.chmod(0o600)
        temporary_path.replace(config_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
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
        spectator_progress=False,
    )
    validate_config(config)
    return config
