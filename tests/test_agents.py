"""Tests for isolated LLM prompting and structured responses."""

from __future__ import annotations

from typing import Any

import werewolf.agents as agents_module
from werewolf.agents import (
    HumanController,
    LLMController,
    OpenAICompatibleClient,
    SafeFallbackController,
    Terminal,
)
from werewolf.config import LLMProviderConfig
from werewolf.models import (
    ActionKind,
    ActionOption,
    ActionRequest,
    Faction,
    MemoryEvent,
    PlayerView,
    Role,
    Visibility,
)


def test_llm_receives_only_supplied_personal_view() -> None:
    """The client payload must contain own secrets but no global hidden state."""
    captured: dict[str, Any] = {}

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"choice":"p2","text":"","thought":"怀疑二号"}',
                    },
                },
            ],
        }

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )
    controller = LLMController(client, persona="谨慎")
    view = PlayerView(
        player_id="p1",
        name="一号",
        role=Role.SEER,
        role_name="预言家",
        role_description="每晚查验一人",
        faction=Faction.GOOD,
        lover=None,
        alive_players=(("p1", "一号"), ("p2", "二号")),
        dead_players=(),
        events=(
            MemoryEvent(
                sequence=1,
                day=1,
                phase="night",
                text="仅一号可见的查验结果",
                visibility=Visibility.PRIVATE,
            ),
        ),
        thoughts=(),
        skills=(),
        day=1,
        phase="vote",
        language="zh-CN",
    )

    response = controller.act(
        view,
        ActionRequest(
            ActionKind.VOTE,
            "选择目标",
            (ActionOption("p2", "二号"),),
        ),
    )

    serialized = str(captured["messages"])
    assert "仅一号可见的查验结果" in serialized
    assert "二号是狼人" not in serialized
    assert response.choice == "p2"
    assert response.thought == "怀疑二号"


def test_llm_parser_accepts_fenced_json() -> None:
    """Common markdown fencing should not break compatible providers."""
    parsed = LLMController._parse_json(  # noqa: SLF001 - parser behavior is the unit under test.
        '```json\n{"choice": null, "text": "你好"}\n```',
    )
    assert parsed["text"] == "你好"


def test_responses_api_payload_and_output_shape() -> None:
    """Codex-style providers should use ``/responses`` request semantics."""
    captured: dict[str, Any] = {}

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": '{"text":"收到"}'},
                    ],
                },
            ],
        }

    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.invalid/v1",
            model="reasoning-model",
            wire_api="responses",
            reasoning_effort="low",
            use_json_mode=False,
        ),
        transport=transport,
    )

    content = client.complete([{"role": "user", "content": "行动"}])

    assert content == '{"text":"收到"}'
    assert captured["input"] == [{"role": "user", "content": "行动"}]
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["store"] is False
    assert "prompt_cache_key" not in captured
    assert "messages" not in captured


def test_responses_prompt_cache_uses_stable_private_key_and_tracks_usage() -> None:
    """Cache routing should be stable, private, and measurable from API usage."""
    captured: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(payload)
        return {
            "output_text": '{"text":"收到"}',
            "usage": {
                "input_tokens": 1600,
                "input_tokens_details": {"cached_tokens": 1024},
                "output_tokens": 80,
            },
        }

    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.invalid/v1",
            model="reasoning-model",
            wire_api="responses",
            use_json_mode=False,
            prompt_cache=True,
            prompt_cache_retention="24h",
        ),
        transport=transport,
    )
    stable_system = "一号的私密身份与技能"

    client.complete(
        [
            {"role": "system", "content": stable_system},
            {"role": "user", "content": "第一轮动态请求"},
        ],
    )
    client.complete(
        [
            {"role": "system", "content": stable_system},
            {"role": "user", "content": "第二轮动态请求"},
        ],
    )

    first_key = captured[0]["prompt_cache_key"]
    assert first_key == captured[1]["prompt_cache_key"]
    other_key = client._payload(  # noqa: SLF001 - cache isolation is under test.
        [{"role": "system", "content": "二号的另一份私密身份与技能"}],
    )["prompt_cache_key"]
    assert other_key != first_key
    assert stable_system not in first_key
    assert len(first_key) <= 64
    assert captured[0]["prompt_cache_retention"] == "24h"
    assert client.observed_input_tokens == 3200
    assert client.observed_cached_tokens == 2048
    assert client.observed_output_tokens == 160
    assert client.observed_usage_responses == 2
    assert client.observed_cache_hit_rate == 0.64


