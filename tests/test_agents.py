"""Tests for isolated LLM prompting and structured responses."""

from __future__ import annotations

import copy
import io
import json
import time
import urllib.error
from dataclasses import replace
from email.message import Message
from typing import Any

import pytest

import werewolf.agents as agents_module
from werewolf.agents import (
    HumanController,
    LLMController,
    OpenAICompatibleClient,
    ProviderHTTPError,
    ProviderOutputLimitError,
    ProviderProtocolError,
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
    PlayerBelief,
    PlayerView,
    Role,
    StrategyState,
    Thought,
    Visibility,
)
from werewolf.tools import PlayerToolbox


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
    assert "tool_choice" not in captured
    assert {tool["function"]["name"] for tool in captured["tools"]} == {
        "get_evidence_ledger",
        "search_visible_history",
        "get_player_dossier",
        "get_vote_analysis",
        "get_claim_matrix",
        "review_action_draft",
    }
    assert response.choice == "p2"
    assert response.thought == "怀疑二号"


def test_llm_parser_accepts_fenced_json() -> None:
    """Common markdown fencing should not break compatible providers."""
    parsed = LLMController._parse_json(  # noqa: SLF001 - parser behavior is the unit under test.
        '```json\n{"choice": null, "text": "你好"}\n```',
    )
    assert parsed["text"] == "你好"


def test_llm_tracks_explicit_null_separately_from_an_omitted_choice() -> None:
    """Only a present ``choice: null`` field can express an abstention."""
    outputs = iter(
        (
            '{"text":"","thought":"等待"}',
            '{"choice":null,"text":"","thought":"明确弃权"}',
        ),
    )

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        return {"choices": [{"message": {"content": next(outputs)}}]}

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )
    controller = LLMController(client)
    request = ActionRequest(
        ActionKind.VOTE,
        "请投票",
        (ActionOption("p2", "2号 玩家2"),),
        allow_abstain=True,
    )

    omitted = controller.act(seat_view(), request)
    explicit = controller.act(seat_view(), request)

    assert omitted.choice is None
    assert omitted.choice_provided is False
    assert explicit.choice is None
    assert explicit.choice_provided is True


def test_choice_action_uses_a_required_enum_schema_and_minimal_contract() -> None:
    """Selection calls should make an omitted or out-of-range choice impossible."""
    captured: dict[str, Any] = {}

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(copy.deepcopy(payload))
        return {"choices": [{"message": {"content": '{"choice":"p3"}'}}]}

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )
    request = ActionRequest(
        ActionKind.VOTE,
        "请投票",
        (
            ActionOption("p3", "3号 智能体3"),
            ActionOption("p4", "4号 智能体4"),
        ),
        allow_abstain=True,
    )

    response = LLMController(client).act(seat_view(), request)

    schema = captured["response_format"]["json_schema"]["schema"]
    assert captured["response_format"]["type"] == "json_schema"
    assert schema == {
        "type": "object",
        "properties": {"choice": {"enum": ["p3", "p4", None]}},
        "required": ["choice"],
        "additionalProperties": False,
    }
    assert "choice 是唯一字段且绝对不能省略" in captured["messages"][-1]["content"]
    assert response.choice == "p3"


def test_choice_schema_falls_back_once_for_older_compatible_providers() -> None:
    """A gateway without JSON Schema support should retain minimal JSON mode."""
    captured: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(copy.deepcopy(payload))
        if payload.get("response_format", {}).get("type") == "json_schema":
            raise ProviderHTTPError(400, unsupported_field="response_format")
        return {"choices": [{"message": {"content": '{"choice":"p3"}'}}]}

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )
    request = ActionRequest(
        ActionKind.VOTE,
        "请投票",
        (ActionOption("p3", "3号 智能体3"),),
    )

    response = LLMController(client).act(seat_view(), request)

    assert [item["response_format"]["type"] for item in captured] == [
        "json_schema",
        "json_object",
    ]
    assert client._json_schema_support is False  # noqa: SLF001
    assert response.choice == "p3"


