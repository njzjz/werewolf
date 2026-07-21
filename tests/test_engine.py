"""Rules-engine integration tests."""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from werewolf.agents import LLMController, OpenAICompatibleClient, Terminal
from werewolf.config import (
    GameConfig,
    LLMProviderConfig,
    PlayerConfig,
    RuleConfig,
    demo_config,
    example_config,
    load_config,
)
from werewolf.engine import DeathCause, Game, role_deck
from werewolf.models import (
    ActionKind,
    ActionOption,
    ActionRequest,
    AgentResponse,
    Faction,
    Role,
    Visibility,
)


class SilentTerminal(Terminal):
    """Suppress judge announcements during tests."""

    def __init__(self) -> None:
        super().__init__(clear_screen=False)

    def announce(self, text: str) -> None:
        """Discard public output; the boundary still records it."""


class CapturingTerminal(SilentTerminal):
    """Capture spectator progress for privacy-focused assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.progress_events: list[str] = []

    def progress(self, text: str) -> None:
        """Record progress without writing test output."""
        self.progress_events.append(text)


class ScriptedController:
    """Return queued choices while recording the judge's sanitized requests."""

    def __init__(
        self,
        responses: dict[ActionKind, list[AgentResponse]] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.requests = []
        self.views = []

    def act(self, view, request):
        """Use a scripted answer, then a deterministic legal fallback."""
        self.views.append(view)
        self.requests.append(request)
        queued = self.responses.get(request.kind, [])
        if queued:
            return queued.pop(0)
        if request.kind in {
            ActionKind.SPEAK,
            ActionKind.LAST_WORDS,
            ActionKind.TEAM_CHAT,
            ActionKind.LOVER_CHAT,
        }:
            return AgentResponse(text="")
        return AgentResponse(
            choice=request.options[0].value if request.options else None,
        )


class FailingController:
    """Controller used to verify fail-fast all-LLM simulations."""

    def act(self, _view, _request):
        """Raise instead of returning an action."""
        msg = "simulated provider outage"
        raise RuntimeError(msg)


class FlakyController:
    """Fail once before returning a valid LLM-like response."""

    def __init__(self) -> None:
        self.calls = 0

    def act(self, _view, _request):
        """Return a response after one transient failure."""
        self.calls += 1
        if self.calls == 1:
            msg = "transient gateway timeout"
            raise RuntimeError(msg)
        return AgentResponse(text="重试成功", thought="保留私密判断")


class BarrierVoteController:
    """Require two vote calls to overlap, proving the engine uses concurrency."""

    def __init__(self, barrier: threading.Barrier) -> None:
        self.barrier = barrier

    def act(self, _view, request):
        """Wait for the peer vote before choosing the first legal target."""
        self.barrier.wait()
        return AgentResponse(choice=request.options[0].value)


@pytest.mark.parametrize(
    ("count", "wolves", "hunters"),
    [(6, 2, 0), (8, 2, 1), (9, 3, 1), (12, 4, 1), (16, 5, 1)],
)
def test_role_deck_is_balanced(count: int, wolves: int, hunters: int) -> None:
    """Classic decks include the expected hostile and special roles."""
    deck = role_deck(count)
    assert len(deck) == count
    assert deck.count(Role.WEREWOLF) == wolves
    assert deck.count(Role.SEER) == 1
    assert deck.count(Role.WITCH) == 1
    assert deck.count(Role.HUNTER) == hunters


def fixed_config() -> GameConfig:
    """Return a deterministic six-player role assignment."""
    roles = [
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.SEER,
        Role.WITCH,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    return fixed_role_config(roles)


def fixed_role_config(
    roles: list[Role],
    role_preset: str = "classic",
) -> GameConfig:
    """Build a fixed-role bot table for focused role-resolution tests."""
    return GameConfig(
        language="zh-CN",
        players=tuple(
            PlayerConfig(
                name=f"玩家{index}",
                controller="bot",
                fixed_role=role,
            )
            for index, role in enumerate(roles, start=1)
        ),
        seed=1,
        clear_screen=False,
        memory_directory=None,
        spectator_progress=False,
        controller_retries=0,
        rules=RuleConfig(max_days=5, randomize_seating=False),
        role_preset=role_preset,
    )


MOVIE_PRESETS = {
    "movie_basic": Counter(
        {
            Role.WEREWOLF: 2,
            Role.VILLAGER: 6,
            Role.SEER: 1,
            Role.BODYGUARD: 1,
        },
    ),
    "movie_crazy_fox": Counter(
        {
            Role.WEREWOLF: 3,
            Role.VILLAGER: 5,
            Role.SEER: 1,
            Role.MEDIUM: 1,
            Role.BODYGUARD: 1,
            Role.FOX: 1,
        },
    ),
    "movie_prison_break": Counter(
        {
            Role.WEREWOLF: 3,
            Role.VILLAGER: 3,
            Role.SEER: 1,
            Role.MEDIUM: 1,
            Role.BODYGUARD: 1,
            Role.SHARED: 2,
            Role.MADMAN: 1,
        },
    ),
    "movie_lovers": Counter(
        {
            Role.WEREWOLF: 2,
            Role.VILLAGER: 5,
            Role.SEER: 1,
            Role.MEDIUM: 1,
            Role.BODYGUARD: 1,
            Role.CUPID: 1,
        },
    ),
    "movie_mad_land": Counter(
        {
            Role.WEREWOLF: 1,
            Role.MADMAN: 7,
            Role.SEER: 1,
            Role.BODYGUARD: 1,
        },
    ),
}


@pytest.mark.parametrize(("preset", "expected"), MOVIE_PRESETS.items())
def test_movie_role_decks_match_the_film_compositions(
    preset: str,
    expected: Counter[Role],
) -> None:
    """Each named film preset should produce its exact advertised role list."""
    deck = role_deck(sum(expected.values()), preset)

    assert Counter(deck) == expected


def test_daily_discussion_uses_a_seeded_circular_starting_seat() -> None:
    """Discussion should rotate around a daily random living seat, not always p1."""
    game = Game(fixed_config(), terminal=SilentTerminal())

    order = game._discussion_order()  # noqa: SLF001

    assert [player.player_id for player in order] == [
        "p6",
        "p1",
        "p2",
        "p3",
        "p4",
        "p5",
    ]


def test_discussion_start_is_independent_from_private_action_randomness() -> None:
    """Secret night branches must not shift the public discussion start."""
    baseline = Game(fixed_config(), terminal=SilentTerminal())
    night_branch = Game(fixed_config(), terminal=SilentTerminal())

    # Controllers, tie-breaking, and role resolution use the main RNG. A
    # different secret night path may consume an arbitrary number of draws.
    for _ in range(10):
        night_branch.rng.random()

    assert [
        player.player_id
        for player in baseline._discussion_order()  # noqa: SLF001
    ] == [
        player.player_id
        for player in night_branch._discussion_order()  # noqa: SLF001
    ]


def test_fresh_game_randomizes_seats_without_detaching_fixed_roles() -> None:
    """Player order should shuffle while an explicit role remains with its owner."""
    config = fixed_config()
    expected_roles = {seat.name: seat.fixed_role for seat in config.players}
    config = replace(
        config,
        rules=replace(config.rules, randomize_seating=True),
    )

    game = Game(config, terminal=SilentTerminal())

    assert [player.name for player in game.players] == [
        "玩家3",
        "玩家4",
        "玩家6",
        "玩家1",
        "玩家5",
        "玩家2",
    ]
    assert {player.name: player.role for player in game.players} == expected_roles


def test_discussion_start_randomization_can_be_disabled() -> None:
    """A house rule should preserve the original fixed seat order when requested."""
    config = fixed_config()
    config = replace(
        config,
        rules=replace(config.rules, randomize_discussion_start=False),
    )
    game = Game(config, terminal=SilentTerminal())

    assert [
        player.player_id
        for player in game._discussion_order()  # noqa: SLF001
    ] == ["p1", "p2", "p3", "p4", "p5", "p6"]


def test_setup_keeps_roles_private_and_wolf_roster_team_only() -> None:
    """Setup secrets are delivered to the owning player or the wolf team."""
    game = Game(fixed_config(), terminal=SilentTerminal())
    game._setup()  # noqa: SLF001 - setup routing is the unit under test.

    for player in game.players:
        own_role_events = [
            event for event in player.memory.events if "你的身份是" in event.text
        ]
        assert len(own_role_events) == 1
        assert own_role_events[0].visibility is Visibility.PRIVATE
    wolf_roster_recipients = [
        recipients
        for event, recipients in game.boundary.audit_log
        if "狼人队友名单" in event.text
    ]
    assert wolf_roster_recipients == [frozenset({"p1", "p2"})]
    assert all(
        "狼人队友名单" not in event.text for event in game.players[2].memory.events
    )


def test_spectator_progress_streams_without_revealing_private_actor() -> None:
    """Progress names public actors but hides identities behind private actions."""
    terminal = CapturingTerminal()
    controller = ScriptedController()
    config = replace(fixed_config(), spectator_progress=True)
    game = Game(config, controllers={"p1": controller}, terminal=terminal)

    game.phase = "discussion"
    game._act(  # noqa: SLF001
        game._by_id["p1"],  # noqa: SLF001
        ActionRequest(ActionKind.SPEAK, "发言"),
    )
    game.phase = "night"
    game._act(  # noqa: SLF001
        game._by_id["p1"],  # noqa: SLF001
        ActionRequest(ActionKind.TEAM_CHAT, "狼聊"),
    )

    assert terminal.progress_events[0] == "1号 玩家1 正在组织公开发言……"
    assert terminal.progress_events[1] == "一项夜间私密行动正在处理中……"
    assert "玩家1" not in terminal.progress_events[1]
    assert "狼人" not in terminal.progress_events[1]


def test_llm_token_summary_reports_cache_hits_without_private_context() -> None:
    """The terminal metric should expose counts but no player-private prompt."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
    )
    client.observed_input_tokens = 2000
    client.observed_cached_tokens = 1200
    client.observed_output_tokens = 100
    client.observed_usage_responses = 2
    game = Game(
        fixed_config(),
        controllers={"p1": LLMController(client, persona="私密人物设定")},
        terminal=SilentTerminal(),
    )

    summary = game._llm_token_usage_text()  # noqa: SLF001

    assert "输入 2000" in summary
    assert "缓存命中 1200（60.0%）" in summary
    assert "私密人物设定" not in summary


def test_strict_controllers_never_fall_back_to_local_bot() -> None:
    """A formal all-LLM game must stop on failures or illegal model choices."""
    config = replace(fixed_config(), strict_controllers=True)
    failing_game = Game(
        config,
        controllers={"p1": FailingController()},
        terminal=SilentTerminal(),
    )
    with pytest.raises(RuntimeError, match="simulated provider outage"):
        failing_game._act(  # noqa: SLF001
            failing_game._by_id["p1"],  # noqa: SLF001
            ActionRequest(ActionKind.SPEAK, "发言"),
        )

    illegal = ScriptedController(
        {ActionKind.VOTE: [AgentResponse(choice="not-a-player")]},
    )
    illegal_game = Game(
        config,
        controllers={"p1": illegal},
        terminal=SilentTerminal(),
    )
    with pytest.raises(RuntimeError, match="illegal choice"):
        illegal_game._act(  # noqa: SLF001
            illegal_game._by_id["p1"],  # noqa: SLF001
            ActionRequest(
                ActionKind.VOTE,
                "投票",
                (ActionOption("p2", "玩家2"),),
            ),
        )


def test_strict_private_failure_does_not_reveal_actor_or_ability() -> None:
    """A resumable night failure must not contaminate the shared terminal."""
    game = Game(
        replace(fixed_config(), strict_controllers=True),
        controllers={"p1": FailingController()},
        terminal=SilentTerminal(),
    )

    with pytest.raises(RuntimeError) as captured:
        game._act(  # noqa: SLF001
            game._by_id["p1"],  # noqa: SLF001
            ActionRequest(
                ActionKind.WOLF_KILL,
                "选择刀口",
                (ActionOption("p3", "3号 玩家3"),),
            ),
        )

    error = str(captured.value)
    assert "玩家1" not in error
    assert "wolf_kill" not in error
    assert "private action" in error


def test_controller_retries_stay_llm_only() -> None:
    """Transient failures should retry the same controller without bot fallback."""
    controller = FlakyController()
    config = replace(
        fixed_config(),
        strict_controllers=True,
        controller_retries=1,
    )
    game = Game(
        config,
        controllers={"p1": controller},
        terminal=SilentTerminal(),
    )

    response = game._act(  # noqa: SLF001
        game._by_id["p1"],  # noqa: SLF001
        ActionRequest(ActionKind.SPEAK, "发言"),
    )

    assert controller.calls == 2
    assert response.text == "重试成功"
    assert game._by_id["p1"].memory.thoughts[-1].text == "保留私密判断"  # noqa: SLF001


def test_non_strict_failure_uses_visible_safe_fallback_and_metrics() -> None:
    """Casual fallback should abstain, report degradation, and remain auditable."""
    provider = LLMProviderConfig(base_url="https://example.invalid/v1", model="test")
    config = fixed_config()
    config = replace(
        config,
        players=tuple(
            replace(player, controller="llm", provider="test")
            for player in config.players
        ),
        providers={"test": provider},
        strict_controllers=False,
        spectator_progress=True,
    )
    terminal = CapturingTerminal()
    game = Game(
        config,
        controllers={"p1": FailingController()},
        terminal=terminal,
    )

    response = game._act(  # noqa: SLF001
        game._by_id["p1"],  # noqa: SLF001
        ActionRequest(
            ActionKind.VOTE,
            "投票",
            (ActionOption("p2", "2号 玩家2"),),
            allow_abstain=True,
        ),
    )

    assert response.choice is None
    assert response.used_fallback is True
    assert game._controller_metrics.fallbacks == 1  # noqa: SLF001
    assert any(
        "公开动作已使用系统安全后备" in item for item in terminal.progress_events
    )


def test_llm_public_votes_are_collected_in_parallel() -> None:
    """Mutually invisible LLM votes should overlap while preserving seat-order output."""
    provider = LLMProviderConfig(base_url="https://example.invalid/v1", model="test")
    config = fixed_config()
    config = replace(
        config,
        players=tuple(
            replace(player, controller="llm", provider="test")
            for player in config.players
        ),
        providers={"test": provider},
        parallel_llm_votes=True,
    )
    barrier = threading.Barrier(2, timeout=2)
    controllers = {
        "p1": BarrierVoteController(barrier),
        "p2": BarrierVoteController(barrier),
        **{f"p{index}": ScriptedController() for index in range(3, 7)},
    }
    game = Game(config, controllers=controllers, terminal=SilentTerminal())
    game.phase = "vote"

    votes = game._collect_votes(None)  # noqa: SLF001

    assert list(votes) == [f"p{index}" for index in range(1, 7)]
    assert all(target is not None for target in votes.values())


def test_parallel_votes_remain_replayable_from_checkpoint(tmp_path) -> None:
    """Concurrent requests must still journal responses in deterministic seat order."""
    provider = LLMProviderConfig(base_url="https://example.invalid/v1", model="test")
    checkpoint = tmp_path / "private.checkpoint.json"
    transcript = tmp_path / "public.log"
    base = fixed_config()
    config = replace(
        base,
        players=tuple(
            replace(player, controller="llm", provider="test")
            for player in base.players
        ),
        providers={"test": provider},
        checkpoint_path=str(checkpoint),
        public_transcript_path=str(transcript),
        parallel_llm_votes=True,
    )
    controllers = {f"p{index}": ScriptedController() for index in range(1, 7)}
    game = Game(config, controllers=controllers)
    game.day = 1
    game.phase = "vote"
    game._save_checkpoint(next_day=1, next_step="daytime")  # noqa: SLF001

    original_votes = game._collect_votes(None)  # noqa: SLF001
    failing = {f"p{index}": FailingController() for index in range(1, 7)}
    resumed = Game(
        config,
        controllers=failing,
        resume_checkpoint=checkpoint,
    )
    resumed.day = 1
    resumed.phase = "vote"

    replayed_votes = resumed._collect_votes(None)  # noqa: SLF001

    assert replayed_votes == original_votes
    assert all(len(controller.requests) == 1 for controller in controllers.values())


def test_checkpoint_restores_the_independent_discussion_rng(tmp_path) -> None:
    """A resumed game should retain the next public discussion draw."""
    checkpoint = tmp_path / "private.checkpoint.json"
    config = replace(fixed_config(), checkpoint_path=str(checkpoint))
    game = Game(config, terminal=SilentTerminal())

    game._discussion_order()  # noqa: SLF001 - consume the first day's draw.
    game._save_checkpoint(next_day=2, next_step="daytime")  # noqa: SLF001
    expected = [
        player.player_id
        for player in game._discussion_order()  # noqa: SLF001
    ]

    resumed = Game(
        config,
        terminal=SilentTerminal(),
        resume_checkpoint=checkpoint,
    )

    assert [
        player.player_id
        for player in resumed._discussion_order()  # noqa: SLF001
    ] == expected

    # Legacy compatibility: checkpoints written before discussion RNG isolation do
    # not include `discussion_rng_state`. They should fall back to the saved main
    # RNG state, preserving the next public discussion draw from the legacy stream.
    legacy_checkpoint = tmp_path / "legacy.checkpoint.json"
    raw = json.loads(checkpoint.read_text(encoding="utf-8"))
    legacy_rng = __import__("random").Random(config.seed)
    legacy_rng.randrange(len(game.players))  # Simulate day-1 discussion draw.
    legacy_state = legacy_rng.getstate()
    raw["rng_state"] = [legacy_state[0], list(legacy_state[1]), legacy_state[2]]
    raw.pop("discussion_rng_state", None)
    legacy_checkpoint.write_text(json.dumps(raw), encoding="utf-8")

    resumed_legacy = Game(
        config,
        terminal=SilentTerminal(),
        resume_checkpoint=legacy_checkpoint,
    )
    legacy_start = legacy_rng.randrange(len(game.players))
    alive_ids = [f"p{index}" for index in range(1, len(game.players) + 1)]
    expected_legacy = [*alive_ids[legacy_start:], *alive_ids[:legacy_start]]

    assert [
        player.player_id
        for player in resumed_legacy._discussion_order()  # noqa: SLF001
    ] == expected_legacy

def test_generated_config_defaults_to_recoverable_strict_play() -> None:
    """New users should receive safe live-game defaults without extra CLI flags."""
    config = example_config()

    assert config["spectator_progress"] is True
    assert config["strict_controllers"] is True
    assert config["controller_retries"] == 2
    assert config["checkpoint_path"] == "game_runs/private.checkpoint.json"
    assert config["public_transcript_path"] == "game_runs/public.log"
    assert config["parallel_llm_votes"] is True
    assert config["human_strategy_notes"] is False
    assert config["providers"]["default"]["max_tokens"] == 2000


def test_checkpoint_replays_each_completed_controller_call(tmp_path) -> None:
    """Resume should replay journaled responses and roll back partial public output."""
    checkpoint = tmp_path / "private.checkpoint.json"
    transcript = tmp_path / "public.log"
    scripted = ScriptedController(
        {
            ActionKind.SPEAK: [
                AgentResponse(text="已完成的发言", thought="已完成的私密判断"),
            ],
        },
    )
    config = replace(
        fixed_config(),
        checkpoint_path=str(checkpoint),
        public_transcript_path=str(transcript),
        strict_controllers=True,
    )
    game = Game(config, controllers={"p1": scripted})
    game.day = 1
    game.phase = "discussion"
    game.boundary.private(
        day=1,
        phase="discussion",
        recipient="p1",
        text="阶段起点私密信息",
    )
    game.terminal.announce("阶段起点")
    game._save_checkpoint(next_day=1, next_step="daytime")  # noqa: SLF001
    request = ActionRequest(ActionKind.SPEAK, "发言")
    response = game._act(game._by_id["p1"], request)  # noqa: SLF001
    game._say(game._by_id["p1"], response.text)  # noqa: SLF001
    assert "已完成的发言" in transcript.read_text(encoding="utf-8")

    resumed = Game(
        config,
        controllers={"p1": FailingController()},
        resume_checkpoint=checkpoint,
    )

    assert "已完成的发言" not in transcript.read_text(encoding="utf-8")
    replayed = resumed._act(resumed._by_id["p1"], request)  # noqa: SLF001
    assert replayed == response
    assert resumed._by_id["p1"].memory.thoughts[-1].text == (  # noqa: SLF001
        "已完成的私密判断"
    )
    assert checkpoint.stat().st_mode & 0o777 == 0o600


def test_players_receive_global_and_role_specific_skills() -> None:
    """Every seat should automatically receive only its own role playbook."""
    game = Game(fixed_config(), terminal=SilentTerminal())

    for player in game.players:
        skill_names = {skill.name for skill in player.skills}
        assert "global_gamecraft" in skill_names
        assert f"role_{player.role.value}" in skill_names
        other_role_skills = {
            f"role_{role.value}" for role in Role if role is not player.role
        }
        assert not skill_names & other_role_skills

    movie_game = Game(
        fixed_role_config(
            [
                Role.WEREWOLF,
                Role.MADMAN,
                Role.SEER,
                Role.BODYGUARD,
                Role.MADMAN,
                Role.MADMAN,
            ],
            "movie_mad_land",
        ),
        terminal=SilentTerminal(),
    )
    assert all(
        "global_movie_survival" in {skill.name for skill in player.skills}
        for player in movie_game.players
    )
    assert all(
        "board_movie_mad_land" in {skill.name for skill in player.skills}
        for player in movie_game.players
    )
    mad_land_skill = next(
        skill
        for skill in movie_game.players[0].skills
        if skill.name == "board_movie_mad_land"
    )
    assert "所有角色都可以声称狂人" in mad_land_skill.instructions
    assert "不是免票证明" in mad_land_skill.instructions
    assert all(
        "global_movie_survival" not in {skill.name for skill in player.skills}
        for player in game.players
    )
    assert all(
        "board_movie_mad_land" not in {skill.name for skill in player.skills}
        for player in game.players
    )


def test_madman_stays_out_of_wolf_chat_and_wins_with_werewolves() -> None:
    """Madmen appear village-side and share victory, but never wolf secrets."""
    roles = [
        Role.WEREWOLF,
        Role.MADMAN,
        Role.SEER,
        Role.VILLAGER,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    seer = ScriptedController(
        {ActionKind.SEER_INSPECT: [AgentResponse(choice="p2")]},
    )
    game = Game(
        fixed_role_config(roles, "movie_prison_break"),
        controllers={"p3": seer},
        terminal=SilentTerminal(),
    )
    game._setup()  # noqa: SLF001
    game.day = 1

    game._seer_turn()  # noqa: SLF001

    assert all(
        event.visibility is not Visibility.WEREWOLF
        for event in game._by_id["p2"].memory.events  # noqa: SLF001
    )
    assert any(
        "玩家2 属于【村人侧】" in event.text
        for event in game._by_id["p3"].memory.events  # noqa: SLF001
    )
    for player_id in ("p3", "p4", "p5", "p6"):
        game._by_id[player_id].alive = False  # noqa: SLF001
    assert game._winner() is Faction.WEREWOLF  # noqa: SLF001
    assert game._winning_players(Faction.WEREWOLF) == (  # noqa: SLF001
        "玩家1",
        "玩家2",
    )
    assert game._prize_shares(("玩家1", "玩家2")) == (  # noqa: SLF001
        ("玩家1", 0.5),
        ("玩家2", 0.5),
    )
    game._by_id["p2"].alive = False  # noqa: SLF001
    assert game._winning_players(Faction.WEREWOLF) == ("玩家1",)  # noqa: SLF001
    assert game._prize_shares(("玩家1",)) == (("玩家1", 1.0),)  # noqa: SLF001


def test_bodyguard_blocks_wolf_attack_and_cannot_protect_self() -> None:
    """The Bodyguard's legal choices exclude themself and protection prevents death."""
    roles = [
        Role.WEREWOLF,
        Role.BODYGUARD,
        Role.SEER,
        Role.VILLAGER,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    wolf = ScriptedController(
        {ActionKind.WOLF_KILL: [AgentResponse(choice="p3")]},
    )
    bodyguard = ScriptedController(
        {ActionKind.BODYGUARD_PROTECT: [AgentResponse(choice="p3")]},
    )
    seer = ScriptedController(
        {ActionKind.SEER_INSPECT: [AgentResponse(choice="p1")]},
    )
    game = Game(
        fixed_role_config(roles, "movie_basic"),
        controllers={"p1": wolf, "p2": bodyguard, "p3": seer},
        terminal=SilentTerminal(),
    )
    game._setup()  # noqa: SLF001
    game.day = 1

    game._night()  # noqa: SLF001

    protect_request = next(
        request
        for request in bodyguard.requests
        if request.kind is ActionKind.BODYGUARD_PROTECT
    )
    assert "p2" not in {option.value for option in protect_request.options}
    assert game._by_id["p3"].alive  # noqa: SLF001
    assert any(
        "平安夜" in event.text
        for event in game._by_id["p4"].memory.events  # noqa: SLF001
    )


def test_medium_receives_only_the_previous_exiles_alignment() -> None:
    """The Medium result names the direct exile and uses divination alignment."""
    roles = [
        Role.WEREWOLF,
        Role.MEDIUM,
        Role.SEER,
        Role.MADMAN,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    game = Game(fixed_role_config(roles), terminal=SilentTerminal())
    game.day = 2
    game._last_exiled_id = "p4"  # noqa: SLF001 - previous vote is test setup.

    game._medium_turn()  # noqa: SLF001

    medium_events = game._by_id["p2"].memory.events  # noqa: SLF001
    assert len(medium_events) == 1
    assert "玩家4 显示为【村人侧】" in medium_events[0].text
    assert all(
        not player.memory.events for player in game.players if player.player_id != "p2"
    )


def test_fox_survives_wolf_attack() -> None:
    """A wolf attack on the Fox resolves as a public peaceful night."""
    roles = [
        Role.WEREWOLF,
        Role.FOX,
        Role.SEER,
        Role.VILLAGER,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    wolf = ScriptedController(
        {ActionKind.WOLF_KILL: [AgentResponse(choice="p2")]},
    )
    seer = ScriptedController(
        {ActionKind.SEER_INSPECT: [AgentResponse(choice="p4")]},
    )
    game = Game(
        fixed_role_config(roles, "movie_crazy_fox"),
        controllers={"p1": wolf, "p3": seer},
        terminal=SilentTerminal(),
    )
    game._setup()  # noqa: SLF001
    game.day = 1

    game._night()  # noqa: SLF001

    assert game._by_id["p2"].alive  # noqa: SLF001
    assert any(
        "平安夜" in event.text
        for event in game._by_id["p2"].memory.events  # noqa: SLF001
    )


def test_fox_dies_when_inspected_and_overrides_base_winner_if_alive() -> None:
    """Divination kills the Fox; otherwise a living Fox steals a base outcome."""
    roles = [
        Role.WEREWOLF,
        Role.FOX,
        Role.SEER,
        Role.VILLAGER,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    wolf = ScriptedController(
        {ActionKind.WOLF_KILL: [AgentResponse(choice="p4")]},
    )
    fox = ScriptedController()
    seer = ScriptedController(
        {ActionKind.SEER_INSPECT: [AgentResponse(choice="p2")]},
    )
    game = Game(
        fixed_role_config(roles, "movie_crazy_fox"),
        controllers={"p1": wolf, "p2": fox, "p3": seer},
        terminal=SilentTerminal(),
    )
    game._setup()  # noqa: SLF001
    game.day = 1

    game._night()  # noqa: SLF001

    assert not game._by_id["p2"].alive  # noqa: SLF001
    assert any(
        "玩家2 属于【村人侧】" in event.text
        for event in game._by_id["p3"].memory.events  # noqa: SLF001
    )

    survival_game = Game(
        fixed_role_config(roles, "movie_crazy_fox"),
        terminal=SilentTerminal(),
    )
    survival_game._by_id["p1"].alive = False  # noqa: SLF001
    assert survival_game._winner() is Faction.FOX  # noqa: SLF001
    assert survival_game._winning_players(Faction.FOX) == ("玩家2",)  # noqa: SLF001


def lovers_game() -> tuple[Game, ScriptedController, ScriptedController]:
    """Create a table where Cupid links the Werewolf and Seer."""
    roles = [
        Role.WEREWOLF,
        Role.CUPID,
        Role.SEER,
        Role.BODYGUARD,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    first_lover = ScriptedController(
        {ActionKind.LOVER_CHAT: [AgentResponse(text="只给恋人看的狼方消息")]},
    )
    cupid = ScriptedController(
        {
            ActionKind.CUPID_LINK: [
                AgentResponse(choice="p1"),
                AgentResponse(choice="p3"),
            ],
        },
    )
    second_lover = ScriptedController(
        {ActionKind.LOVER_CHAT: [AgentResponse(text="只给恋人看的预言家消息")]},
    )
    game = Game(
        fixed_role_config(roles, "movie_lovers"),
        controllers={"p1": first_lover, "p2": cupid, "p3": second_lover},
        terminal=SilentTerminal(),
    )
    game._setup()  # noqa: SLF001
    return game, first_lover, second_lover


def test_cupid_links_lovers_and_lovers_chat_without_leaking() -> None:
    """Only the selected pair receives the Lover subrole and private chat."""
    game, _, _ = lovers_game()
    game.day = 1
    game.phase = "night"

    game._lover_turn()  # noqa: SLF001

    assert game._by_id["p1"].lover_id == "p3"  # noqa: SLF001
    assert game._by_id["p3"].lover_id == "p1"  # noqa: SLF001
    assert "subrole_lover" in {
        skill.name
        for skill in game._by_id["p1"].skills  # noqa: SLF001
    }
    chat_routes = [
        recipients
        for event, recipients in game.boundary.audit_log
        if event.visibility is Visibility.LOVERS and event.sender is not None
    ]
    assert chat_routes == [frozenset({"p1", "p3"})] * 2
    outsider_text = "\n".join(
        event.text
        for event in game._by_id["p4"].memory.events  # noqa: SLF001
    )
    assert "只给恋人看的" not in outsider_text


def test_lover_dies_of_heartbreak_and_lovers_can_steal_the_endgame() -> None:
    """Lovers die together; only surviving members claim the exclusive result."""
    death_game, _, heartbroken_lover = lovers_game()
    death_game.day = 1
    death_game._apply_deaths(  # noqa: SLF001
        {"p1": {DeathCause.VOTE}},
        "玩家1 被放逐。",
    )

    assert not death_game._by_id["p1"].alive  # noqa: SLF001
    assert not death_game._by_id["p3"].alive  # noqa: SLF001
    assert any(
        "恋人 3号 玩家3 随之殉情" in event.text
        for event in death_game._by_id["p4"].memory.events  # noqa: SLF001
    )
    assert all(
        request.kind is not ActionKind.LAST_WORDS
        for request in heartbroken_lover.requests
    )

    win_game, _, _ = lovers_game()
    for player_id in ("p2", "p4", "p5", "p6"):
        win_game._by_id[player_id].alive = False  # noqa: SLF001
    assert win_game._winner() is Faction.LOVERS  # noqa: SLF001
    assert win_game._winning_players(Faction.LOVERS) == (  # noqa: SLF001
        "玩家1",
        "玩家3",
    )
    result = win_game._finish(Faction.LOVERS, "test")  # noqa: SLF001
    assert result.winning_players == ("玩家1", "玩家3")
    assert result.prize_shares == (("玩家1", 0.5), ("玩家3", 0.5))


def test_shared_players_only_learn_each_other() -> None:
    """The Shared Player confirmation is delivered as two one-seat secrets."""
    roles = [
        Role.WEREWOLF,
        Role.SHARED,
        Role.SHARED,
        Role.SEER,
        Role.VILLAGER,
        Role.VILLAGER,
    ]
    game = Game(fixed_role_config(roles), terminal=SilentTerminal())

    game._setup()  # noqa: SLF001

    routes = [
        recipients
        for event, recipients in game.boundary.audit_log
        if "另一名共有者是" in event.text
    ]
    assert routes == [frozenset({"p2"}), frozenset({"p3"})]
    assert any(
        "玩家3" in event.text
        for event in game._by_id["p2"].memory.events  # noqa: SLF001
    )
    assert all(
        "另一名共有者是" not in event.text
        for event in game._by_id["p4"].memory.events  # noqa: SLF001
    )


def test_sixteen_llm_case_config_is_loadable() -> None:
    """The documented case configuration should stay executable."""
    path = Path(__file__).parents[1] / "examples" / "16_llm_responses.json"

    config = load_config(path)

    assert len(config.players) == 16
    assert all(player.controller == "llm" for player in config.players)
    assert config.providers["responses"].wire_api == "responses"

    transcript = (
        path.parent / "case_studies" / "16_llm_2026-07-20_public_transcript.txt"
    ).read_text(encoding="utf-8")
    assert "[法官] 狼人阵营获胜" in transcript
    assert "达到最大天数" not in transcript
    assert "[恢复]" not in transcript
    assert "[技术回滚]" not in transcript
    assert "控制器调用失败" not in transcript
    assert "OPENAI_API_KEY" not in transcript


@pytest.mark.parametrize(
    ("filename", "preset", "player_count"),
    [
        ("movie_lovers.json", "movie_lovers", 11),
        ("movie_crazy_fox.json", "movie_crazy_fox", 12),
        ("movie_mad_land.json", "movie_mad_land", 10),
    ],
)
def test_movie_example_configs_are_loadable(
    filename: str,
    preset: str,
    player_count: int,
) -> None:
    """Documented human-plus-bot movie examples should remain runnable."""
    path = Path(__file__).parents[1] / "examples" / filename

    config = load_config(path)

    assert config.role_preset == preset
    assert len(config.players) == player_count
    assert config.players[0].controller == "human"


def test_offline_game_completes_and_exports_separate_memories(tmp_path: Path) -> None:
    """A full judge-only simulation should terminate without any API access."""
    output = str(tmp_path)
    config = replace(demo_config(8, seed=7), memory_directory=output)
    game = Game(config, terminal=SilentTerminal())

    result = game.run()

    assert result.winner in {Faction.GOOD, Faction.WEREWOLF, None}
    assert result.prize_shares == ()
    assert result.days <= config.rules.max_days
    wolf_ids = {
        player.player_id for player in game.players if player.role is Role.WEREWOLF
    }
    for event, recipients in game.boundary.audit_log:
        if event.visibility is Visibility.WEREWOLF:
            assert recipients <= wolf_ids
        elif event.visibility is Visibility.PRIVATE:
            assert len(recipients) == 1
    paths = sorted(tmp_path.glob("*.json"))
    assert len(paths) == 8
    for path, player in zip(paths, game.players):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["player_id"] == player.player_id
        assert payload["role"] == player.role.value
    non_wolf = next(
        player for player in game.players if player.role is not Role.WEREWOLF
    )
    non_wolf_path = next(
        path for path in paths if path.name.startswith(f"{non_wolf.player_id}_")
    )
    non_wolf_payload = json.loads(non_wolf_path.read_text(encoding="utf-8"))
    assert all(
        event["visibility"] != Visibility.WEREWOLF.value
        for event in non_wolf_payload["events"]
    )
    assert all(
        "狼人队友名单" not in event["text"] for event in non_wolf_payload["events"]
    )
    assert any(player.memory.thoughts for player in game.players)


def test_invalid_role_count_is_rejected() -> None:
    """Unsupported table sizes should fail before a match starts."""
    with pytest.raises(ValueError, match="6 to 16"):
        role_deck(5)
    with pytest.raises(ValueError, match="requires 11 players"):
        role_deck(10, "movie_lovers")


def test_last_words_follow_death_timing_and_cause() -> None:
    """Later night deaths stay silent while daytime exiles may speak."""
    game = Game(fixed_config(), terminal=SilentTerminal())

    game.day = 1
    assert game._allows_last_words({DeathCause.WOLF})  # noqa: SLF001
    game.day = 2
    assert not game._allows_last_words({DeathCause.WOLF})  # noqa: SLF001
    assert not game._allows_last_words({DeathCause.DIVINATION})  # noqa: SLF001
    assert game._allows_last_words({DeathCause.VOTE})  # noqa: SLF001
    assert not game._allows_last_words({DeathCause.HUNTER})  # noqa: SLF001
    assert not game._allows_last_words({DeathCause.HEARTBREAK})  # noqa: SLF001
