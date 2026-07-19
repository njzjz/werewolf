"""Tests for isolated LLM prompting and structured responses."""

from __future__ import annotations

from typing import Any

from werewolf.agents import LLMController, OpenAICompatibleClient, Terminal
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
    assert "messages" not in captured


def test_terminal_persists_only_explicit_public_output(tmp_path) -> None:
    """The spectator transcript should mirror judge, progress, and public speech."""
    transcript = tmp_path / "public.log"
    terminal = Terminal(clear_screen=False, transcript_path=transcript)

    terminal.announce("天亮了。")
    terminal.progress("公开行动处理中……")
    terminal.say("玩家01", "这是公开发言。")

    assert transcript.read_text(encoding="utf-8") == (
        "\n[法官] 天亮了。\n[观战] 公开行动处理中……\n[玩家01] 这是公开发言。\n"
    )


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
            b'data: {"type":"response.completed","response":{}}\n',
            b"data: [DONE]\n",
        ],
    )

    assert content == '{"text":"nihao"}'