def test_llm_places_append_only_history_before_dynamic_action() -> None:
    """Changing the current action must not invalidate the cached history prefix."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
    )
    controller = LLMController(client, persona="谨慎")
    view = PlayerView(
        player_id="p1",
        name="一号",
        role=Role.VILLAGER,
        role_name="平民",
        role_description="没有夜间技能",
        faction=Faction.GOOD,
        lover=None,
        alive_players=(("p1", "一号"), ("p2", "二号")),
        dead_players=(),
        events=(
            MemoryEvent(
                sequence=1,
                day=1,
                phase="day",
                text="稳定的公开历史",
                visibility=Visibility.PUBLIC,
            ),
        ),
        thoughts=(),
        skills=(),
        day=1,
        phase="vote",
        language="zh-CN",
    )

    first = controller._messages(  # noqa: SLF001 - prompt layout is the unit under test.
        view,
        ActionRequest(ActionKind.SPEAK, "请发言"),
    )
    second = controller._messages(  # noqa: SLF001 - prompt layout is the unit under test.
        view,
        ActionRequest(
            ActionKind.VOTE,
            "请选择目标",
            (ActionOption("p2", "二号"),),
        ),
    )

    assert first[:2] == second[:2]
    assert "稳定的公开历史" in first[1]["content"]
    assert "当前：" not in first[1]["content"]
    assert "当前：" in first[2]["content"]
    assert first[2] != second[2]


def test_llm_receives_seat_map_and_public_parity_constraint() -> None:
    """Structured public mechanics should prevent impossible role-world claims."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
    )
    controller = LLMController(client)
    view = PlayerView(
        player_id="p1",
        name="玩家0",
        role=Role.VILLAGER,
        role_name="平民",
        role_description="没有夜间技能",
        faction=Faction.GOOD,
        lover=None,
        alive_players=(("p1", "玩家0"), ("p3", "智能体5"), ("p8", "智能体4")),
        dead_players=(("p2", "智能体3"),),
        events=(),
        thoughts=(),
        skills=(),
        day=3,
        phase="discussion",
        language="zh-CN",
        seat_number=1,
        seat_players=(
            ("p1", 1, "玩家0"),
            ("p2", 2, "智能体3"),
            ("p3", 3, "智能体5"),
            ("p8", 8, "智能体4"),
        ),
        mechanical_context="第2天4人存活且游戏继续，因此至多1名存活狼人。",
    )

    messages = controller._messages(  # noqa: SLF001
        view,
        ActionRequest(ActionKind.SPEAK, "请发言"),
    )
    current = messages[-1]["content"]

    assert '"seat": 8' in current
    assert '"alive": false' in current
    assert "至多1名存活狼人" in current


