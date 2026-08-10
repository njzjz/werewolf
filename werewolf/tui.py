"""Dependency-free terminal interface for creating and editing game configs."""

from __future__ import annotations

import importlib
import math
import os
import select as select_module
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, TextIO, cast

from .config import (
    MAX_PLAYERS,
    MIN_PLAYERS,
    RECOMMENDED_CHECKPOINT_PATH,
    RECOMMENDED_PUBLIC_TRANSCRIPT_PATH,
    ROLE_PRESET_SIZES,
    GameConfig,
    LLMProviderConfig,
    PlayerConfig,
    RuleConfig,
    load_config,
    validate_config,
    write_config,
)
from .engine import role_deck
from .models import ROLE_NAMES, Role
from .rendering import contains_terminal_control, sanitize_rendered_text
from .skills import BUILTIN_SKILLS

if os.name != "nt":  # pragma: no cover - platform imports need a real TTY.
    import termios
    import tty

_CYAN = "\x1b[38;5;81m"
_GREEN = "\x1b[38;5;114m"
_YELLOW = "\x1b[38;5;221m"
_RED = "\x1b[38;5;203m"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"

_PRESET_LABELS = {
    "classic": "经典狼人杀",
    "movie_basic": "电影 · BASIC",
    "movie_crazy_fox": "电影 · CRAZY FOX",
    "movie_prison_break": "电影 · PRISON BREAK",
    "movie_lovers": "电影 · LOVERS",
    "movie_mad_land": "电影 · MAD LAND",
}

_PRESET_DESCRIPTIONS = {
    "classic": "6–16 人，按人数自动生成平衡牌组",
    "movie_basic": "10 人：基础电影牌组",
    "movie_crazy_fox": "12 人：加入灵媒、守卫与妖狐",
    "movie_prison_break": "12 人：加入共有者与狂人",
    "movie_lovers": "11 人：丘比特与恋人生存结算",
    "movie_mad_land": "10 人：一狼、七狂人与村人少数阵营",
}

_CONTROLLER_LABELS = {
    "human": "真人",
    "llm": "LLM",
    "bot": "本地机器人",
}


@dataclass(frozen=True)
class Choice:
    """One selectable TUI row with a stable machine value."""

    value: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class TUIResult:
    """Outcome returned to the CLI after leaving the alternate screen."""

    config_path: Path
    start_game: bool
    saved: bool