def test_choice_schema_uses_the_responses_text_format_shape() -> None:
    """Responses API providers receive their native strict-schema envelope."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.invalid/v1",
            model="test",
            wire_api="responses",
        ),
    )
    schema = {
        "type": "object",
        "properties": {"choice": {"enum": ["p3"]}},
        "required": ["choice"],
        "additionalProperties": False,
    }

    payload = client._payload([], response_schema=schema)  # noqa: SLF001

    assert payload["text"]["format"] == {
        "type": "json_schema",
        "name": "werewolf_action",
        "strict": True,
        "schema": schema,
    }


def test_llm_conservatively_recovers_one_explicit_choice_from_prose() -> None:
    """A stranded explicit vote may be applied, but an ambiguous list may not."""
    outputs = iter(
        (
            '{"thought":"今天会投3号 智能体3。"}',
            '{"thought":"在3号 智能体3和4号 智能体4之间比较。"}',
        ),
    )

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        return {"choices": [{"message": {"content": next(outputs)}}]}

    controller = LLMController(
        OpenAICompatibleClient(
            LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
            transport=transport,
        ),
    )
    request = ActionRequest(
        ActionKind.VOTE,
        "请投票",
        (
            ActionOption("p3", "3号 智能体3"),
            ActionOption("p4", "4号 智能体4"),
        ),
        allow_abstain=True,
    )

    recovered = controller.act(seat_view(), request)
    ambiguous = controller.act(seat_view(), request)

    assert recovered.choice == "p3"
    assert recovered.choice_provided is True
    assert ambiguous.choice is None
    assert ambiguous.choice_provided is False


def test_llm_does_not_force_an_empty_history_lookup() -> None:
    """Opening actions may use tools, but should not pay for a useless forced call."""
    captured: dict[str, Any] = {}

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"choices": [{"message": {"content": '{"text":"开场发言"}'}}]}

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )

    LLMController(client).act(
        seat_view(),
        ActionRequest(ActionKind.SPEAK, "请开场", requires_text=True),
    )

    assert "tools" in captured
    assert "tool_choice" not in captured


def test_chat_completion_falls_back_to_reasoning_content() -> None:
    """Compatible reasoning gateways may return only ``reasoning_content``."""

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning_content": '{"choice":null,"text":"收到"}',
                    },
                },
            ],
        }

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )

    content = client.complete([{"role": "user", "content": "行动"}])

    assert content == '{"choice":null,"text":"收到"}'


def test_chat_completion_prefers_final_content_over_reasoning_content() -> None:
    """Provider reasoning must not replace an available final answer."""

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"text":"最终答案"}',
                        "reasoning_content": "内部推理",
                    },
                },
            ],
        }

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )

    content = client.complete([{"role": "user", "content": "行动"}])

    assert content == '{"text":"最终答案"}'


def test_chat_rejects_partial_content_when_output_limit_was_hit() -> None:
    """A non-empty but cut-off statement must be retried, not published."""

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"choice":null,"text":"理由是他在首日反复强调"}',
                    },
                    "finish_reason": "length",
                },
            ],
        }

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )

    with pytest.raises(ProviderOutputLimitError, match="output limit"):
        client.complete([{"role": "user", "content": "行动"}])


def test_responses_rejects_incomplete_output_even_when_text_exists() -> None:
    """Responses incomplete status should override a syntactically valid fragment."""

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output_text": '{"text":"被截断的发言"}',
        }

    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.invalid/v1",
            model="test",
            wire_api="responses",
        ),
        transport=transport,
    )

    with pytest.raises(ProviderOutputLimitError, match="incomplete"):
        client.complete([{"role": "user", "content": "行动"}])


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


def test_chat_api_sends_configured_reasoning_effort() -> None:
    """Chat-compatible reasoning models must not silently lose their effort."""
    captured: dict[str, Any] = {}

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"choices": [{"message": {"content": '{"text":"收到"}'}}]}

    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.invalid/v1",
            model="reasoning-model",
            wire_api="chat",
            reasoning_effort="max",
        ),
        transport=transport,
    )

    client.complete([{"role": "user", "content": "行动"}])

    assert captured["reasoning_effort"] == "max"
    assert "reasoning" not in captured


def test_chat_tools_continue_with_private_results_and_bounded_rounds() -> None:
    """Chat function calls should be executed and continued in one private loop."""
    captured: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = [
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-ledger",
                                "type": "function",
                                "function": {
                                    "name": "get_evidence_ledger",
                                    "arguments": "{}",
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                },
            ],
        },
        {
            "choices": [
                {
                    "message": {
                        "content": '{"choice":"p3","text":"投三号"}',
                    },
                },
            ],
        },
    ]

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(copy.deepcopy(payload))
        return responses.pop(0)

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )
    toolbox = PlayerToolbox(seat_view())

    result = client.complete_with_tools(
        [{"role": "user", "content": "请投票"}],
        toolbox.specs,
        toolbox.execute,
        max_rounds=1,
        require_first_tool=True,
    )

    assert result == '{"choice":"p3","text":"投三号"}'
    assert captured[0]["tools"][0]["type"] == "function"
    assert captured[0]["tools"][0]["function"]["name"] == "get_evidence_ledger"
    assert captured[0]["tool_choice"] == "required"
    assert "tools" not in captured[1]
    assert "tool_choice" not in captured[1]
    assert captured[1]["messages"][-2]["tool_calls"][0]["id"] == "call-ledger"
    assert captured[1]["messages"][-1]["role"] == "tool"
    assert captured[1]["messages"][-1]["tool_call_id"] == "call-ledger"
    assert '"ok": true' in captured[1]["messages"][-1]["content"]
    assert client.observed_tool_calls == 1
    assert client.observed_tool_failures == 0


def test_responses_tools_preserve_output_items_for_continuation() -> None:
    """Responses reasoning and calls must be replayed before function output."""
    captured: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = [
        {
            "output": [
                {"type": "reasoning", "id": "rs_1", "summary": []},
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call-search",
                    "name": "search_visible_history",
                    "arguments": '{"query":"智能体3"}',
                },
            ],
        },
        {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"choice":null,"text":"已核对"}',
                        },
                    ],
                },
            ],
        },
    ]

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(copy.deepcopy(payload))
        return responses.pop(0)

    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.invalid/v1",
            model="test",
            wire_api="responses",
        ),
        transport=transport,
    )
    toolbox = PlayerToolbox(seat_view())

    result = client.complete_with_tools(
        [{"role": "user", "content": "回顾历史"}],
        toolbox.specs,
        toolbox.execute,
        max_rounds=2,
        require_first_tool=True,
    )

    assert result == '{"choice":null,"text":"已核对"}'
    assert captured[0]["tools"][0]["name"] == "get_evidence_ledger"
    assert captured[0]["tool_choice"] == "required"
    continuation = captured[1]["input"]
    assert continuation[-3]["type"] == "reasoning"
    assert continuation[-2]["type"] == "function_call"
    assert continuation[-1]["type"] == "function_call_output"
    assert continuation[-1]["call_id"] == "call-search"


def test_tools_fall_back_when_provider_rejects_the_field() -> None:
    """A compatible provider without tools should be probed once, then remembered."""
    captured: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(copy.deepcopy(payload))
        if "tools" in payload:
            raise ProviderHTTPError(400, unsupported_field="tools")
        return {"choices": [{"message": {"content": '{"text":"普通回答"}'}}]}

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )
    toolbox = PlayerToolbox(seat_view())

    first = client.complete_with_tools(
        [{"role": "user", "content": "行动"}],
        toolbox.specs,
        toolbox.execute,
        max_rounds=2,
        require_first_tool=True,
    )
    second = client.complete_with_tools(
        [{"role": "user", "content": "再行动"}],
        toolbox.specs,
        toolbox.execute,
        max_rounds=2,
        require_first_tool=True,
    )

    assert first == second == '{"text":"普通回答"}'
    assert ["tools" in payload for payload in captured] == [True, False, False]


def test_controller_runs_evidence_then_review_and_returns_structured_memory() -> None:
    """A history-backed speech should retrieve, review, then finalize its state."""
    captured: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = [
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-votes",
                                "type": "function",
                                "function": {
                                    "name": "get_vote_analysis",
                                    "arguments": "{}",
                                },
                            },
                        ],
                    },
                },
            ],
        },
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-review",
                                "type": "function",
                                "function": {
                                    "name": "review_action_draft",
                                    "arguments": json.dumps(
                                        {
                                            "choice": None,
                                            "text": "我目前最怀疑三号。",
                                            "evidence_sequences": [1],
                                            "counter_case": "三号也可能只是判断失误。",
                                            "plan": "下一轮复核其票型。",
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            },
                        ],
                    },
                },
            ],
        },
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "choice": None,
                                "text": "我目前最怀疑三号。",
                                "thought": "三号当前最可疑。",
                                "note": "观察下一轮票型。",
                                "memory": {
                                    "beliefs": [
                                        {
                                            "player_id": "p3",
                                            "suspicion": 78,
                                            "confidence": 66,
                                            "evidence_sequences": [1, 999],
                                            "rationale": "一号事件中的公开发言。",
                                        },
                                    ],
                                    "open_questions": ["三号会不会改票？"],
                                    "plan": "下一轮复核三号。",
                                    "counter_case": "三号可能只是表达失误。",
                                },
                            },
                            ensure_ascii=False,
                        ),
                    },
                },
            ],
        },
    ]

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(copy.deepcopy(payload))
        return responses.pop(0)

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )
    view = replace(
        seat_view(),
        phase="discussion",
        events=(
            MemoryEvent(
                sequence=1,
                day=1,
                phase="discussion",
                text="3号 智能体3：我认为一号可疑。",
                visibility=Visibility.PUBLIC,
                sender="3号 智能体3",
            ),
        ),
    )

    result = LLMController(client).act(
        view,
        ActionRequest(
            ActionKind.SPEAK,
            "请发言",
            requires_text=True,
        ),
    )

    assert {tool["function"]["name"] for tool in captured[0]["tools"]} == {
        "get_evidence_ledger",
        "search_visible_history",
        "get_player_dossier",
        "get_vote_analysis",
        "get_claim_matrix",
    }
    assert [tool["function"]["name"] for tool in captured[1]["tools"]] == [
        "review_action_draft",
    ]
    assert "tools" not in captured[2]
    assert result.text == "我目前最怀疑三号。"
    assert result.strategy is not None
    assert result.strategy.beliefs[0].suspicion == 78
    assert result.strategy.beliefs[0].evidence_sequences == (1,)
    assert result.strategy.open_questions == ("三号会不会改票？",)


def test_choice_only_action_offers_tools_without_forcing_three_requests() -> None:
    """Parallel-friendly choices keep tools optional to avoid request bursts."""
    captured: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(copy.deepcopy(payload))
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"choice":"p3","text":""}',
                    },
                },
            ],
        }

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )
    view = replace(
        seat_view(),
        phase="vote",
        events=(
            MemoryEvent(
                sequence=1,
                day=1,
                phase="discussion",
                text="3号 智能体3：我认为一号可疑。",
                visibility=Visibility.PUBLIC,
                sender="3号 智能体3",
            ),
        ),
    )

    result = LLMController(client).act(
        view,
        ActionRequest(
            ActionKind.VOTE,
            "请投票",
            (ActionOption("p3", "3号 智能体3"),),
        ),
    )

    assert result.choice == "p3"
    assert len(captured) == 1
    assert {tool["function"]["name"] for tool in captured[0]["tools"]} == {
        "get_evidence_ledger",
        "search_visible_history",
        "get_player_dossier",
        "get_vote_analysis",
        "get_claim_matrix",
        "review_action_draft",
    }
    assert "tool_choice" not in captured[0]


def test_structured_strategy_is_private_prompt_context() -> None:
    """The latest belief state should be readable next turn but never public."""
    strategy = StrategyState(
        day=2,
        phase="vote",
        beliefs=(
            PlayerBelief(
                player_id="p3",
                suspicion=75,
                confidence=60,
                evidence_sequences=(8,),
                rationale="八号事件中改票。",
            ),
        ),
        open_questions=("三号身份声明是否一致？",),
        plan="优先核对三号。",
        counter_case="三号可能是被迫改票。",
    )

    controller = LLMController(
        OpenAICompatibleClient(
            LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        ),
    )
    messages = controller._messages(  # noqa: SLF001
        replace(seat_view(), strategy=strategy),
        ActionRequest(ActionKind.SPEAK, "请发言", requires_text=True),
    )

    assert "结构化策略状态" in messages[-2]["content"]
    assert '"suspicion": 75' in messages[-2]["content"]
    assert "优先核对三号" not in str(messages[:3])


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


def test_usage_reads_vendor_specific_cache_fields() -> None:
    """DeepSeek and Anthropic-compatible gateways name their cache field differently."""
    usages = [
        {
            "prompt_tokens": 1000,
            "completion_tokens": 10,
            "prompt_cache_hit_tokens": 640,
            "prompt_cache_miss_tokens": 360,
        },
        {
            "prompt_tokens": 1000,
            "completion_tokens": 10,
            "cache_read_input_tokens": 360,
        },
    ]

    def transport(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "choices": [{"message": {"content": '{"text":"收到"}'}}],
            "usage": usages.pop(0),
        }

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="deepseek-chat"),
        transport=transport,
    )

    client.complete([{"role": "user", "content": "一"}])
    client.complete([{"role": "user", "content": "二"}])

    assert client.observed_input_tokens == 2000
    assert client.observed_cached_tokens == 1000
    assert client.observed_cache_reports == 2
    assert client.observed_cache_hit_rate == 0.5


def test_cache_hit_rate_is_unknown_when_usage_omits_cache_fields() -> None:
    """Gateways that trim usage leave the cached share unmeasured, not zero."""

    def transport(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "choices": [{"message": {"content": '{"text":"收到"}'}}],
            "usage": {"prompt_tokens": 5825, "completion_tokens": 16},
        }

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )

    client.complete([{"role": "user", "content": "一"}])

    assert client.observed_input_tokens == 5825
    assert client.observed_cache_reports == 0
    assert client.observed_cache_hit_rate is None


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

    assert first[:-1] == second[:-1]
    assert "稳定的公开历史" in first[1]["content"]
    assert "当前法官请求" not in first[1]["content"]
    assert "当前法官请求" in first[-1]["content"]
    assert first[-1] != second[-1]


def test_llm_shares_public_prefix_before_isolating_private_context() -> None:
    """Players should reuse public prompt tokens without mixing their secrets."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.invalid/v1",
            model="test",
            wire_api="responses",
            prompt_cache=True,
        ),
    )
    public_event = MemoryEvent(
        sequence=1,
        day=1,
        phase="discussion",
        text="法官宣布昨夜平安夜",
        visibility=Visibility.PUBLIC,
    )
    first_view = replace(
        seat_view(),
        events=(
            public_event,
            MemoryEvent(
                sequence=2,
                day=1,
                phase="night",
                text="一号的私密查验",
                visibility=Visibility.PRIVATE,
            ),
        ),
        thoughts=(Thought(day=1, phase="night", text="一号的私密计划"),),
    )
    second_view = replace(
        seat_view(),
        player_id="p3",
        name="智能体3",
        role=Role.WEREWOLF,
        role_name="狼人",
        role_description="夜间参与狼聊",
        faction=Faction.WEREWOLF,
        seat_number=3,
        events=(
            public_event,
            MemoryEvent(
                sequence=3,
                day=1,
                phase="night",
                text="三号的私密狼聊",
                visibility=Visibility.WEREWOLF,
            ),
        ),
        thoughts=(Thought(day=1, phase="night", text="三号的私密计划"),),
    )

    first = LLMController(client)._messages(  # noqa: SLF001
        first_view,
        ActionRequest(ActionKind.SPEAK, "请发言"),
    )
    second = LLMController(client)._messages(  # noqa: SLF001
        second_view,
        ActionRequest(ActionKind.SPEAK, "请发言"),
    )

    assert first[:3] == second[:3]
    assert "法官宣布昨夜平安夜" in first[1]["content"]
    assert "一号的私密查验" not in str(first[:3])
    assert "三号的私密狼聊" not in str(second[:3])
    assert "一号的私密查验" in first[4]["content"]
    assert "一号的私密计划" in first[5]["content"]
    assert "三号的私密狼聊" in second[4]["content"]
    assert "三号的私密计划" in second[5]["content"]
    first_key = client._payload(first)["prompt_cache_key"]  # noqa: SLF001
    second_key = client._payload(second)["prompt_cache_key"]  # noqa: SLF001
    later_public_view = replace(
        first_view,
        events=(
            public_event,
            MemoryEvent(
                sequence=4,
                day=1,
                phase="discussion",
                text="新增公开发言",
                visibility=Visibility.PUBLIC,
            ),
            first_view.events[1],
        ),
    )
    later_key = client._payload(  # noqa: SLF001
        LLMController(client)._messages(  # noqa: SLF001
            later_public_view,
            ActionRequest(ActionKind.VOTE, "请投票"),
        ),
    )["prompt_cache_key"]
    assert first_key != second_key
    assert first_key == later_key


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
    public_state = messages[2]["content"]

    assert '"seat": 8' in public_state
    assert '"alive": false' in public_state
    assert "至多1名存活狼人" in public_state


