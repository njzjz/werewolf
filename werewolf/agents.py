"""Human, local-bot, and OpenAI-compatible LLM player controllers."""

from __future__ import annotations

import hashlib
import http.client
import json
import random
import re
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .models import (
    ActionKind,
    ActionRequest,
    AgentResponse,
    MemoryEvent,
    PlayerView,
    Visibility,
)

try:
    import readline as _readline
except ImportError:  # pragma: no cover - unavailable on some non-POSIX builds.
    _readline = None

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

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

    def __init__(
        self,
        *,
        clear_screen: bool = True,
        transcript_path: str | Path | None = None,
        reset_transcript: bool = True,
    ) -> None:
        self.clear_screen = clear_screen
        self._output_lock = threading.RLock()
        self._transient_progress_active = False
        self.transcript_path = Path(transcript_path) if transcript_path else None
        if self.transcript_path is not None:
            self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
            if reset_transcript:
                self.transcript_path.write_text("", encoding="utf-8")
            else:
                self.transcript_path.touch(exist_ok=True)

    def clear(self) -> None:
        """Clear only interactive terminals; captured logs remain readable."""
        if self.clear_screen and sys.stdout.isatty():
            with self._output_lock:
                self._clear_transient_progress_locked()
                print("\033[2J\033[H", end="", flush=True)

    def announce(self, text: str) -> None:
        """Print a public judge announcement."""
        self._emit(f"\n[法官] {text}")

    def progress(self, text: str) -> None:
        """Print a persistent spectator event without game secrets."""
        self._emit(f"[观战] {text}")

    def metric(self, text: str, *, label: str = "统计") -> None:
        """Print an end-of-game technical summary distinct from live progress."""
        self._emit(f"[{label}] {text}")

    def notice(self, text: str, *, label: str = "提示") -> None:
        """Print a persistent preflight warning before private roles are assigned."""
        self._emit(f"[{label}] {text}")

    def transient_progress(self, text: str) -> None:
        """Update one in-place TTY status without appending it to public logs."""
        if not sys.stdout.isatty():
            return
        with self._output_lock:
            print(f"\r\033[2K[观战] {text}", end="", flush=True)
            self._transient_progress_active = True

    def clear_transient_progress(self) -> None:
        """Remove the current in-place status line after an action completes."""
        if not sys.stdout.isatty():
            return
        with self._output_lock:
            self._clear_transient_progress_locked()

    def say(
        self,
        player_name: str,
        text: str,
        *,
        fallback_label: str | None = None,
    ) -> None:
        """Print and persist one completed public player statement."""
        marker = f" · {fallback_label}" if fallback_label else ""
        self._emit(f"[{player_name}{marker}] {text}")

    def _emit(self, rendered: str) -> None:
        """Write a public line to stdout and the optional spectator transcript."""
        with self._output_lock:
            self._clear_transient_progress_locked()
            print(rendered, flush=True)
            if self.transcript_path is not None:
                with self.transcript_path.open("a", encoding="utf-8") as file:
                    file.write(rendered + "\n")

    def _clear_transient_progress_locked(self) -> None:
        """Clear transient output while the caller holds ``_output_lock``."""
        if self._transient_progress_active:
            print("\r\033[2K", end="", flush=True)
            self._transient_progress_active = False

    def transcript_size(self) -> int | None:
        """Return the current public transcript size in bytes, if configured."""
        if self.transcript_path is None:
            return None
        return self.transcript_path.stat().st_size

    def truncate_transcript(self, size: int | None) -> None:
        """Roll a transcript back to a checkpoint byte offset without padding it."""
        if self.transcript_path is None or size is None:
            return
        current_size = self.transcript_path.stat().st_size
        if current_size < size:
            msg = (
                f"Transcript {self.transcript_path} is shorter than checkpoint "
                f"offset {size}"
            )
            raise ValueError(msg)
        with self.transcript_path.open("r+b") as file:
            file.truncate(size)

    def private_turn(self, view: PlayerView) -> None:
        """Render only the active human's already-authorized memory."""
        self.clear()
        seat = f"{view.seat_number}号 " if view.seat_number else ""
        if view.language == "en":
            seat = f"Seat {view.seat_number} " if view.seat_number else ""
            print(f"=== Private turn: {seat}{view.name} | Role: {view.role_name} ===")
            print(view.role_description)
        else:
            print(f"=== {seat}{view.name} 的私密回合 | 身份：{view.role_name} ===")
            print(view.role_description)
        if view.lover:
            lover_label = "Lover" if view.language == "en" else "恋人"
            print(f"{lover_label}: {view.lover[1]}")
        self._render_state(view)
        recent = self._recent_events(view)
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
                    Visibility.LOVERS: "恋人",
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

    @staticmethod
    def full_history(view: PlayerView) -> None:
        """Render the complete authorized timeline on explicit human request."""
        title = (
            "Complete authorized history" if view.language == "en" else "完整可见历史"
        )
        print(f"\n=== {title} ===")
        last_group: tuple[int, str] | None = None
        for event in view.events:
            group = (event.day, event.phase)
            if group != last_group:
                print(f"\n--- D{event.day} / {event.phase} ---")
                last_group = group
            marker = (
                event.visibility.value
                if view.language == "en"
                else {
                    Visibility.PUBLIC: "公开",
                    Visibility.PRIVATE: "私密",
                    Visibility.WEREWOLF: "狼队",
                    Visibility.LOVERS: "恋人",
                }[event.visibility]
            )
            print(f"[{marker}] {event.text}")

    @staticmethod
    def _recent_events(view: PlayerView) -> tuple[MemoryEvent, ...]:
        """Keep the active day readable while retaining key older milestones."""
        current = [event for event in view.events if event.day == view.day]
        important_words = (
            "游戏开始",
            "Game begins",
            "公开投票结果",
            "Public votes",
            "被放逐",
            "eliminated",
            "昨夜死亡",
            "night's deaths",
            "平安夜",
            "Nobody died",
        )
        older_important = [
            event
            for event in view.events
            if event.day < view.day
            and any(word in event.text for word in important_words)
        ]
        selected = [*older_important[-5:], *current]
        return tuple(selected[-16:])

    @staticmethod
    def _render_state(view: PlayerView) -> None:
        """Show a compact public-state panel before the authorized timeline."""
        if not view.seat_players:
            return
        alive_ids = {player_id for player_id, _ in view.alive_players}
        alive = [
            (f"Seat {seat} {name}" if view.language == "en" else f"{seat}号 {name}")
            for player_id, seat, name in view.seat_players
            if player_id in alive_ids
        ]
        dead = [
            (f"Seat {seat} {name}" if view.language == "en" else f"{seat}号 {name}")
            for player_id, seat, name in view.seat_players
            if player_id not in alive_ids
        ]
        if view.language == "en":
            print(f"\n--- Public state | Day {view.day} · {view.phase} ---")
            print(f"Alive ({len(alive)}): {', '.join(alive)}")
            print(f"Dead ({len(dead)}): {', '.join(dead) if dead else 'none'}")
        else:
            print(f"\n--- 公共状态 | 第 {view.day} 天 · {view.phase} ---")
            print(f"存活（{len(alive)}）：{'、'.join(alive)}")
            print(f"死亡（{len(dead)}）：{'、'.join(dead) if dead else '无'}")
        if view.mechanical_context:
            print(view.mechanical_context)