class TerminalUI:
    """Small terminal toolkit with arrow-key and numbered-input fallbacks."""

    def __init__(
        self,
        *,
        stdin: TextIO,
        stdout: TextIO,
        color: bool | None = None,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.interactive = bool(
            getattr(stdin, "isatty", lambda: False)()
            and getattr(stdout, "isatty", lambda: False)()
        )
        color_allowed = "NO_COLOR" not in os.environ
        self.color = self.interactive and color_allowed if color is None else color

    def __enter__(self) -> TerminalUI:  # noqa: PYI034 - Python 3.9 lacks Self.
        """Enter the alternate screen when a real terminal is available."""
        if self.interactive:
            self._write("\x1b[?1049h\x1b[H")
        return self

    def __exit__(self, *_exc: object) -> None:
        """Restore the user's previous screen even after Ctrl-C."""
        if self.interactive:
            self._write("\x1b[?1049l")

    def _write(self, text: str) -> None:
        """Write and flush one terminal update."""
        self.stdout.write(text)
        self.stdout.flush()

    def styled(self, text: str, style: str) -> str:
        """Apply ANSI styling only when color output is enabled."""
        return f"{style}{text}{_RESET}" if self.color else text

    def screen(
        self,
        title: str,
        *,
        subtitle: str = "",
        body: list[str] | tuple[str, ...] = (),
        footer: str = "",
    ) -> None:
        """Render one consistent full-screen panel."""
        width = min(max(shutil.get_terminal_size((88, 24)).columns, 48), 96)
        rule = "─" * width
        lines = [
            self.styled(" WEREWOLF  /  CONFIGURE ", _BOLD + _CYAN),
            self.styled(rule, _DIM),
            self.styled(title, _BOLD),
        ]
        if subtitle:
            lines.append(self.styled(subtitle, _DIM))
        lines.append("")
        lines.extend(body)
        if footer:
            lines.extend(("", self.styled(rule, _DIM), self.styled(footer, _DIM)))
        prefix = "\x1b[2J\x1b[H" if self.interactive else ""
        self._write(prefix + "\n".join(lines) + "\n")

    def select(
        self,
        title: str,
        choices: list[Choice],
        *,
        subtitle: str = "",
        default: str | None = None,
        allow_back: bool = True,
        body_prefix: list[str] | None = None,
    ) -> str | None:
        """Choose a row with arrows/Enter or a portable numbered prompt."""
        if not choices:
            msg = "select() requires at least one choice"
            raise ValueError(msg)
        selected = next(
            (index for index, choice in enumerate(choices) if choice.value == default),
            0,
        )
        if not self.interactive:
            body = list(body_prefix or [])
            for index, choice in enumerate(choices, start=1):
                marker = " (默认)" if index - 1 == selected else ""
                body.append(f"  {index}. {choice.label}{marker}")
                if choice.description:
                    body.append(f"     {choice.description}")
            self.screen(
                title,
                subtitle=subtitle,
                body=body,
                footer="输入序号并回车" + ("；q 返回" if allow_back else ""),
            )
            while True:
                raw = self._readline("选择 › ").strip().lower()
                if not raw:
                    return choices[selected].value
                if allow_back and raw in {"q", "quit", "back"}:
                    return None
                if raw.isdigit() and 1 <= int(raw) <= len(choices):
                    return choices[int(raw) - 1].value
                self._write("请输入列表中的有效序号。\n")

        while True:
            body = list(body_prefix or [])
            for index, choice in enumerate(choices):
                active = index == selected
                marker = self.styled("›", _BOLD + _CYAN) if active else " "
                label = self.styled(choice.label, _BOLD) if active else choice.label
                body.append(f"  {marker} {label}")
                if choice.description:
                    style = _CYAN if active else _DIM
                    body.append(f"      {self.styled(choice.description, style)}")
            footer = "↑/↓ 移动   Enter 选择"
            if allow_back:
                footer += "   Esc/q 返回"
            self.screen(title, subtitle=subtitle, body=body, footer=footer)
            key = self._read_key()
            if key in {"up", "k"}:
                selected = (selected - 1) % len(choices)
            elif key in {"down", "j"}:
                selected = (selected + 1) % len(choices)
            elif key == "home":
                selected = 0
            elif key == "end":
                selected = len(choices) - 1
            elif key in {"enter", "right", "l"}:
                return choices[selected].value
            elif allow_back and key in {"escape", "left", "h", "q"}:
                return None
            elif key.isdigit() and key != "0" and int(key) <= len(choices):
                return choices[int(key) - 1].value

    def text_input(
        self,
        title: str,
        label: str,
        *,
        default: str = "",
        allow_empty: bool = False,
        validator: Callable[[str], str | None] | None = None,
        subtitle: str = "",
    ) -> str:
        """Read and validate one line of text without echoing secrets."""
        while True:
            display_default = sanitize_rendered_text(default, limit=200)
            body = [label]
            if display_default:
                body.append(self.styled(f"当前值：{display_default}", _DIM))
            self.screen(
                title,
                subtitle=subtitle,
                body=body,
                footer="直接回车保留当前值",
            )
            value = self._readline("输入 › ").strip()
            if not value:
                value = default
            if not value and not allow_empty:
                self._write("该字段不能为空。\n")
                continue
            if contains_terminal_control(value):
                self._write("输入不能包含终端控制字符、制表符或换行。\n")
                continue
            error = validator(value) if validator else None
            if error:
                self._write(f"{sanitize_rendered_text(error, limit=300)}\n")
                continue
            return value

    def number_input(
        self,
        title: str,
        label: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        """Read a bounded integer value."""

        def validate(raw: str) -> str | None:
            if not raw.lstrip("-").isdigit():
                return "请输入整数。"
            value = int(raw)
            if not minimum <= value <= maximum:
                return f"请输入 {minimum}–{maximum} 之间的值。"
            return None

        return int(
            self.text_input(
                title,
                label,
                default=str(default),
                validator=validate,
            ),
        )

    def float_input(
        self,
        title: str,
        label: str,
        *,
        default: float,
        minimum: float,
    ) -> float:
        """Read a finite floating-point value with a lower bound."""

        def validate(raw: str) -> str | None:
            try:
                value = float(raw)
            except ValueError:
                return "请输入数字。"
            if value < minimum or not math.isfinite(value):
                return f"请输入不小于 {minimum:g} 的有限数字。"
            return None

        return float(
            self.text_input(
                title,
                label,
                default=f"{default:g}",
                validator=validate,
            ),
        )

    def confirm(
        self,
        title: str,
        question: str,
        *,
        default: bool = True,
    ) -> bool:
        """Ask a yes/no question using the standard selection treatment."""
        result = self.select(
            title,
            [Choice("yes", "是"), Choice("no", "否")],
            subtitle=question,
            default="yes" if default else "no",
            allow_back=False,
        )
        return result == "yes"

    def notice(self, title: str, message: str) -> None:
        """Show an informational panel and wait for acknowledgement."""
        self.screen(
            title,
            body=[sanitize_rendered_text(message, limit=1200)],
            footer="按 Enter 继续",
        )
        if self.interactive:
            while self._read_key() not in {"enter", "escape", "q"}:
                pass
        else:
            self._readline("")

    def _readline(self, prompt: str) -> str:
        """Read one line while retaining readline support on the real stdio."""
        if self.stdin is sys.stdin and self.stdout is sys.stdout:
            return input(prompt)
        self._write(prompt)
        value = self.stdin.readline()
        if value == "":
            raise EOFError
        return value.rstrip("\r\n")

    def _read_key(self) -> str:
        """Read one portable navigation key in raw mode."""
        if os.name == "nt":  # pragma: no cover - exercised on Windows runners.
            windows_console: Any = importlib.import_module("msvcrt")
            first = windows_console.getwch()
            if first in {"\x00", "\xe0"}:
                return {
                    "H": "up",
                    "P": "down",
                    "K": "left",
                    "M": "right",
                    "G": "home",
                    "O": "end",
                }.get(windows_console.getwch(), "")
            return self._decode_key(first)

        descriptor = self.stdin.fileno()
        previous = termios.tcgetattr(descriptor)
        try:
            tty.setraw(descriptor)
            first = os.read(descriptor, 1).decode(errors="ignore")
            sequence = first
            if first == "\x1b":
                while select_module.select([descriptor], [], [], 0.02)[0]:
                    sequence += os.read(descriptor, 1).decode(errors="ignore")
            return {
                "\x1b[A": "up",
                "\x1b[B": "down",
                "\x1b[C": "right",
                "\x1b[D": "left",
                "\x1b[H": "home",
                "\x1b[F": "end",
                "\x1bOH": "home",
                "\x1bOF": "end",
            }.get(sequence, self._decode_key(first))
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)

    @staticmethod
    def _decode_key(value: str) -> str:
        """Map a single character to its semantic key name."""
        if value in {"\r", "\n"}:
            return "enter"
        if value == "\x03":
            raise KeyboardInterrupt
        if value == "\x1b":
            return "escape"
        return value.lower()


class ConfigurationTUI:
    """Stateful configuration workbench built from the terminal primitives."""

    def __init__(self, path: Path, ui: TerminalUI) -> None:
        self.path = path
        self.ui = ui
        self.existing = path.exists()
        self.config = load_config(path) if self.existing else self._new_config()
        self.dirty = not self.existing

    @staticmethod
    def _new_config() -> GameConfig:
        """Build the recommended one-human, seven-LLM starting point."""
        provider = LLMProviderConfig(
            base_url="https://api.openai.com/v1",
            model="your-model-id",
            api_key_env="OPENAI_API_KEY",
        )
        players = [PlayerConfig(name="你", controller="human")]
        players.extend(
            PlayerConfig(
                name=f"智能体{index}",
                controller="llm",
                provider="default",
            )
            for index in range(1, 8)
        )
        return GameConfig(
            language="zh-CN",
            players=tuple(players),
            providers={"default": provider},
            public_transcript_path=RECOMMENDED_PUBLIC_TRANSCRIPT_PATH,
            checkpoint_path=RECOMMENDED_CHECKPOINT_PATH,
        )

    def run(self) -> TUIResult | None:
        """Run until the user saves, starts, or explicitly cancels."""
        opening = self._opening_screen()
        if opening == "start":
            return TUIResult(self.path, start_game=True, saved=False)
        if opening != "configure":
            return None
        while True:
            action = self._dashboard()
            if action is None:
                if not self.dirty or self.ui.confirm(
                    "退出配置",
                    "放弃尚未保存的修改？",
                    default=False,
                ):
                    return None
                continue
            if action == "game":
                self._edit_game()
            elif action == "players":
                self._edit_players()
            elif action == "providers":
                self._edit_providers()
            elif action == "rules":
                self._edit_rules()
            elif action == "experience":
                self._edit_experience()
            elif action == "review":
                result = self._review()
                if result is not None:
                    return result

    def _opening_screen(self) -> str | None:
        """Explain whether the workbench is creating or editing a file."""
        safe_path = sanitize_rendered_text(self.path, limit=300)
        if self.existing:
            return self.ui.select(
                "配置已就绪",
                [
                    Choice("configure", "打开配置工作台", "修改后统一检查并保存"),
                    Choice("start", "直接开始游戏", "不修改当前配置"),
                    Choice("cancel", "退出"),
                ],
                subtitle=f"已加载 {safe_path}",
                allow_back=False,
            )
        return self.ui.select(
            "创建一局真正属于你的狼人杀",
            [
                Choice(
                    "configure",
                    "开始配置",
                    "已准备好 1 真人 + 7 LLM 的安全推荐值",
                ),
                Choice("cancel", "暂不配置"),
            ],
            subtitle=f"将保存到 {safe_path}；密钥只引用环境变量，不写入 JSON",
            allow_back=False,
        )

    def _dashboard(self) -> str | None:
        """Render the high-level configuration sections and health state."""
        counts = Counter(player.controller for player in self.config.players)
        provider_summary = self._provider_summary()
        errors, warnings = self._issues()
        health = "可以保存"
        if errors:
            health = f"还需处理 {len(errors)} 个问题"
        elif warnings:
            health = f"可保存，另有 {len(warnings)} 个提醒"
        prefix = [
            f"  {self.ui.styled('状态', _BOLD)}  {health}",
            f"  {self.ui.styled('文件', _BOLD)}  {sanitize_rendered_text(self.path, limit=180)}",
            "",
        ]
        return self.ui.select(
            "配置工作台",
            [
                Choice(
                    "game",
                    f"游戏与牌组    {_PRESET_LABELS[self.config.role_preset]}",
                    f"{len(self.config.players)} 人 · {self.config.language} · "
                    f"seed={self.config.seed if self.config.seed is not None else '随机'}",
                ),
                Choice(
                    "players",
                    "玩家与席位",
                    f"真人 {counts['human']} · LLM {counts['llm']} · "
                    f"本地机器人 {counts['bot']}",
                ),
                Choice("providers", "模型与 Provider", provider_summary),
                Choice("rules", "房规", self._rules_summary()),
                Choice("experience", "终端体验与安全", self._experience_summary()),
                Choice(
                    "review",
                    "检查、保存并开始",
                    "运行完整校验，确认输出文件和模型设置",
                ),
            ],
            subtitle="各部分可反复修改；Esc 随时返回",
            body_prefix=prefix,
        )

    def _edit_game(self) -> None:
        """Edit language, deck, player count, and deterministic seed."""
        language = self.ui.select(
            "游戏与牌组  ·  语言",
            [
                Choice("zh-CN", "简体中文", "法官文本和 LLM 语言要求均为中文"),
                Choice(
                    "en", "English", "Judge copy and LLM language policy in English"
                ),
            ],
            default=self.config.language,
        )
        if language is None:
            return
        deck_choices = [
            Choice(
                "custom",
                "自定义牌组",
                "从经典牌组开始，逐项调整身份计数",
            ),
            *[
                Choice(name, _PRESET_LABELS[name], _PRESET_DESCRIPTIONS[name])
                for name in ROLE_PRESET_SIZES
            ],
        ]
        deck_default = (
            "custom" if self.config.roles is not None else self.config.role_preset
        )
        deck = self.ui.select(
            "游戏与牌组  ·  身份牌",
            deck_choices,
            default=deck_default,
        )
        if deck is None:
            return
        old_count = len(self.config.players)
        old_preset = self.config.role_preset
        if deck == "custom":
            count = self.ui.select(
                "游戏与牌组  ·  自定义牌组人数",
                [
                    Choice(str(value), f"{value} 人", "随后逐项调整身份计数")
                    for value in range(MIN_PLAYERS, MAX_PLAYERS + 1)
                ],
                default=str(old_count),
            )
            if count is None:
                return
            player_count = int(count)
            role_preset = self.config.role_preset
        else:
            role_preset = deck
            fixed_size = ROLE_PRESET_SIZES[role_preset]
            if fixed_size is None:
                count = self.ui.select(
                    "游戏与牌组  ·  人数",
                    [
                        Choice(str(value), f"{value} 人", "经典牌组自动平衡")
                        for value in range(MIN_PLAYERS, MAX_PLAYERS + 1)
                    ],
                    default=str(old_count),
                )
                if count is None:
                    return
                player_count = int(count)
            else:
                player_count = fixed_size
        seed_raw = self.ui.text_input(
            "游戏与牌组  ·  随机种子",
            "留空表示每局随机；填写整数可复现座位、身份与本地 bot 行为。",
            default="" if self.config.seed is None else str(self.config.seed),
            allow_empty=True,
            validator=lambda value: (
                None
                if not value or value.lstrip("-").isdigit()
                else "随机种子必须是整数或留空。"
            ),
        )
        changed_deck = role_preset != old_preset or player_count != old_count
        roles = (
            self.config.roles
            if deck == "custom" and player_count == old_count
            else None
        )
        players = self._resize_players(player_count)
        if changed_deck or (self.config.roles is not None and deck != "custom"):
            players = tuple(replace(player, fixed_role=None) for player in players)
        self.config = replace(
            self.config,
            language=language,
            role_preset=role_preset,
            roles=roles,
            players=players,
            seed=int(seed_raw) if seed_raw else None,
        )
        self.dirty = True
        if deck == "custom":
            self._edit_custom_deck()

    def _edit_custom_deck(self) -> None:
        """Edit a complete role-count table and enforce deck invariants."""
        count = len(self.config.players)
        roles = self.config.roles or role_deck(count)
        role_counts = Counter(roles)
        while True:
            total = sum(role_counts.values())
            choices = [
                Choice(
                    role.value,
                    f"{ROLE_NAMES['zh-CN'][role]:<8} {role_counts[role]} 张",
                    "选择后修改数量",
                )
                for role in Role
            ]
            choices.extend(
                [
                    Choice("reset", "恢复经典牌组", f"按 {count} 人经典配置重置"),
                    Choice("done", "完成", f"当前 {total}/{count} 张"),
                ],
            )
            selected = self.ui.select(
                "自定义牌组",
                choices,
                subtitle=f"身份总数必须等于玩家数；当前 {total}/{count}",
                default="done",
            )
            if selected is None:
                return
            if selected == "reset":
                role_counts = Counter(role_deck(count))
                continue
            if selected == "done":
                candidate_roles = tuple(
                    role for role in Role for _ in range(role_counts[role])
                )
                candidate = replace(self.config, roles=candidate_roles)
                try:
                    validate_config(candidate)
                except (TypeError, ValueError) as exc:
                    self.ui.notice("牌组还不能使用", str(exc))
                    continue
                self.config = candidate
                self.dirty = True
                return
            role = Role(selected)
            maximum = 2 if role is Role.SHARED else count
            if role in {
                Role.SEER,
                Role.WITCH,
                Role.HUNTER,
                Role.MEDIUM,
                Role.BODYGUARD,
                Role.FOX,
                Role.CUPID,
            }:
                maximum = 1
            role_counts[role] = self.ui.number_input(
                "自定义牌组",
                f"{ROLE_NAMES['zh-CN'][role]}的数量",
                default=role_counts[role],
                minimum=0,
                maximum=maximum,
            )

    def _edit_players(self) -> None:
        """Apply a quick controller layout or edit individual seats."""
        while True:
            counts = Counter(player.controller for player in self.config.players)
            selected = self.ui.select(
                "玩家与席位",
                [
                    Choice("human_llm", "1 真人 + 其余 LLM", "推荐的个人游玩方式"),
                    Choice("all_llm", "全 LLM", "适合观战、评测和复盘"),
                    Choice("human_bot", "1 真人 + 本地机器人", "完全离线，不访问 API"),
                    Choice("all_bot", "全本地机器人", "最快验证规则与终端效果"),
                    Choice(
                        "custom",
                        "自定义数量",
                        f"当前：真人 {counts['human']} / LLM {counts['llm']} / "
                        f"bot {counts['bot']}",
                    ),
                    Choice(
                        "seats", "逐席编辑", "名称、控制器、persona、技能和固定身份"
                    ),
                    Choice("done", "完成"),
                ],
                default="seats",
            )
            if selected in {None, "done"}:
                return
            player_count = len(self.config.players)
            if selected == "human_llm":
                controllers = ["human", *("llm" for _ in range(player_count - 1))]
                self._apply_layout(controllers)
            elif selected == "all_llm":
                self._apply_layout(["llm"] * player_count)
            elif selected == "human_bot":
                controllers = ["human", *("bot" for _ in range(player_count - 1))]
                self._apply_layout(controllers)
            elif selected == "all_bot":
                self._apply_layout(["bot"] * player_count)
            elif selected == "custom":
                humans = self.ui.number_input(
                    "自定义玩家数量",
                    "真人数量",
                    default=counts["human"],
                    minimum=0,
                    maximum=player_count,
                )
                llms = self.ui.number_input(
                    "自定义玩家数量",
                    "LLM 数量",
                    default=min(counts["llm"], player_count - humans),
                    minimum=0,
                    maximum=player_count - humans,
                )
                bots = player_count - humans - llms
                self._apply_layout(
                    ["human"] * humans + ["llm"] * llms + ["bot"] * bots,
                )
            elif selected == "seats":
                self._edit_seats()

    def _edit_seats(self) -> None:
        """Select and edit one seat at a time."""
        while True:
            choices = [
                Choice(
                    str(index),
                    f"{index + 1:>2}号  {sanitize_rendered_text(player.name, limit=60)}",
                    self._player_description(player),
                )
                for index, player in enumerate(self.config.players)
            ]
            choices.append(Choice("done", "完成"))
            selected = self.ui.select(
                "逐席编辑",
                choices,
                subtitle="座位号在开局洗牌后保持稳定，名称必须唯一",
                default="done",
            )
            if selected in {None, "done"}:
                return
            self._edit_seat(int(cast("str", selected)))

    def _edit_seat(self, index: int) -> None:
        """Edit all user-facing and controller-specific fields for one player."""
        while True:
            player = self.config.players[index]
            fixed_role = (
                ROLE_NAMES["zh-CN"][player.fixed_role]
                if player.fixed_role is not None
                else "随机"
            )
            provider = player.provider or "—"
            selected = self.ui.select(
                f"{index + 1}号席位  ·  {sanitize_rendered_text(player.name, limit=80)}",
                [
                    Choice("name", f"名称              {player.name}"),
                    Choice(
                        "controller",
                        f"控制器            {_CONTROLLER_LABELS[player.controller]}",
                    ),
                    Choice("provider", f"Provider          {provider}"),
                    Choice(
                        "persona",
                        "Persona           "
                        + (
                            sanitize_rendered_text(player.persona, limit=50) or "未设置"
                        ),
                    ),
                    Choice(
                        "skills",
                        f"行为技能          {', '.join(player.skills) or '无'}",
                    ),
                    Choice("role", f"固定身份          {fixed_role}"),
                    Choice("done", "完成"),
                ],
                default="done",
            )
            if selected in {None, "done"}:
                return
            updated = player
            if selected == "name":
                name = self.ui.text_input(
                    "编辑席位名称",
                    "名称会出现在公共频道中。",
                    default=player.name,
                )
                updated = replace(player, name=name)
            elif selected == "controller":
                controller = self.ui.select(
                    "编辑控制器",
                    [
                        Choice("human", "真人", "从当前终端读取发言和选择"),
                        Choice("llm", "LLM", "调用 OpenAI-compatible provider"),
                        Choice("bot", "本地机器人", "离线、确定性的简单控制器"),
                    ],
                    default=player.controller,
                )
                if controller is None:
                    continue
                provider_name = player.provider
                if controller == "llm":
                    provider_name = self._default_provider_name()
                else:
                    provider_name = None
                updated = replace(
                    player,
                    controller=controller,
                    provider=provider_name,
                )
            elif selected == "provider":
                if player.controller != "llm":
                    self.ui.notice(
                        "无需 Provider", "只有 LLM 控制器需要绑定 Provider。"
                    )
                    continue
                if not self.config.providers:
                    self._create_provider(assign_all=False)
                    continue
                provider_name = self.ui.select(
                    "选择 Provider",
                    [
                        Choice(name, name, provider.model)
                        for name, provider in self.config.providers.items()
                    ],
                    default=player.provider,
                )
                if provider_name is None:
                    continue
                updated = replace(player, provider=provider_name)
            elif selected == "persona":
                persona = self.ui.text_input(
                    "编辑 Persona",
                    "描述这个玩家稳定的表达风格；留空表示不额外约束。",
                    default=player.persona,
                    allow_empty=True,
                )
                updated = replace(player, persona=persona)
            elif selected == "skills":
                updated = replace(player, skills=self._edit_skills(player.skills))
            elif selected == "role":
                role_value = self.ui.select(
                    "固定身份",
                    [Choice("random", "随机分配", "推荐用于公平对局")]
                    + [Choice(role.value, ROLE_NAMES["zh-CN"][role]) for role in Role],
                    default=(
                        player.fixed_role.value if player.fixed_role else "random"
                    ),
                )
                if role_value is None:
                    continue
                updated = replace(
                    player,
                    fixed_role=None if role_value == "random" else Role(role_value),
                )
            players = list(self.config.players)
            players[index] = updated
            self.config = replace(self.config, players=tuple(players))
            self.dirty = True

    def _edit_skills(self, current: tuple[str, ...]) -> tuple[str, ...]:
        """Toggle built-in behavioral skills while retaining a stable order."""
        selected = set(current)
        while True:
            choices = [
                Choice(
                    name,
                    f"{'✓' if name in selected else '○'}  {name}",
                    skill.description,
                )
                for name, skill in BUILTIN_SKILLS.items()
            ]
            choices.append(Choice("done", "完成", f"已选择 {len(selected)} 项"))
            value = self.ui.select("行为技能", choices, default="done")
            if value in {None, "done"}:
                return tuple(name for name in BUILTIN_SKILLS if name in selected)
            value = cast("str", value)
            if value in selected:
                selected.remove(value)
            else:
                selected.add(value)

    def _edit_providers(self) -> None:
        """Create and edit OpenAI-compatible provider definitions."""
        while True:
            choices = [
                Choice(
                    f"provider:{name}",
                    sanitize_rendered_text(name, limit=80),
                    sanitize_rendered_text(
                        f"{provider.model} · {provider.wire_api} · {provider.base_url}",
                        limit=180,
                    ),
                )
                for name, provider in self.config.providers.items()
            ]
            choices.extend(
                [
                    Choice("new", "新建 Provider", "可连接 OpenAI、兼容代理或本地服务"),
                    Choice("done", "完成"),
                ],
            )
            selected = self.ui.select(
                "模型与 Provider",
                choices,
                subtitle="API 密钥推荐通过环境变量读取，TUI 不会要求输入密钥值",
                default="done",
            )
            if selected in {None, "done"}:
                return
            if selected == "new":
                self._create_provider(assign_all=True)
            else:
                provider_value = cast("str", selected)
                self._edit_provider(provider_value[len("provider:") :])

    def _create_provider(self, *, assign_all: bool) -> None:
        """Add one provider and optionally route every LLM seat to it."""

        def validate_name(value: str) -> str | None:
            if value in self.config.providers:
                return "这个 Provider 名称已经存在。"
            return None

        name = self.ui.text_input(
            "新建 Provider",
            "内部名称，例如 default、openai 或 local。",
            default="default" if not self.config.providers else "provider-2",
            validator=validate_name,
        )
        base_url = self.ui.text_input(
            "新建 Provider  ·  API 地址",
            "OpenAI-compatible API 根地址。",
            default="https://api.openai.com/v1",
        )
        model = self.ui.text_input(
            "新建 Provider  ·  模型",
            "填写服务实际接受的模型 ID。",
            default="your-model-id",
        )
        api_key_env = self.ui.text_input(
            "新建 Provider  ·  密钥环境变量",
            "只保存变量名；留空适用于无需鉴权的本地服务。",
            default="OPENAI_API_KEY",
            allow_empty=True,
        )
        providers = dict(self.config.providers)
        providers[name] = LLMProviderConfig(
            base_url=base_url,
            model=model,
            api_key_env=api_key_env or None,
        )
        config = replace(self.config, providers=providers)
        llm_players = [
            player for player in config.players if player.controller == "llm"
        ]
        if llm_players and (
            not assign_all
            or self.ui.confirm(
                "绑定 Provider",
                "将所有 LLM 席位绑定到这个 Provider？",
                default=True,
            )
        ):
            config = replace(
                config,
                players=tuple(
                    replace(player, provider=name)
                    if player.controller == "llm"
                    else player
                    for player in config.players
                ),
            )
        self.config = config
        self.dirty = True

    def _edit_provider(self, name: str) -> None:
        """Edit common and advanced settings for one provider."""
        while True:
            provider = self.config.providers[name]
            safe_base_url = sanitize_rendered_text(provider.base_url, limit=120)
            safe_model = sanitize_rendered_text(provider.model, limit=100)
            safe_api_key_env = sanitize_rendered_text(
                provider.api_key_env or "未设置",
                limit=80,
            )
            selected = self.ui.select(
                f"Provider  ·  {sanitize_rendered_text(name, limit=80)}",
                [
                    Choice("base_url", f"API 地址          {safe_base_url}"),
                    Choice("model", f"模型 ID           {safe_model}"),
                    Choice(
                        "api_key_env",
                        f"密钥环境变量      {safe_api_key_env}",
                    ),
                    Choice("wire_api", f"接口协议          {provider.wire_api}"),
                    Choice(
                        "reasoning",
                        f"推理强度          {provider.reasoning_effort or '由服务决定'}",
                    ),
                    Choice(
                        "temperature", f"Temperature       {provider.temperature:g}"
                    ),
                    Choice("timeout", f"超时              {provider.timeout:g}s"),
                    Choice("max_tokens", f"单次输出上限      {provider.max_tokens}"),
                    Choice(
                        "stream", f"流式传输          {self._on_off(provider.stream)}"
                    ),
                    Choice(
                        "json_mode",
                        f"JSON mode         {self._on_off(provider.use_json_mode)}",
                    ),
                    Choice(
                        "ipv4", f"强制 IPv4         {self._on_off(provider.force_ipv4)}"
                    ),
                    Choice(
                        "cache",
                        f"Prompt cache      {self._on_off(provider.prompt_cache)}",
                    ),
                    Choice(
                        "assign", "绑定全部 LLM 席位", "将当前 Provider 设为全桌默认"
                    ),
                    *(
                        [Choice("remove_key", "移除 JSON 中的明文 API key", "推荐操作")]
                        if provider.api_key is not None
                        else []
                    ),
                    Choice("done", "完成"),
                ],
                default="done",
            )
            if selected in {None, "done"}:
                return
            updated = provider
            if selected == "base_url":
                updated = replace(
                    provider,
                    base_url=self.ui.text_input(
                        "Provider API 地址",
                        "客户端会自动追加 /responses 或 /chat/completions。",
                        default=provider.base_url,
                    ),
                )
            elif selected == "model":
                updated = replace(
                    provider,
                    model=self.ui.text_input(
                        "Provider 模型 ID",
                        "必须与服务端实际模型名称完全一致。",
                        default=provider.model,
                    ),
                )
            elif selected == "api_key_env":
                value = self.ui.text_input(
                    "Provider 密钥环境变量",
                    "这里只填写变量名，不填写真实密钥；留空表示不鉴权。",
                    default=provider.api_key_env or "",
                    allow_empty=True,
                )
                updated = replace(provider, api_key_env=value or None)
            elif selected == "wire_api":
                wire_api = self.ui.select(
                    "Provider 接口协议",
                    [
                        Choice("chat", "Chat Completions", "兼容范围最广"),
                        Choice(
                            "responses", "Responses", "支持 reasoning 与 prompt caching"
                        ),
                    ],
                    default=provider.wire_api,
                )
                if wire_api is None:
                    continue
                updated = replace(provider, wire_api=wire_api)
                if wire_api != "responses":
                    updated = replace(
                        updated,
                        prompt_cache=False,
                        prompt_cache_retention=None,
                    )
            elif selected == "reasoning":
                values = ["none", "low", "medium", "high", "xhigh"]
                current = provider.reasoning_effort or "none"
                if current not in values:
                    values.insert(-1, current)
                reasoning = self.ui.select(
                    "Provider 推理强度",
                    [
                        Choice(value, "由服务决定" if value == "none" else value)
                        for value in values
                    ],
                    default=current,
                )
                if reasoning is None:
                    continue
                updated = replace(
                    provider,
                    reasoning_effort=None if reasoning == "none" else reasoning,
                )
            elif selected == "temperature":
                updated = replace(
                    provider,
                    temperature=self.ui.float_input(
                        "Provider Temperature",
                        "随机性参数，不能为负数。",
                        default=provider.temperature,
                        minimum=0,
                    ),
                )
            elif selected == "timeout":
                updated = replace(
                    provider,
                    timeout=self.ui.float_input(
                        "Provider 超时",
                        "单次请求超时秒数。",
                        default=provider.timeout,
                        minimum=0.1,
                    ),
                )
            elif selected == "max_tokens":
                updated = replace(
                    provider,
                    max_tokens=self.ui.number_input(
                        "Provider 输出上限",
                        "单个动作允许的最大输出 token。",
                        default=provider.max_tokens,
                        minimum=1,
                        maximum=1_000_000,
                    ),
                )
            elif selected == "stream":
                updated = replace(provider, stream=not provider.stream)
            elif selected == "json_mode":
                updated = replace(provider, use_json_mode=not provider.use_json_mode)
            elif selected == "ipv4":
                updated = replace(provider, force_ipv4=not provider.force_ipv4)
            elif selected == "cache":
                if provider.wire_api != "responses":
                    self.ui.notice(
                        "Prompt cache 暂不可用",
                        "请先将接口协议切换为 Responses。",
                    )
                    continue
                if provider.prompt_cache:
                    updated = replace(
                        provider,
                        prompt_cache=False,
                        prompt_cache_retention=None,
                    )
                else:
                    retention = self.ui.select(
                        "Prompt cache 保留时间",
                        [
                            Choice("default", "由服务决定"),
                            Choice("in-memory", "in-memory"),
                            Choice("24h", "24h"),
                        ],
                        default="default",
                    )
                    if retention is None:
                        continue
                    updated = replace(
                        provider,
                        prompt_cache=True,
                        prompt_cache_retention=(
                            None if retention == "default" else retention
                        ),
                    )
            elif selected == "assign":
                self.config = replace(
                    self.config,
                    players=tuple(
                        replace(player, provider=name)
                        if player.controller == "llm"
                        else player
                        for player in self.config.players
                    ),
                )
                self.dirty = True
                continue
            elif selected == "remove_key":
                updated = replace(provider, api_key=None)
            providers = dict(self.config.providers)
            providers[name] = updated
            self.config = replace(self.config, providers=providers)
            self.dirty = True

    def _edit_rules(self) -> None:
        """Edit supported house rules from a compact toggle menu."""
        while True:
            rules = self.config.rules
            fields = [
                ("witch_can_self_save", "女巫可以自救"),
                ("witch_can_use_two_potions_same_night", "女巫同夜可用两瓶药"),
                ("reveal_roles_on_death", "死亡时公开身份"),
                ("allow_self_vote", "允许投票给自己"),
                ("last_words", "启用遗言"),
                ("first_night_last_words", "首夜死亡有遗言"),
                ("night_death_last_words", "普通夜死有遗言"),
                ("day_vote_last_words", "白天放逐有遗言"),
                ("hunter_shot_last_words", "猎人枪杀目标有遗言"),
                ("randomize_discussion_start", "随机讨论起始座位"),
                ("randomize_seating", "开局随机座位"),
            ]
            choices = [
                Choice("max_days", f"最大天数                  {rules.max_days}"),
                Choice(
                    "wolf_chat_rounds",
                    f"每夜狼人讨论轮数          {rules.wolf_chat_rounds}",
                ),
            ]
            choices.extend(
                Choice(
                    field_name, f"{label:<24}{self._on_off(getattr(rules, field_name))}"
                )
                for field_name, label in fields
            )
            choices.extend(
                [
                    Choice("reset", "恢复推荐房规"),
                    Choice("done", "完成"),
                ],
            )
            selected = self.ui.select("房规", choices, default="done")
            if selected in {None, "done"}:
                return
            if selected == "reset":
                self.config = replace(self.config, rules=RuleConfig())
            elif selected == "max_days":
                self.config = replace(
                    self.config,
                    rules=replace(
                        rules,
                        max_days=self.ui.number_input(
                            "房规  ·  最大天数",
                            "达到上限仍未决出胜负时结束对局。",
                            default=rules.max_days,
                            minimum=1,
                            maximum=999,
                        ),
                    ),
                )
            elif selected == "wolf_chat_rounds":
                self.config = replace(
                    self.config,
                    rules=replace(
                        rules,
                        wolf_chat_rounds=self.ui.number_input(
                            "房规  ·  狼人讨论",
                            "每夜袭击前的队内讨论轮数；0 表示直接行动。",
                            default=rules.wolf_chat_rounds,
                            minimum=0,
                            maximum=20,
                        ),
                    ),
                )
            else:
                field_name = cast("str", selected)
                self.config = replace(
                    self.config,
                    rules=replace(
                        rules,
                        **{field_name: not getattr(rules, field_name)},
                    ),
                )
            self.dirty = True

    def _edit_experience(self) -> None:
        """Edit safety, recovery, terminal, and output behavior."""
        while True:
            config = self.config
            safe_checkpoint = sanitize_rendered_text(
                config.checkpoint_path or "关闭",
                limit=110,
            )
            safe_transcript = sanitize_rendered_text(
                config.public_transcript_path or "关闭",
                limit=110,
            )
            safe_memory = sanitize_rendered_text(
                config.memory_directory or "关闭",
                limit=110,
            )
            selected = self.ui.select(
                "终端体验与安全",
                [
                    Choice(
                        "clear",
                        f"私密回合间清屏          {self._on_off(config.clear_screen)}",
                    ),
                    Choice(
                        "progress",
                        f"安全观战进度            {self._on_off(config.spectator_progress)}",
                    ),
                    Choice(
                        "strict",
                        f"控制器严格模式          {self._on_off(config.strict_controllers)}",
                    ),
                    Choice(
                        "retries",
                        f"控制器重试次数          {config.controller_retries}",
                    ),
                    Choice(
                        "checkpoint",
                        f"私密恢复点              {safe_checkpoint}",
                    ),
                    Choice(
                        "transcript",
                        f"公开观战日志            {safe_transcript}",
                    ),
                    Choice(
                        "memory",
                        f"终局个人记忆            {safe_memory}",
                    ),
                    Choice(
                        "confirm",
                        f"真人关键选择二次确认    {self._on_off(config.confirm_critical_actions)}",
                    ),
                    Choice(
                        "notes",
                        f"真人私密策略笔记        {self._on_off(config.human_strategy_notes)}",
                    ),
                    Choice(
                        "votes",
                        f"LLM 公开投票并发        {self._on_off(config.parallel_llm_votes)}",
                    ),
                    Choice(
                        "context",
                        f"LLM 历史字符上限        {config.context_char_limit}",
                    ),
                    Choice("reset", "恢复推荐体验设置"),
                    Choice("done", "完成"),
                ],
                default="done",
            )
            if selected in {None, "done"}:
                return
            if selected == "clear":
                self.config = replace(config, clear_screen=not config.clear_screen)
            elif selected == "progress":
                self.config = replace(
                    config,
                    spectator_progress=not config.spectator_progress,
                )
            elif selected == "strict":
                self.config = replace(
                    config,
                    strict_controllers=not config.strict_controllers,
                )
            elif selected == "retries":
                self.config = replace(
                    config,
                    controller_retries=self.ui.number_input(
                        "控制器重试次数",
                        "模型调用失败或选择非法时的额外尝试次数。",
                        default=config.controller_retries,
                        minimum=0,
                        maximum=20,
                    ),
                )
            elif selected == "checkpoint":
                self.config = replace(
                    config,
                    checkpoint_path=self._optional_path(
                        "私密恢复点",
                        "包含身份和私密行动，请勿公开分享；留空关闭。",
                        config.checkpoint_path,
                        RECOMMENDED_CHECKPOINT_PATH,
                    ),
                )
            elif selected == "transcript":
                self.config = replace(
                    config,
                    public_transcript_path=self._optional_path(
                        "公开观战日志",
                        "只记录可公开事件；留空关闭。",
                        config.public_transcript_path,
                        RECOMMENDED_PUBLIC_TRANSCRIPT_PATH,
                    ),
                )
            elif selected == "memory":
                self.config = replace(
                    config,
                    memory_directory=self._optional_path(
                        "终局个人记忆",
                        "每名玩家分别导出个人视角；留空关闭。",
                        config.memory_directory,
                        "game_memories",
                    ),
                )
            elif selected == "confirm":
                self.config = replace(
                    config,
                    confirm_critical_actions=not config.confirm_critical_actions,
                )
            elif selected == "notes":
                self.config = replace(
                    config,
                    human_strategy_notes=not config.human_strategy_notes,
                )
            elif selected == "votes":
                self.config = replace(
                    config,
                    parallel_llm_votes=not config.parallel_llm_votes,
                )
            elif selected == "context":
                self.config = replace(
                    config,
                    context_char_limit=self.ui.number_input(
                        "LLM 历史字符上限",
                        "每名玩家可见历史进入提示词的字符上限。",
                        default=config.context_char_limit,
                        minimum=2000,
                        maximum=10_000_000,
                    ),
                )
            elif selected == "reset":
                self.config = replace(
                    config,
                    clear_screen=True,
                    spectator_progress=True,
                    strict_controllers=True,
                    controller_retries=2,
                    checkpoint_path=RECOMMENDED_CHECKPOINT_PATH,
                    public_transcript_path=RECOMMENDED_PUBLIC_TRANSCRIPT_PATH,
                    memory_directory="game_memories",
                    confirm_critical_actions=True,
                    human_strategy_notes=False,
                    parallel_llm_votes=True,
                    context_char_limit=24000,
                )
            self.dirty = True

    def _review(self) -> TUIResult | None:
        """Show validation results, save atomically, and optionally start."""
        errors, warnings = self._issues()
        counts = Counter(player.controller for player in self.config.players)
        safe_checkpoint = sanitize_rendered_text(
            self.config.checkpoint_path or "关闭",
            limit=180,
        )
        safe_transcript = sanitize_rendered_text(
            self.config.public_transcript_path or "关闭",
            limit=180,
        )
        safe_path = sanitize_rendered_text(self.path, limit=180)
        body = [
            f"  牌组      {_PRESET_LABELS[self.config.role_preset]} · {len(self.config.players)} 人",
            f"  席位      真人 {counts['human']} · LLM {counts['llm']} · bot {counts['bot']}",
            f"  模型      {self._provider_summary()}",
            f"  恢复点    {safe_checkpoint}",
            f"  公开日志  {safe_transcript}",
            "",
        ]
        if errors:
            body.append(self.ui.styled("需要处理", _BOLD + _RED))
            body.extend(
                f"  × {sanitize_rendered_text(error, limit=300)}" for error in errors
            )
        else:
            body.append(self.ui.styled("✓ 配置校验通过", _BOLD + _GREEN))
        if warnings:
            body.extend(("", self.ui.styled("启动提醒", _BOLD + _YELLOW)))
            body.extend(
                f"  ! {sanitize_rendered_text(warning, limit=300)}"
                for warning in warnings
            )
        if errors:
            self.ui.notice("检查配置", "\n".join(body))
            return None
        action = self.ui.select(
            "检查配置",
            [
                Choice("start", "保存并开始游戏", "退出配置界面后立即开局"),
                Choice("save", "仅保存", f"写入 {safe_path}"),
                Choice("back", "返回修改"),
            ],
            subtitle="配置校验通过",
            default="start",
            body_prefix=body,
        )
        if action in {None, "back"}:
            return None
        if (
            action == "start"
            and warnings
            and not self.ui.confirm(
                "带提醒启动",
                "这些提醒不会阻止保存。确认继续？",
                default=True,
            )
        ):
            return None
        write_config(
            self.config, self.path, overwrite=self.existing or self.path.exists()
        )
        self.existing = True
        self.dirty = False
        return TUIResult(self.path, start_game=action == "start", saved=True)

    def _issues(self) -> tuple[list[str], list[str]]:
        """Return blocking validation errors and non-blocking launch warnings."""
        errors: list[str] = []
        warnings: list[str] = []
        try:
            validate_config(self.config)
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
        referenced = {
            player.provider
            for player in self.config.players
            if player.controller == "llm" and player.provider
        }
        for name in sorted(referenced):
            provider = self.config.providers.get(name)
            if provider is None:
                continue
            if provider.model in {"your-model-id", ""}:
                errors.append(f"Provider {name!r} 还没有填写可用的模型 ID")
            if provider.api_key_env and not os.environ.get(provider.api_key_env):
                warnings.append(
                    f"启动前需要设置环境变量 {provider.api_key_env}",
                )
            if provider.api_key is not None:
                warnings.append(
                    f"Provider {name!r} 仍在 JSON 中保存明文 api_key",
                )
        if not self.config.checkpoint_path:
            warnings.append("未启用私密恢复点，中止后无法续局")
        if not self.config.strict_controllers:
            warnings.append("控制器失败时会使用系统安全后备，本局不属于完整 LLM 对局")
        return errors, warnings

    def _resize_players(self, count: int) -> tuple[PlayerConfig, ...]:
        """Resize the table while preserving existing seats wherever possible."""
        players = list(self.config.players[:count])
        default_provider = self._default_provider_name()
        while len(players) < count:
            index = len(players) + 1
            players.append(
                PlayerConfig(
                    name=f"智能体{index}",
                    controller="llm",
                    provider=default_provider,
                ),
            )
        return tuple(self._deduplicate_generated_names(players))

    def _apply_layout(self, controllers: list[str]) -> None:
        """Apply controller types and sensible names without overwriting custom ones."""
        default_provider = self._default_provider_name()
        counters: Counter[str] = Counter()
        players: list[PlayerConfig] = []
        for player, controller in zip(self.config.players, controllers):
            counters[controller] += 1
            name = player.name
            if self._is_generated_name(name):
                name = self._generated_name(controller, counters[controller])
            players.append(
                replace(
                    player,
                    name=name,
                    controller=controller,
                    provider=default_provider if controller == "llm" else None,
                ),
            )
        self.config = replace(
            self.config,
            players=tuple(self._deduplicate_generated_names(players)),
        )
        self.dirty = True

    @staticmethod
    def _is_generated_name(name: str) -> bool:
        """Recognize names created by the templates so layouts may relabel them."""
        if name == "你":
            return True
        prefixes = ("真人", "智能体", "机器人", "玩家")
        return any(
            name.startswith(prefix) and name[len(prefix) :].isdigit()
            for prefix in prefixes
        )

    @staticmethod
    def _generated_name(controller: str, ordinal: int) -> str:
        """Return a friendly generated name for one controller type."""
        if controller == "human":
            return "你" if ordinal == 1 else f"真人{ordinal}"
        if controller == "llm":
            return f"智能体{ordinal}"
        return f"机器人{ordinal}"

    @staticmethod
    def _deduplicate_generated_names(
        players: list[PlayerConfig],
    ) -> list[PlayerConfig]:
        """Avoid collisions introduced when generated and custom names mix."""
        used: set[str] = set()
        result: list[PlayerConfig] = []
        for player in players:
            name = player.name
            if name in used:
                suffix = 2
                while f"{name}-{suffix}" in used:
                    suffix += 1
                name = f"{name}-{suffix}"
            used.add(name)
            result.append(replace(player, name=name))
        return result

    def _default_provider_name(self) -> str | None:
        """Choose the conventional, currently referenced, or sole provider."""
        if "default" in self.config.providers:
            return "default"
        for player in self.config.players:
            if player.controller == "llm" and player.provider in self.config.providers:
                return player.provider
        return next(iter(self.config.providers), None)

    def _optional_path(
        self,
        title: str,
        description: str,
        current: str | None,
        recommended: str,
    ) -> str | None:
        """Edit an optional output path where an empty value disables output."""
        value = self.ui.text_input(
            title,
            description,
            default=current or recommended,
            allow_empty=True,
            subtitle="输入单个 - 可明确关闭；直接回车保留建议路径",
        )
        return None if value == "-" else value or None

    def _provider_summary(self) -> str:
        """Return a compact provider summary for dashboards."""
        llm_players = [
            player for player in self.config.players if player.controller == "llm"
        ]
        if not llm_players:
            return "当前席位无需网络模型"
        referenced = {
            player.provider for player in llm_players if player.provider is not None
        }
        if len(referenced) == 1:
            name = next(iter(referenced))
            provider = self.config.providers.get(name)
            if provider is not None:
                return sanitize_rendered_text(
                    f"{name} · {provider.model} · {provider.wire_api}",
                    limit=180,
                )
        return f"{len(self.config.providers)} 个 Provider · {len(referenced)} 个已使用"

    def _rules_summary(self) -> str:
        """Return the key house-rule values shown on the dashboard."""
        rules = self.config.rules
        role_reveal = "死亡亮身份" if rules.reveal_roles_on_death else "死亡不亮身份"
        return f"最多 {rules.max_days} 天 · 狼聊 {rules.wolf_chat_rounds} 轮 · {role_reveal}"

    def _experience_summary(self) -> str:
        """Return the safety posture shown on the dashboard."""
        checkpoint = "恢复点开启" if self.config.checkpoint_path else "无恢复点"
        strict = "严格模式" if self.config.strict_controllers else "允许安全后备"
        return f"{strict} · {checkpoint} · 重试 {self.config.controller_retries} 次"

    @staticmethod
    def _player_description(player: PlayerConfig) -> str:
        """Return one safe, compact seat description."""
        controller = _CONTROLLER_LABELS[player.controller]
        if player.controller == "llm":
            controller += f" · {player.provider or '未绑定 Provider'}"
        if player.fixed_role is not None:
            controller += f" · 固定{ROLE_NAMES['zh-CN'][player.fixed_role]}"
        return controller

    @staticmethod
    def _on_off(value: bool) -> str:
        """Use an immediately scannable localized boolean label."""
        return "开启" if value else "关闭"


def run_config_tui(
    path: str | Path = "werewolf.json",
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    color: bool | None = None,
) -> TUIResult | None:
    """Create or edit a configuration through the terminal workbench."""
    ui = TerminalUI(
        stdin=stdin or sys.stdin,
        stdout=stdout or sys.stdout,
        color=color,
    )
    with ui:
        return ConfigurationTUI(Path(path), ui).run()