def seat_view(language: str = "zh-CN") -> PlayerView:
    """Return a minimal seated view for prompt and answer parsing tests."""
    return PlayerView(
        player_id="p1",
        name="智能体5",
        role=Role.VILLAGER,
        role_name="平民",
        role_description="没有夜间技能",
        faction=Faction.GOOD,
        lover=None,
        alive_players=(("p1", "智能体5"), ("p3", "智能体3")),
        dead_players=(),
        events=(),
        thoughts=(),
        skills=(),
        day=1,
        phase="discussion",
        language=language,
        seat_number=1,
        seat_players=(("p1", 1, "智能体5"), ("p3", 3, "智能体3")),
    )


def test_llm_prompt_states_the_public_seat_the_player_must_claim() -> None:
    """An agent named like a seat still has to introduce its own seat number."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
    )
    controller = LLMController(client)

    messages = controller._messages(  # noqa: SLF001
        seat_view(),
        ActionRequest(ActionKind.SPEAK, "请发言", requires_text=True),
    )

    assert '"public_label": "1号 智能体5"' in messages[3]["content"]
    assert '"role": "平民"' not in str(messages[:3])
    assert "名字恰好是“你”" in messages[0]["content"]
    assert "不得用后来出现的白天发言" in messages[0]["content"]
    assert "不得逐句复述" in messages[0]["content"]
    assert "text 必须非空" in messages[-1]["content"]
    assert "必须提供 text：True" in messages[-1]["content"]


def test_llm_receives_the_judge_reason_for_a_rejected_answer() -> None:
    """A retry must tell the model what was wrong instead of repeating itself."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
    )
    controller = LLMController(client)
    request = ActionRequest(
        ActionKind.VOTE,
        "请投票",
        (ActionOption("p3", "3号 智能体3"),),
        allow_abstain=True,
    )

    first = controller._messages(seat_view(), request)  # noqa: SLF001
    retried = controller._messages(  # noqa: SLF001
        seat_view(),
        replace(request, retry_feedback="choice 不在合法选项内"),
    )

    assert first[:2] == retried[:2]
    assert "判定你上一次的回答无效" not in first[-1]["content"]
    assert "choice 不在合法选项内" in retried[-1]["content"]