class HumanController:
    """Read decisions from the person currently using the terminal."""

    def __init__(
        self,
        terminal: Terminal,
        *,
        require_handoff: bool = True,
        ask_strategy_note: bool = True,
        confirm_critical_actions: bool = True,
    ) -> None:
        self.terminal = terminal
        self.require_handoff = require_handoff
        self.ask_strategy_note = ask_strategy_note
        self.confirm_critical_actions = confirm_critical_actions
        self._enable_line_editing()

    @staticmethod
    def _enable_line_editing() -> None:
        """Enable Unicode-aware deletion and cursor movement when readline exists."""
        if _readline is None:
            return
        for binding in (
            "set editing-mode emacs",
            '"\\e[D": backward-char',
            '"\\e[C": forward-char',
        ):
            _readline.parse_and_bind(binding)

    def act(self, view: PlayerView, request: ActionRequest) -> AgentResponse:
        """Collect a validated choice and an optional private strategy note."""
        self.terminal.private_turn(view)
        print(f"\n{request.prompt}")
        print(
            "Type /history to view the complete authorized timeline."
            if view.language == "en"
            else "输入 /history 可查看完整可见历史。",
        )
        if request.kind in {
            ActionKind.SPEAK,
            ActionKind.LAST_WORDS,
            ActionKind.TEAM_CHAT,
            ActionKind.LOVER_CHAT,
        }:
            text = self._read_text(view)
            thought = self._thought(view) if self.ask_strategy_note else ""
            self._handoff(view)
            return AgentResponse(text=text, thought=thought)
        choice = self._choose(view, request)
        thought = self._thought(view) if self.ask_strategy_note else ""
        self._handoff(view)
        return AgentResponse(choice=choice, thought=thought)

    def _read_text(self, view: PlayerView) -> str:
        """Read speech while reserving an explicit full-history command."""
        while True:
            text = input("> ").strip()
            if text == "/history":
                self.terminal.full_history(view)
                continue
            return text

    @staticmethod
    def _thought(view: PlayerView) -> str:
        prompt = (
            "Private strategy note (optional): "
            if view.language == "en"
            else "私密策略笔记（可留空）："
        )
        return input(prompt).strip()

    def _choose(self, view: PlayerView, request: ActionRequest) -> str | None:
        self._print_options(view, request)
        legal = {
            str(index): option.value
            for index, option in enumerate(request.options, start=1)
        }
        abstain_label = self._abstain_label(view, request.kind)
        while True:
            raw = input("> ").strip()
            if raw == "/history":
                self.terminal.full_history(view)
                self._print_options(view, request)
                continue
            if request.allow_abstain and raw in {"", "0"}:
                if self._confirm_choice(view, request, abstain_label):
                    return None
                continue
            if raw in legal:
                option = request.options[int(raw) - 1]
                if self._confirm_choice(view, request, option.label):
                    return option.value
                continue
            retry = (
                "Please enter a listed number."
                if view.language == "en"
                else "请输入列表中的编号。"
            )
            print(retry)

    def _print_options(self, view: PlayerView, request: ActionRequest) -> None:
        """Render choices again after a history lookup or rejected confirmation."""
        for index, option in enumerate(request.options, start=1):
            print(f"  {index}. {option.label}")
        abstain_label = self._abstain_label(view, request.kind)
        if request.allow_abstain:
            print(f"  0. {abstain_label}")

    def _confirm_choice(
        self,
        view: PlayerView,
        request: ActionRequest,
        label: str,
    ) -> bool:
        """Confirm irreversible or publicly consequential human choices."""
        critical = {
            ActionKind.VOTE,
            ActionKind.WOLF_KILL,
            ActionKind.SEER_INSPECT,
            ActionKind.WITCH_SAVE,
            ActionKind.WITCH_POISON,
            ActionKind.HUNTER_SHOOT,
            ActionKind.BODYGUARD_PROTECT,
            ActionKind.CUPID_LINK,
        }
        if not self.confirm_critical_actions or request.kind not in critical:
            return True
        prompt = (
            f"Confirm [{label}]? Press Enter to confirm, or r to choose again: "
            if view.language == "en"
            else f"确认选择【{label}】？回车确认，输入 r 重选："
        )
        return input(prompt).strip().lower() != "r"

    @staticmethod
    def _abstain_label(view: PlayerView, kind: ActionKind) -> str:
        """Use action-specific wording instead of conflating votes and skills."""
        if view.language == "en":
            return {
                ActionKind.VOTE: "Abstain",
                ActionKind.WOLF_KILL: "No attack",
                ActionKind.WITCH_SAVE: "Do not use antidote",
                ActionKind.WITCH_POISON: "Do not use poison",
                ActionKind.HUNTER_SHOOT: "Do not shoot",
            }.get(kind, "Do not use")
        return {
            ActionKind.VOTE: "弃权",
            ActionKind.WOLF_KILL: "不袭击",
            ActionKind.WITCH_SAVE: "不使用解药",
            ActionKind.WITCH_POISON: "不使用毒药",
            ActionKind.HUNTER_SHOOT: "不开枪",
        }.get(kind, "不使用")

    def _handoff(self, view: PlayerView) -> None:
        if self.require_handoff and self.terminal.clear_screen and sys.stdout.isatty():
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
    observed_input_tokens: int = field(default=0, init=False)
    observed_cached_tokens: int = field(default=0, init=False)
    observed_output_tokens: int = field(default=0, init=False)
    observed_usage_responses: int = field(default=0, init=False)

    def complete(self, messages: list[dict[str, str]]) -> str:
        """Return assistant content from an OpenAI-compatible response."""
        payload = self._payload(messages)
        if self.transport:
            response = self.transport(payload)
        elif self.config.stream:
            return self._post_stream(payload)
        else:
            response = self._post(payload)
        self._record_usage(response)
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
            if self.config.prompt_cache:
                payload["prompt_cache_key"] = self._prompt_cache_key(messages)
                if self.config.prompt_cache_retention:
                    payload["prompt_cache_retention"] = (
                        self.config.prompt_cache_retention
                    )
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
    def _prompt_cache_key(messages: list[dict[str, str]]) -> str:
        """Hash the stable system prefix into a short, non-secret cache key.

        The key deliberately excludes changing history and action fields. Each
        player system prompt contains their name, role, persona, and private
        skills, so distinct private contexts cannot accidentally share a key.
        """
        stable_prefix = messages[:1]
        serialized = json.dumps(
            stable_prefix,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(serialized.encode()).hexdigest()[:32]
        return f"werewolf-v1-{digest}"

    @property
    def observed_cache_hit_rate(self) -> float | None:
        """Return the provider-reported cached share of observed input tokens."""
        if self.observed_input_tokens <= 0:
            return None
        return self.observed_cached_tokens / self.observed_input_tokens

    def _record_usage(self, response: dict[str, Any]) -> bool:
        """Accumulate Responses and Chat token usage without logging prompts."""
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return False
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
        input_details = usage.get(
            "input_tokens_details",
            usage.get("prompt_tokens_details", {}),
        )
        cached_tokens = (
            input_details.get("cached_tokens", 0)
            if isinstance(input_details, dict)
            else 0
        )
        if not isinstance(input_tokens, int):
            return False
        self.observed_input_tokens += input_tokens
        self.observed_cached_tokens += (
            cached_tokens if isinstance(cached_tokens, int) else 0
        )
        self.observed_output_tokens += (
            output_tokens if isinstance(output_tokens, int) else 0
        )
        self.observed_usage_responses += 1
        return True

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

    def _request(self, payload: dict[str, Any]) -> urllib.request.Request:
        """Build one authenticated request without exposing its credentials."""
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
        return urllib.request.Request(  # noqa: S310 - URL is an explicit user configuration.
            endpoint,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )

    def _opener(self) -> urllib.request.OpenerDirector:
        """Return the configured network opener, optionally forcing IPv4."""
        return (
            urllib.request.build_opener(_IPv4HTTPHandler(), _IPv4HTTPSHandler())
            if self.config.force_ipv4
            else urllib.request.build_opener()
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._request(payload)
        try:
            with self._opener().open(
                request,
                timeout=self.config.timeout,
            ) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            msg = f"LLM API returned HTTP {exc.code}: {detail}"
            raise RuntimeError(msg) from exc
        except urllib.error.URLError as exc:
            msg = f"Could not reach LLM API: {exc.reason}"
            raise RuntimeError(msg) from exc

    def _post_stream(self, payload: dict[str, Any]) -> str:
        """Consume an SSE response incrementally and return assembled model text."""
        stream_payload = {**payload, "stream": True}
        request = self._request(stream_payload)
        try:
            with self._opener().open(
                request,
                timeout=self.config.timeout,
            ) as response:
                return self._stream_content(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            msg = f"LLM API returned HTTP {exc.code}: {detail}"
            raise RuntimeError(msg) from exc
        except urllib.error.URLError as exc:
            msg = f"Could not reach LLM API: {exc.reason}"
            raise RuntimeError(msg) from exc

    def _stream_content(self, lines: Iterable[bytes]) -> str:
        """Extract assistant text deltas from Responses or Chat SSE events."""
        parts: list[str] = []
        completed_response: dict[str, Any] | None = None
        stream_usage: dict[str, Any] | None = None
        for raw_line in lines:
            line = raw_line.decode(errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data_text = line.removeprefix("data:").strip()
            if not data_text or data_text == "[DONE]":
                continue
            try:
                event = json.loads(data_text)
            except json.JSONDecodeError as exc:
                msg = f"Malformed streaming event: {data_text[:500]}"
                raise RuntimeError(msg) from exc
            event_type = event.get("type")
            if isinstance(event.get("usage"), dict):
                stream_usage = event["usage"]
            if event_type in {"error", "response.failed"}:
                error = event.get("error") or event.get("response", {}).get("error")
                msg = f"LLM streaming API failed: {error or event!r}"
                raise RuntimeError(msg)
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    parts.append(delta)
                continue
            if event_type == "response.output_text.done" and not parts:
                text = event.get("text")
                if isinstance(text, str):
                    parts.append(text)
                continue
            if event_type == "response.completed":
                response = event.get("response")
                if isinstance(response, dict):
                    completed_response = response
                continue
            for choice in event.get("choices", []):
                if not isinstance(choice, dict):
                    continue
                content = choice.get("delta", {}).get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    parts.extend(
                        str(item.get("text", ""))
                        for item in content
                        if isinstance(item, dict)
                    )
        usage_recorded = (
            self._record_usage(completed_response)
            if completed_response is not None
            else False
        )
        if not usage_recorded and stream_usage is not None:
            self._record_usage({"usage": stream_usage})
        if parts:
            return "".join(parts)
        if completed_response is not None:
            return self._responses_content(completed_response)
        msg = "Streaming API completed without assistant text"
        raise RuntimeError(msg)


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
            "不得假设或索取其他玩家的私密上下文。法官是确定性程序，必须服从合法选项；"
            "身份推演必须满足当前请求中的公开机械约束，尤其不能构造本应已经触发终局的存活狼坑。\n"
            f"{language_rule}\n你的名字：{view.name}\n你的座位号：{view.seat_number or '未提供'}\n"
            f"你的身份：{view.role_name}\n"
            f"身份说明：{view.role_description}\n人物设定：{self.persona or '自然参与游戏'}\n"
            f"恋人信息：{view.lover[1] if view.lover else '无'}\n"
            f"个人技能：\n{skills}\n"
            "仅返回一个 JSON 对象：choice 是选项 value 或 null；text 是要公开或向指定私密频道发送的内容；"
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
        history = self._trim_history(history)
        options = [
            {"value": item.value, "label": item.label} for item in request.options
        ]
        seat_map = [
            {
                "id": player_id,
                "seat": seat,
                "name": name,
                "alive": any(
                    alive_id == player_id for alive_id, _ in view.alive_players
                ),
            }
            for player_id, seat, name in view.seat_players
        ]
        history_message = f"你的可见历史：\n{history or '（暂无）'}"
        current_request = (
            f"当前：第 {view.day} 天，阶段 {view.phase}\n"
            f"座位与存活状态：{json.dumps(seat_map, ensure_ascii=False)}\n"
            f"公开机械约束：{view.mechanical_context or '暂无额外约束'}\n"
            f"法官请求：{request.prompt}\n动作类型：{request.kind.value}\n"
            f"合法选项：{json.dumps(options, ensure_ascii=False)}\n"
            f"允许弃权：{request.allow_abstain}\n"
            "对于发言类动作填写 text；对于选择类动作只把合法 value 填入 choice。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": history_message},
            {"role": "user", "content": current_request},
        ]

    def _trim_history(self, history: str) -> str:
        """Trim old history in stable chunks so its prefix does not slide each call.

        A character-by-character rolling tail changes the first history token on
        every request after the limit is reached, defeating prefix caching. The
        chunked cutoff remains fixed for many calls and advances only when the
        accumulated overflow crosses another chunk boundary.
        """
        marker = "[较早内容因上下文长度省略]\n"
        target_length = self.context_char_limit - len(marker)
        if len(history) <= self.context_char_limit:
            return history
        chunk_size = max(512, min(4096, self.context_char_limit // 8))
        overflow = len(history) - target_length
        cutoff_target = ((overflow + chunk_size - 1) // chunk_size) * chunk_size
        line_break = history.find("\n", cutoff_target)
        cutoff = line_break + 1 if line_break >= 0 else cutoff_target
        return marker + history[cutoff:]

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
    """Offline baseline controller used for demos and deterministic tests."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()  # noqa: S311 - game simulation, not security.

    def act(self, view: PlayerView, request: ActionRequest) -> AgentResponse:
        """Choose only from the supplied legal options without hidden state."""
        thought = self._thought(view, request)
        if request.kind in {ActionKind.TEAM_CHAT, ActionKind.LOVER_CHAT}:
            message = (
                self._team_message(view)
                if request.kind is ActionKind.TEAM_CHAT
                else self._lover_message(view)
            )
            return AgentResponse(text=message, thought=thought)
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
            self._visible_label(view, player_id, name)
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
            self._visible_label(view, player_id, name)
            for player_id, name in view.alive_players
            if player_id != view.player_id
        ]
        target = self.rng.choice(targets) if targets else "目标"
        if view.language == "en":
            return f"I suggest attacking {target}; keep our daytime positions separate."
        return f"建议考虑袭击{target}，白天尽量不要让我们的站边完全一致。"

    @staticmethod
    def _lover_message(view: PlayerView) -> str:
        partner = (
            BotController._visible_label(view, view.lover[0], view.lover[1])
            if view.lover
            else "partner"
        )
        if view.language == "en":
            return (
                f"{partner}, we should keep both of us alive without exposing our link."
            )
        return f"{partner}，我们需要同时存活，并避免公开暴露恋人关系。"

    @staticmethod
    def _visible_label(view: PlayerView, player_id: str, name: str) -> str:
        """Use the same stable seat label as the judge when a seat map is available."""
        seat = next(
            (
                seat_number
                for mapped_id, seat_number, _ in view.seat_players
                if mapped_id == player_id
            ),
            0,
        )
        if not seat:
            return name
        return f"Seat {seat} {name}" if view.language == "en" else f"{seat}号 {name}"

    @staticmethod
    def _thought(view: PlayerView, request: ActionRequest) -> str:
        if view.language == "en":
            return f"Re-evaluate visible evidence before action {request.kind.value}."
        return f"在执行 {request.kind.value} 前重新检查自己的可见信息。"


class SafeFallbackController:
    """Deterministic, conservative fallback for explicitly non-strict games.

    Public votes and optional irreversible abilities abstain. Mandatory private
    abilities use the first legal option so a casual game can continue without
    introducing additional randomness. Every such response is marked by the
    judge before it is applied.
    """

    def act(self, view: PlayerView, request: ActionRequest) -> AgentResponse:
        """Return the least destructive legal response for the requested action."""
        if request.kind in {ActionKind.TEAM_CHAT, ActionKind.LOVER_CHAT}:
            return AgentResponse(text="")
        if request.kind in {ActionKind.SPEAK, ActionKind.LAST_WORDS}:
            text = (
                "(controller unavailable; remains silent)"
                if view.language == "en"
                else "（控制器不可用，本轮保持沉默）"
            )
            return AgentResponse(text=text)
        if request.allow_abstain:
            return AgentResponse(choice=None)
        choice = request.options[0].value if request.options else None
        return AgentResponse(choice=choice)