def test_history_trimming_advances_in_cache_friendly_chunks() -> None:
    """Small appends beyond the limit should retain the same trimmed prefix."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
    )
    controller = LLMController(client, context_char_limit=2000)
    history = "\n".join(f"事件{index:03d}:" + "证据" * 20 for index in range(80))

    first = controller._trim_history(history)  # noqa: SLF001
    second = controller._trim_history(history + "\n新增短事件")  # noqa: SLF001

    assert second.startswith(first)
    assert len(second) <= controller.context_char_limit


def test_terminal_persists_only_explicit_public_output(tmp_path) -> None:
    """The spectator transcript should mirror judge, progress, and public speech."""
    transcript = tmp_path / "public.log"
    terminal = Terminal(clear_screen=False, transcript_path=transcript)

    terminal.announce("天亮了。")
    terminal.progress("公开行动处理中……")
    terminal.transient_progress("这条临时状态不能进入日志")
    terminal.clear_transient_progress()
    terminal.say("玩家01", "这是公开发言。")

    assert transcript.read_text(encoding="utf-8") == (
        "\n[法官] 天亮了。\n[观战] 公开行动处理中……\n[玩家01] 这是公开发言。\n"
    )


def test_human_controller_enables_readline_cursor_bindings(monkeypatch) -> None:
    """Human input should activate character deletion and left/right movement."""

    class FakeReadline:
        def __init__(self) -> None:
            self.bindings: list[str] = []

        def parse_and_bind(self, binding: str) -> None:
            self.bindings.append(binding)

    fake = FakeReadline()
    monkeypatch.setattr(agents_module, "_readline", fake)

    HumanController(Terminal(clear_screen=False))

    assert "set editing-mode emacs" in fake.bindings
    assert '"\\e[D": backward-char' in fake.bindings
    assert '"\\e[C": forward-char' in fake.bindings


def test_single_human_choice_skips_notes_and_terminal_handoff(monkeypatch) -> None:
    """One local human should confirm a vote without two unrelated extra prompts."""
    answers = iter(["/history", "1", ""])
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    controller = HumanController(
        Terminal(clear_screen=True),
        require_handoff=False,
        ask_strategy_note=False,
        confirm_critical_actions=True,
    )
    view = PlayerView(
        player_id="p1",
        name="一号",
        role=Role.VILLAGER,
        role_name="平民",
        role_description="没有夜间技能",
        faction=Faction.GOOD,
        lover=None,
        alive_players=(("p1", "一号"), ("p2", "二号")),
        dead_players=(),
        events=(),
        thoughts=(),
        skills=(),
        day=1,
        phase="vote",
        language="zh-CN",
    )

    response = controller.act(
        view,
        ActionRequest(
            ActionKind.VOTE,
            "请选择目标",
            (ActionOption("p2", "2号 二号"),),
            allow_abstain=True,
        ),
    )

    assert response.choice == "p2"
    assert response.thought == ""
    assert len(prompts) == 3
    assert "确认选择" in prompts[2]


def test_safe_fallback_abstains_from_optional_irreversible_actions() -> None:
    """A provider outage must not randomly poison, shoot, or cast a public vote."""
    view = PlayerView(
        player_id="p1",
        name="一号",
        role=Role.WITCH,
        role_name="女巫",
        role_description="有一瓶毒药",
        faction=Faction.GOOD,
        lover=None,
        alive_players=(("p1", "一号"), ("p2", "二号")),
        dead_players=(),
        events=(),
        thoughts=(),
        skills=(),
        day=1,
        phase="night",
        language="zh-CN",
    )
    controller = SafeFallbackController()

    poison = controller.act(
        view,
        ActionRequest(
            ActionKind.WITCH_POISON,
            "是否用毒",
            (ActionOption("p2", "2号 二号"),),
            allow_abstain=True,
        ),
    )
    required = controller.act(
        view,
        ActionRequest(
            ActionKind.SEER_INSPECT,
            "必须查验",
            (ActionOption("p2", "2号 二号"),),
        ),
    )

    assert poison.choice is None
    assert required.choice == "p2"


def test_responses_sse_stream_is_assembled_without_exposing_partial_json() -> None:
    """Responses text deltas should reconstruct one complete controller payload."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.invalid/v1",
            model="streaming-model",
            wire_api="responses",
            stream=True,
        ),
    )

    content = client._stream_content(  # noqa: SLF001 - SSE parsing is the unit under test.
        [
            b"event: response.output_text.delta\n",
            b'data: {"type":"response.output_text.delta","delta":"{\\"text\\":\\"ni"}\n',
            b'data: {"type":"response.output_text.delta","delta":"hao\\"}"}\n',
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1200,"input_tokens_details":{"cached_tokens":1024},"output_tokens":30}}}\n',
            b"data: [DONE]\n",
        ],
    )

    assert content == '{"text":"nihao"}'
    assert client.observed_input_tokens == 1200
    assert client.observed_cached_tokens == 1024