def test_llm_choice_accepts_seat_number_label_and_name() -> None:
    """Models answer with the seat or name far more often than the raw value."""
    request = ActionRequest(
        ActionKind.VOTE,
        "请投票",
        (ActionOption("p3", "3号 智能体3"), ActionOption("p4", "4号 智能体4")),
        allow_abstain=True,
    )

    resolve = LLMController._resolve_choice  # noqa: SLF001

    assert resolve("p3", request) == "p3"
    assert resolve("3号 智能体3", request) == "p3"
    assert resolve("3号", request) == "p3"
    assert resolve(3, request) == "p3"
    assert resolve("智能体3", request) == "p3"
    assert resolve("我投 p4", request) == "p4"
    assert resolve("p3 或 p4", request) == "p3 或 p4"
    assert resolve("p9", request) == "p9"


def test_llm_choice_keeps_double_digit_seats_apart() -> None:
    """A ten-plus seat table must not read ``p10`` as the option ``p1``."""
    request = ActionRequest(
        ActionKind.VOTE,
        "请投票",
        tuple(
            ActionOption(f"p{seat}", f"{seat}号 智能体{seat}") for seat in range(1, 13)
        ),
        allow_abstain=True,
    )

    resolve = LLMController._resolve_choice  # noqa: SLF001

    assert resolve("我投 p10", request) == "p10"
    assert resolve("10号", request) == "p10"
    assert resolve("vote for p1", request) == "p1"
    assert resolve("p1 和 p2 都可疑", request) == "p1 和 p2 都可疑"


