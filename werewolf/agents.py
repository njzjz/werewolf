"""Human, local-bot, and OpenAI-compatible LLM player controllers."""

from __future__ import annotations

import http.client
import json
import random
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from .models import (
    ActionKind,
    ActionRequest,
    AgentResponse,
    PlayerView,
    Visibility,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .config import LLMProviderConfig


def _create_ipv4_connection(
    address: tuple[str, int],
    timeout: float | None = None,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Connect through IPv4 when a host's unreachable IPv6 wins DNS ordering."""
    host, port = address
    last_error: OSError | None = None
    for family, sock_type, proto, _, socket_address in socket.getaddrinfo(
        host,
        port,
        socket.AF_INET,
        socket.SOCK_STREAM,
    ):
        sock = socket.socket(family, sock_type, proto)
        try:
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(socket_address)
        except OSError as exc:
            last_error = exc
            sock.close()
            continue
        return sock
    if last_error:
        raise last_error
    msg = f"No IPv4 address found for {host}"
    raise OSError(msg)


class _IPv4HTTPConnection(http.client.HTTPConnection):
    """HTTP connection that keeps standard behavior but forces IPv4 dialing."""

    def connect(self) -> None:
        self._create_connection = _create_ipv4_connection
        super().connect()


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that preserves SNI/cert checks while forcing IPv4."""

    def connect(self) -> None:
        self._create_connection = _create_ipv4_connection
        super().connect()


class _IPv4HTTPHandler(urllib.request.HTTPHandler):
    """Route urllib HTTP requests through the IPv4 connection class."""

    def http_open(self, request: urllib.request.Request) -> object:
        return self.do_open(_IPv4HTTPConnection, request)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    """Route urllib HTTPS requests through the IPv4 connection class."""

    def https_open(self, request: urllib.request.Request) -> object:
        return self.do_open(
            _IPv4HTTPSConnection,
            request,
            context=self._context,
        )


class Controller(Protocol):
    """Minimal interface implemented by every kind of player."""

    def act(self, view: PlayerView, request: ActionRequest) -> AgentResponse:
        """Return one legal choice or a piece of speech."""


class Terminal:
    """Small terminal adapter that supports pass-and-play privacy."""

    def __init__(self, *, clear_screen: bool = True) -> None:
        self.clear_screen = clear_screen

    def clear(self) -> None:
        """Clear only interactive terminals; captured logs remain readable."""
        if self.clear_screen and sys.stdout.isatty():
            print("\033[2J\033[H", end="", flush=True)

    def announce(self, text: str) -> None:
        """Print a public judge announcement."""
        print(f"\n[法官] {text}", flush=True)

    def private_turn(self, view: PlayerView) -> None:
        """Render only the active human's already-authorized memory."""
        self.clear()
        if view.language == "en":
            print(f"=== Private turn: {view.name} | Role: {view.role_name} ===")
            print(view.role_description)
        else:
            print(f"=== {view.name} 的私密回合 | 身份：{view.role_name} ===")
            print(view.role_description)
        recent = view.events[-18:]
        if recent:
            title = (
                "Recent authorized information"
                if view.language == "en"
                else "最近可见信息"
            )
            print(f"\n--- {title} ---")
            for event in recent:
                marker = {
                    Visibility.PUBLIC: "公开",
                    Visibility.PRIVATE: "私密",
                    Visibility.WEREWOLF: "狼队",
                }[event.visibility]
                if view.language == "en":
                    marker = event.visibility.value
                print(f"[{marker}] {event.text}")
        if view.thoughts:
            title = (
                "Your latest strategy note"
                if view.language == "en"
                else "你的最近策略笔记"
            )
            print(f"\n--- {title} ---\n{view.thoughts[-1].text}")


class HumanController:
    """Read decisions from the person currently using the terminal."""

    def __init__(self, terminal: Terminal) -> None:
        self.terminal = terminal

    def act(self, view: PlayerView, request: ActionRequest) -> AgentResponse:
        """Collect a validated choice and an optional private strategy note."""
        self.terminal.private_turn(view)
        print(f"\n{request.prompt}")
        if request.kind in {
            ActionKind.SPEAK,
            ActionKind.LAST_WORDS,
            ActionKind.TEAM_CHAT,
        }:
            text = input("> ").strip()
            thought = self._thought(view)
            self._handoff(view)
            return AgentResponse(text=text, thought=thought)
        choice = self._choose(view, request)
        thought = self._thought(view)
        self._handoff(view)
        return AgentResponse(choice=choice, thought=thought)

    @staticmethod
    def _thought(view: PlayerView) -> str:
        prompt = (
            "Private strategy note (optional): "
            if view.language == "en"
            else "私密策略笔记（可留空）："
        )
        return input(prompt).strip()

    @staticmethod
    def _choose(view: PlayerView, request: ActionRequest) -> str | None:
        for index, option in enumerate(request.options, start=1):
            print(f"  {index}. {option.label}")
        abstain_label = "Abstain" if view.language == "en" else "弃权/不使用"
        if request.allow_abstain:
            print(f"  0. {abstain_label}")
        legal = {
            str(index): option.value
            for index, option in enumerate(request.options, start=1)
        }
        while True:
            raw = input("> ").strip()
            if request.allow_abstain and raw in {"", "0"}:
                return None
            if raw in legal:
                return legal[raw]
            retry = (
                "Please enter a listed number."
                if view.language == "en"
                else "请输入列表中的编号。"
            )
            print(retry)

    def _handoff(self, view: PlayerView) -> None:
        if self.terminal.clear_screen and sys.stdout.isatty():
            prompt = (
                "Press Enter and pass the terminal..."
                if view.language == "en"
                else "回车后将终端交给下一位玩家……"
            )
            input(prompt)
            self.terminal.clear()


@dataclass
class OpenAICompatibleClient:
    """Standard-library client for chat-completions and Responses endpoints."""

    config: LLMProviderConfig
    transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def complete(self, messages: list[dict[str, str]]) -> str:
        """Return assistant content from an OpenAI-compatible response."""
        payload = self._payload(messages)
        response = self.transport(payload) if self.transport else self._post(payload)
        if self.config.wire_api == "responses":
            return self._responses_content(response)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            msg = f"Malformed chat-completions response: {response!r}"
            raise RuntimeError(msg) from exc
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content)

    def _payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """Build the request shape selected by the provider configuration."""
        if self.config.wire_api == "responses":
            payload: dict[str, Any] = {
                "model": self.config.model,
                "input": messages,
                "max_output_tokens": self.config.max_tokens,
                "store": False,
            }
            if self.config.reasoning_effort:
                payload["reasoning"] = {"effort": self.config.reasoning_effort}
            if self.config.use_json_mode:
                payload["text"] = {"format": {"type": "json_object"}}
            return payload
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if self.config.use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    @staticmethod
    def _responses_content(response: dict[str, Any]) -> str:
        """Extract text from standard and common compatible Responses shapes."""
        output_text = response.get("output_text")
        if isinstance(output_text, str) and output_text:
            return output_text
        parts: list[str] = []
        for item in response.get("output", []):
            if not isinstance(item, dict):
                continue
            parts.extend(
                content["text"]
                for content in item.get("content", [])
                if isinstance(content, dict) and isinstance(content.get("text"), str)
            )
        if parts:
            return "".join(parts)
        msg = f"Malformed Responses API response: {response!r}"
        raise RuntimeError(msg)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = self.config.base_url.rstrip("/")
        suffix = (
            "/responses" if self.config.wire_api == "responses" else "/chat/completions"
        )
        if not endpoint.endswith(suffix):
            endpoint += suffix
        if urllib.parse.urlparse(endpoint).scheme not in {"http", "https"}:
            msg = "LLM base_url must use http:// or https://"
            raise ValueError(msg)
        headers = {"Content-Type": "application/json", **self.config.extra_headers}
        api_key = self.config.resolved_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(  # noqa: S310 - URL is an explicit user configuration.
            endpoint,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            opener = (
                urllib.request.build_opener(_IPv4HTTPHandler(), _IPv4HTTPSHandler())
                if self.config.force_ipv4
                else urllib.request.build_opener()
            )
            with opener.open(request, timeout=self.config.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            msg = f"LLM API returned HTTP {exc.code}: {detail}"
            raise RuntimeError(msg) from exc
        except urllib.error.URLError as exc:
            msg = f"Could not reach LLM API: {exc.reason}"
            raise RuntimeError(msg) from exc


class LLMController:
    """Prompt one isolated LLM context and parse its structured action."""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        *,
        persona: str = "",
        context_char_limit: int = 24000,
    ) -> None:
        self.client = client
        self.persona = persona
        self.context_char_limit = context_char_limit

    def act(self, view: PlayerView, request: ActionRequest) -> AgentResponse:
        """Ask for JSON so private thought and external action cannot mix."""
        messages = self._messages(view, request)
        raw = self.client.complete(messages)
        data = self._parse_json(raw)
        return AgentResponse(
            choice=self._optional_string(data.get("choice")),
            text=self._optional_string(data.get("text")) or "",
            thought=self._optional_string(data.get("thought")) or "",
            note=self._optional_string(data.get("note")) or "",
        )

    def _messages(
        self,
        view: PlayerView,
        request: ActionRequest,
    ) -> list[dict[str, str]]:
        language_rule = (
            "所有输出必须使用中文。"
            if view.language == "zh-CN"
            else "Use English for all output."
        )
        skills = (
            "\n".join(
                f"- {skill.description}: {skill.instructions}" for skill in view.skills
            )
            or "- 无额外技能"
        )
        system = (
            "你正在参加一局狼人杀。你只能依据下面提供的个人视图行动；未出现的信息对你不可见，"
            "不得假设或索取其他玩家的私密上下文。法官是确定性程序，必须服从合法选项。\n"
            f"{language_rule}\n你的名字：{view.name}\n你的身份：{view.role_name}\n"
            f"身份说明：{view.role_description}\n人物设定：{self.persona or '自然参与游戏'}\n"
            f"个人技能：\n{skills}\n"
            "仅返回一个 JSON 对象：choice 是选项 value 或 null；text 是要公开/狼队发送的内容；"
            "thought 是仅写入你个人记忆的简短策略与判断；note 可记录待验证事项。"
        )
        event_lines = [
            f"#{event.sequence} D{event.day}/{event.phase} [{event.visibility.value}] {event.text}"
            for event in view.events
        ]
        thought_lines = [
            f"D{item.day}/{item.phase}: {item.text}" for item in view.thoughts
        ]
        history = "\n".join([*event_lines, "--- 私密策略笔记 ---", *thought_lines])
        if len(history) > self.context_char_limit:
            history = (
                "[较早内容因上下文长度省略]\n" + history[-self.context_char_limit :]
            )
        options = [
            {"value": item.value, "label": item.label} for item in request.options
        ]
        user = (
            f"当前：第 {view.day} 天，阶段 {view.phase}\n"
            f"存活玩家：{[name for _, name in view.alive_players]}\n"
            f"死亡玩家：{[name for _, name in view.dead_players]}\n"
            f"你的可见历史：\n{history or '（暂无）'}\n\n"
            f"法官请求：{request.prompt}\n动作类型：{request.kind.value}\n"
            f"合法选项：{json.dumps(options, ensure_ascii=False)}\n"
            f"允许弃权：{request.allow_abstain}\n"
            "对于发言类动作填写 text；对于选择类动作只把合法 value 填入 choice。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        clean = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            raw.strip(),
            flags=re.IGNORECASE,
        )
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", clean, flags=re.DOTALL)
            if not match:
                msg = f"LLM did not return JSON: {raw[:500]}"
                raise RuntimeError(msg) from None
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                msg = f"LLM returned invalid JSON: {raw[:500]}"
                raise RuntimeError(msg) from exc
        if not isinstance(data, dict):
            msg = "LLM response must be a JSON object"
            raise TypeError(msg)
        return data

    @staticmethod
    def _optional_string(value: object) -> str | None:
        if value is None:
            return None
        return str(value).strip() or None


class BotController:
    """Offline baseline controller used for demos, tests, and API fallbacks."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()  # noqa: S311 - game simulation, not security.

    def act(self, view: PlayerView, request: ActionRequest) -> AgentResponse:
        """Choose only from the supplied legal options without hidden state."""
        thought = self._thought(view, request)
        if request.kind is ActionKind.TEAM_CHAT:
            return AgentResponse(text=self._team_message(view), thought=thought)
        if request.kind in {ActionKind.SPEAK, ActionKind.LAST_WORDS}:
            return AgentResponse(text=self._speech(view, request), thought=thought)
        if not request.options or (request.allow_abstain and self.rng.random() < 0.12):
            return AgentResponse(choice=None, thought=thought)
        if request.kind is ActionKind.WITCH_SAVE and self.rng.random() < 0.65:
            return AgentResponse(choice=request.options[0].value, thought=thought)
        if request.kind is ActionKind.WITCH_POISON and self.rng.random() < 0.7:
            return AgentResponse(choice=None, thought=thought)
        return AgentResponse(
            choice=self.rng.choice(request.options).value,
            thought=thought,
        )

    def _speech(self, view: PlayerView, request: ActionRequest) -> str:
        alive = [
            name
            for player_id, name in view.alive_players
            if player_id != view.player_id
        ]
        target = self.rng.choice(alive) if alive else "其他人"
        if view.language == "en":
            if request.kind is ActionKind.LAST_WORDS:
                return f"My final suspicion is on {target}; review the voting record."
            return (
                f"I am watching {target}. Please compare claims with the public votes."
            )
        if request.kind is ActionKind.LAST_WORDS:
            return f"我的遗言：重点复盘{target}的投票和立场变化。"
        return f"我目前会重点观察{target}，请大家结合公开投票检查发言是否前后一致。"

    def _team_message(self, view: PlayerView) -> str:
        targets = [
            name
            for player_id, name in view.alive_players
            if player_id != view.player_id
        ]
        target = self.rng.choice(targets) if targets else "目标"
        if view.language == "en":
            return f"I suggest attacking {target}; keep our daytime positions separate."
        return f"建议考虑袭击{target}，白天尽量不要让我们的站边完全一致。"

    @staticmethod
    def _thought(view: PlayerView, request: ActionRequest) -> str:
        if view.language == "en":
            return f"Re-evaluate visible evidence before action {request.kind.value}."
        return f"在执行 {request.kind.value} 前重新检查自己的可见信息。"