def test_llm_only_abstains_where_the_judge_allows_it() -> None:
    """Refusal wording resolves to abstain, but never for a mandatory ability."""
    optional = ActionRequest(
        ActionKind.WITCH_SAVE,
        "是否使用解药？",
        (ActionOption("save", "使用解药"),),
        allow_abstain=True,
    )
    mandatory = ActionRequest(
        ActionKind.SEER_INSPECT,
        "查验谁？",
        (ActionOption("p3", "3号 智能体3"),),
    )

    resolve = LLMController._resolve_choice  # noqa: SLF001

    assert resolve("不使用解药", optional) is None
    assert resolve("弃权", optional) is None
    assert resolve("yes", optional) == "save"
    assert resolve("使用解药", optional) == "save"
    assert resolve("弃权", mandatory) == "弃权"


def test_llm_parser_recovers_the_object_from_a_chatty_answer() -> None:
    """A trailing sentence or second object must not discard a valid decision."""
    parsed = LLMController._parse_json(  # noqa: SLF001
        '好的，我的回答是：{"choice": "p3", "text": "投三号"} 希望有帮助。{"note": "忽略"}',
    )

    assert parsed["choice"] == "p3"
    assert parsed["text"] == "投三号"


def test_empty_chat_answer_names_the_output_limit_as_its_cause() -> None:
    """A silent player needs a visible reason instead of a raw response dump."""

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        }

    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
        transport=transport,
    )

    with pytest.raises(RuntimeError, match="finish_reason=length"):
        client.complete([{"role": "user", "content": "行动"}])


def test_provider_http_error_exposes_only_safe_structured_metadata() -> None:
    """An echoed private request body must never enter a printable exception."""
    headers = Message()
    headers["x-request-id"] = "req-safe_123"
    headers["retry-after"] = "3.5"
    body = io.BytesIO(
        b'{"error":{"code":"bad_request","param":"messages",'
        b'"message":"ULTRA_PRIVATE_WOLF_CHAT"}}',
    )
    raw = urllib.error.HTTPError(
        "https://example.invalid/v1/chat/completions",
        400,
        "bad request",
        headers,
        body,
    )

    error = OpenAICompatibleClient._http_error(raw)  # noqa: SLF001

    assert isinstance(error, ProviderHTTPError)
    assert error.status_code == 400
    assert error.error_code == "bad_request"
    assert error.request_id == "req-safe_123"
    assert error.retry_after_seconds == 3.5
    assert "ULTRA_PRIVATE_WOLF_CHAT" not in str(error)
    assert "messages" not in str(error)


def test_provider_http_error_identifies_an_explicit_unsupported_field() -> None:
    """Compatibility fallback metadata comes from structured 4xx details only."""
    body = io.BytesIO(
        b'{"error":{"code":"unknown_parameter","param":"stream_options",'
        b'"message":"Unsupported parameter"}}',
    )
    raw = urllib.error.HTTPError(
        "https://example.invalid/v1/chat/completions",
        400,
        "bad request",
        Message(),
        body,
    )

    error = OpenAICompatibleClient._http_error(raw)  # noqa: SLF001

    assert error.unsupported_field == "stream_options"
    assert "stream_options" not in str(error)


def test_authenticated_provider_requests_disable_all_redirects() -> None:
    """A redirect must not create a second request carrying private headers."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.invalid/v1",
            model="test",
            api_key="TOP-SECRET",
        ),
    )
    request = client._request({"model": "test"})  # noqa: SLF001
    redirect_handler = next(
        handler
        for handler in getattr(client._opener(), "handlers")  # noqa: B009, SLF001
        if isinstance(handler, agents_module._NoRedirectHandler)  # noqa: SLF001
    )

    redirected = redirect_handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://attacker.invalid/collect",
    )

    assert request.get_header("Authorization") == "Bearer TOP-SECRET"
    assert redirected is None


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (
            "https://example.test/v1?api-version=2024-10-21",
            "https://example.test/v1/chat/completions?api-version=2024-10-21",
        ),
        (
            "https://example.test/v1/chat/completions?api-version=2024-10-21",
            "https://example.test/v1/chat/completions?api-version=2024-10-21",
        ),
    ],
)
def test_provider_endpoint_preserves_queries_and_complete_paths(
    base_url: str,
    expected: str,
) -> None:
    """Azure-style query parameters belong after the selected endpoint path."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url=base_url, model="test"),
    )

    assert client._request({"model": "test"}).full_url == expected  # noqa: SLF001


def test_provider_endpoint_rejects_fragments() -> None:
    """Fragments are client-side identifiers and cannot identify an API endpoint."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.test/v1#private-fragment",
            model="test",
        ),
    )

    with pytest.raises(ValueError, match="fragment"):
        client._request({"model": "test"})  # noqa: SLF001


def test_chat_stream_requests_usage_and_retries_without_it_when_rejected() -> None:
    """Chat streams report no usage unless asked, and old services reject asking."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
    )
    sent: list[dict[str, Any]] = []

    def fake_read(payload: dict[str, Any]) -> str:
        sent.append(payload)
        if "stream_options" in payload:
            raise ProviderHTTPError(400, unsupported_field="stream_options")
        return '{"text":"完成"}'

    client._read_stream = fake_read  # type: ignore[assignment]  # noqa: SLF001

    content = client._post_stream({"model": "test"})  # noqa: SLF001

    assert content == '{"text":"完成"}'
    assert sent[0]["stream"] is True
    assert sent[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in sent[1]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("HTTP 500 body happened to mention stream_options"),
        ProviderHTTPError(500, unsupported_field="stream_options"),
        ProviderHTTPError(400, unsupported_field="another_field"),
        ProviderProtocolError("Malformed streaming event mentioning stream_options"),
    ],
)
def test_chat_stream_does_not_retry_on_unstructured_or_non_4xx_errors(
    error: RuntimeError,
) -> None:
    """Only an explicit unsupported-field response may cause a second inference."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
    )
    sent: list[dict[str, Any]] = []

    def fake_read(payload: dict[str, Any]) -> str:
        sent.append(payload)
        raise error

    client._read_stream = fake_read  # type: ignore[assignment]  # noqa: SLF001

    with pytest.raises(type(error), match=r"stream_options|HTTP 500|request failed"):
        client._post_stream({"model": "test"})  # noqa: SLF001

    assert len(sent) == 1


def test_sse_done_terminates_without_waiting_for_connection_close() -> None:
    """The protocol terminator should stop before a provider's trailing bytes."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
    )

    def lines():
        yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n'
        yield b"data: [DONE]\n"
        pytest.fail("the client read beyond the SSE terminator")

    assert client._stream_content(lines()) == "done"  # noqa: SLF001


def test_stream_heartbeats_cannot_extend_the_total_deadline() -> None:
    """Frequent bytes may avoid an inactivity timeout but not the hard deadline."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.invalid/v1",
            model="test",
            timeout=0.05,
        ),
    )

    def heartbeats():
        while True:
            time.sleep(0.02)
            yield b": keepalive\n"

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="total timeout"):
        client._stream_content(heartbeats())  # noqa: SLF001

    assert time.monotonic() - started < 0.2


def test_non_streaming_response_body_has_a_hard_size_limit() -> None:
    """A provider cannot make the client accumulate an unbounded JSON body."""

    class OversizedBody:
        def __init__(self) -> None:
            self.remaining = agents_module.MAX_RESPONSE_BYTES + 1

        def read1(self, size: int) -> bytes:
            count = min(size, self.remaining)
            self.remaining -= count
            return b"x" * count

    with pytest.raises(RuntimeError, match="size limit"):
        OpenAICompatibleClient._read_bounded_body(  # noqa: SLF001
            OversizedBody(),
            time.monotonic() + 1,
        )


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


def test_history_sections_share_one_total_context_budget() -> None:
    """Separating cache lanes must not multiply the configured context limit."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(base_url="https://example.invalid/v1", model="test"),
    )
    controller = LLMController(client, context_char_limit=2000)

    sections = controller._trim_history_sections(  # noqa: SLF001
        "公开" * 1000,
        "私密" * 1000,
        "策略" * 1000,
    )

    assert sum(map(len, sections)) <= controller.context_char_limit
    assert all(section.startswith("[较早内容因上下文长度省略]") for section in sections)


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


def test_terminal_transient_progress_is_muted_and_erased(monkeypatch) -> None:
    """Live waiting state should look subdued and leave no completed line behind."""

    class TTYBuffer(io.StringIO):
        def isatty(self) -> bool:
            return True

    output = TTYBuffer()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(agents_module.sys, "stdout", output)
    terminal = Terminal(clear_screen=False)

    terminal.transient_progress("LLM high 推理中：1 秒")
    terminal.clear_transient_progress()

    rendered = output.getvalue()
    assert "\033[38;5;244m[观战]" in rendered
    assert rendered.endswith("\r\033[2K")

    output.seek(0)
    output.truncate()
    monkeypatch.setenv("NO_COLOR", "1")
    terminal.transient_progress("等待 Provider")

    assert "\033[2m[观战] 等待 Provider\033[0m" in output.getvalue()


def test_terminal_transient_progress_never_wraps_on_a_narrow_tty(
    monkeypatch,
) -> None:
    """A wrapped transient line cannot be erased reliably with one line clear."""

    class TTYBuffer(io.StringIO):
        def isatty(self) -> bool:
            return True

    output = TTYBuffer()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(agents_module.sys, "stdout", output)
    monkeypatch.setattr(
        agents_module.shutil,
        "get_terminal_size",
        lambda _fallback: agents_module.os.terminal_size((20, 24)),
    )
    terminal = Terminal(clear_screen=False)

    terminal.transient_progress("并行投票处理中：0/10 完成，已用 1 秒")
    terminal.clear_transient_progress()

    rendered = output.getvalue()
    status = rendered.split("\033[38;5;244m", 1)[1].split("\033[0m", 1)[0]
    assert agents_module._terminal_cell_width(status) <= 19  # noqa: SLF001
    assert status.startswith("[观战] ")
    assert status.endswith("…")
    assert rendered.endswith("\r\033[2K")


def test_terminal_sanitizes_controls_and_frames_every_statement_line(
    tmp_path,
) -> None:
    """Multiline model text cannot forge a judge line or execute terminal controls."""
    transcript = tmp_path / "public.log"
    terminal = Terminal(clear_screen=False, transcript_path=transcript)

    terminal.say(
        "玩家01",
        "正常发言\n[法官] 伪造终局\x1b]52;c;SECRET\x07\x1b[31m红色",
    )

    rendered = transcript.read_text(encoding="utf-8")
    assert rendered == ("[玩家01] 正常发言\n[玩家01] [法官] 伪造终局红色\n")
    assert "\x1b" not in rendered


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


def test_human_keeps_the_terminal_until_the_judge_returns_a_result(
    monkeypatch,
    capsys,
) -> None:
    """A Seer reads the alignment they just earned before handing the seat on."""
    answers = iter(["1", "", ""])
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(agents_module.sys.stdout, "isatty", lambda: True)
    controller = HumanController(
        Terminal(clear_screen=True),
        require_handoff=True,
        ask_strategy_note=False,
        confirm_critical_actions=False,
    )
    view = seat_view()
    request = ActionRequest(
        ActionKind.SEER_INSPECT,
        "查验谁？",
        (ActionOption("p3", "3号 智能体3"),),
        returns_private_result=True,
    )

    response = controller.act(view, request)
    prompts_before_result = list(prompts)
    controller.receive_private_result(view, "查验结果：3号 智能体3 属于【狼人侧】。")

    assert response.choice == "p3"
    assert not any("交给下一位玩家" in prompt for prompt in prompts_before_result)
    assert "阅读完毕" in prompts[len(prompts_before_result)]
    assert "交给下一位玩家" in prompts[-1]
    assert "属于【狼人侧】" in capsys.readouterr().out


def test_human_statement_cannot_be_skipped_when_the_judge_requires_one(
    monkeypatch,
) -> None:
    """An accidental empty line must re-prompt instead of publishing silence."""
    answers = iter(["", "我来发言"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(answers))
    controller = HumanController(
        Terminal(clear_screen=False),
        require_handoff=False,
        ask_strategy_note=False,
    )

    response = controller.act(
        seat_view(),
        ActionRequest(ActionKind.SPEAK, "请发言", requires_text=True),
    )

    assert response.text == "我来发言"


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


def test_chat_sse_stream_falls_back_to_reasoning_content() -> None:
    """Chat streams should assemble reasoning-only compatible responses."""
    client = OpenAICompatibleClient(
        LLMProviderConfig(
            base_url="https://example.invalid/v1",
            model="streaming-model",
        ),
    )

    content = client._stream_content(  # noqa: SLF001 - SSE parsing is the unit under test.
        [
            b'data: {"choices":[{"delta":{"reasoning_content":"{\\"text\\":\\"ni"}}]}\n',
            b'data: {"choices":[{"delta":{"reasoning_content":"hao\\"}"}}]}\n',
            b"data: [DONE]\n",
        ],
    )

    assert content == '{"text":"nihao"}'
