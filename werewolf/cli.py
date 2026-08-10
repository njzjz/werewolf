"""Command-line entry point for configuring and running games."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from .config import (
    ROLE_PRESET_SIZES,
    GameConfig,
    demo_config,
    load_config,
    write_example_config,
)
from .engine import Game
from .rendering import sanitize_rendered_text
from .tui import run_config_tui


def _print_resume_hint(active_checkpoint: str | None, config_path: str) -> None:
    """Print one copyable recovery command when a private checkpoint exists."""
    if active_checkpoint and Path(active_checkpoint).exists():
        print(
            f"恢复点已保留，可运行：werewolf play {config_path} "
            f"--resume {active_checkpoint}",
            file=sys.stderr,
        )


def _safe_cli_error(exc: BaseException) -> str:
    """Bound and flatten exception text before writing it to the terminal."""
    return sanitize_rendered_text(exc, limit=500).replace("\n", " ")


def _config_path_from_args(args: argparse.Namespace) -> str:
    """Resolve the concise positional path while retaining --config compatibility."""
    if args.config_path and args.config_option:
        msg = "配置路径只能通过位置参数或 --config 指定一次"
        raise ValueError(msg)
    return str(args.config_path or args.config_option or "werewolf.json")


def _validate_play_modes(resume_checkpoint: str | None, force_new: bool) -> None:
    """Reject mutually exclusive destructive and recovery modes."""
    if resume_checkpoint and force_new:
        msg = "--resume 与 --force-new 不能同时使用"
        raise ValueError(msg)


def _validate_provider_credentials(config: GameConfig) -> None:
    """Fail before creating a match when a used provider environment key is absent.

    Provider keys remain lazily resolved by the HTTP client so local unauthenticated
    endpoints keep working. An explicitly configured environment variable, however,
    is an operator requirement and should be checked before roles, logs, or a fresh
    checkpoint are created.
    """
    used_providers = sorted(
        {
            player.provider
            for player in config.players
            if player.controller == "llm" and player.provider is not None
        },
    )
    for name in used_providers:
        provider = config.providers[name]
        if provider.api_key_env is None:
            continue
        try:
            provider.resolved_api_key()
        except ValueError:
            if config.language == "en":
                msg = (
                    f"Provider {name!r} requires environment variable "
                    f"{provider.api_key_env!r}. Set it in this terminal before "
                    "starting or resuming the game."
                )
            else:
                msg = (
                    f"Provider {name!r} 需要环境变量 {provider.api_key_env!r}；"
                    "请先在当前终端设置该变量，再开始或恢复游戏。"
                )
            raise ValueError(msg) from None


def build_parser() -> argparse.ArgumentParser:
    """Create the public CLI parser."""
    parser = argparse.ArgumentParser(
        prog="werewolf",
        description="纯终端的真人/LLM 狼人杀游戏",
    )
    subparsers = parser.add_subparsers(dest="command")

    configure_parser = subparsers.add_parser(
        "configure",
        aliases=["config", "setup"],
        help="通过 TUI 创建或编辑配置",
    )
    configure_parser.add_argument(
        "path",
        nargs="?",
        default="werewolf.json",
        help="要创建或编辑的 JSON 配置路径",
    )
    configure_parser.add_argument(
        "--no-color",
        action="store_true",
        help="关闭 TUI 彩色样式",
    )

    init_parser = subparsers.add_parser("init", help="生成一份 JSON 配置模板")
    init_parser.add_argument("path", nargs="?", default="werewolf.json")
    init_parser.add_argument("--force", action="store_true", help="覆盖已有文件")
    init_parser.add_argument(
        "--full",
        action="store_true",
        help="生成包含全部高级选项的完整参考模板",
    )

    play_parser = subparsers.add_parser("play", help="按配置开始游戏")
    play_parser.add_argument(
        "config_path",
        nargs="?",
        help="JSON 配置路径；默认 werewolf.json",
    )
    play_parser.add_argument(
        "--config",
        dest="config_option",
        help="JSON 配置路径（兼容旧用法）",
    )
    play_parser.add_argument(
        "--no-clear",
        action="store_true",
        help="不清屏，仅适合单真人日志与调试；多真人模式会拒绝启动",
    )
    play_parser.add_argument(
        "--no-memory",
        action="store_true",
        help="结束后不导出个人记忆",
    )
    progress_mode = play_parser.add_mutually_exclusive_group()
    progress_mode.add_argument(
        "--spectator",
        action="store_true",
        help="实时显示不泄密的 LLM 行动与推理进度",
    )
    progress_mode.add_argument(
        "--no-spectator",
        action="store_true",
        help="关闭 LLM 行动与推理进度显示",
    )
    controller_mode = play_parser.add_mutually_exclusive_group()
    controller_mode.add_argument(
        "--strict-controllers",
        action="store_true",
        help="控制器失败或非法选择时终止，不使用本地机器人后备",
    )
    controller_mode.add_argument(
        "--allow-fallback",
        action="store_true",
        help="控制器重试耗尽后使用有明确标识的确定性安全后备",
    )
    play_parser.add_argument(
        "--transcript",
        help="将公开观战频道实时写入指定 UTF-8 文件",
    )
    play_parser.add_argument(
        "--controller-retries",
        type=int,
        help="模型调用失败或返回非法选择时的重试次数",
    )
    play_parser.add_argument(
        "--checkpoint",
        help="在安全阶段及每次控制器响应后原子保存私密恢复点",
    )
    play_parser.add_argument(
        "--resume",
        help="从指定私密恢复点继续游戏",
    )
    play_parser.add_argument(
        "--force-new",
        action="store_true",
        help="明确放弃已有恢复点和输出文件并开始新对局",
    )
    play_parser.add_argument(
        "--strategy-notes",
        action="store_true",
        help="每次真人行动后询问可选的私密策略笔记",
    )
    play_parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="真人关键选择不再二次确认",
    )
    play_parser.add_argument(
        "--json-result",
        action="store_true",
        help="在本地化结算后额外输出一行机器可读 JSON",
    )
    play_parser.add_argument(
        "--sequential-votes",
        action="store_true",
        help="禁用互不可见的 LLM 公开投票并发请求",
    )

    demo_parser = subparsers.add_parser("demo", help="运行无需 API 的本地机器人演示")
    demo_parser.add_argument("--players", type=int, choices=range(6, 17))
    demo_parser.add_argument("--seed", type=int, default=7)
    demo_parser.add_argument(
        "--preset",
        default="classic",
        choices=tuple(ROLE_PRESET_SIZES),
        help="游戏模式：classic、killer、ghost_similar、ghost_blank 或电影系列预设",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Execute a CLI subcommand and provide concise terminal errors."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            args.command = "configure"
            args.path = "werewolf.json"
            args.no_color = False
        else:
            parser.print_help()
            return
    resume_checkpoint: str | None = None
    active_checkpoint: str | None = None
    config_path = "werewolf.json"
    config = None
    in_configurator = False
    try:
        if args.command in {"configure", "config", "setup"}:
            in_configurator = True
            tui_result = run_config_tui(
                args.path,
                color=False if args.no_color else None,
            )
            in_configurator = False
            if tui_result is None:
                return
            config_path = str(tui_result.config_path)
            if tui_result.saved:
                print(f"配置已保存：{tui_result.config_path}")
            if not tui_result.start_game:
                print(f"准备开局时运行：werewolf play {tui_result.config_path}")
                return
            config = load_config(tui_result.config_path)
            active_checkpoint = config.checkpoint_path
        elif args.command == "init":
            path = write_example_config(args.path, force=args.force, full=args.full)
            print(f"已生成配置：{path}")
            if args.full:
                print("已生成完整参考模板；常规开局通常只需精简模板。")
            else:
                print(
                    "请设置 OPENAI_API_KEY，确认 model 和玩家列表后运行："
                    f"werewolf play {path}",
                )
            return
        if args.command == "demo":
            preset_size = ROLE_PRESET_SIZES[args.preset]
            player_count = args.players or preset_size or 8
            config = demo_config(player_count, args.seed, args.preset)
        else:
            config_path = _config_path_from_args(args)
            config = load_config(Path(config_path))
            if args.no_clear:
                config = replace(config, clear_screen=False)
            if args.no_memory:
                config = replace(config, memory_directory=None)
            if args.spectator:
                config = replace(config, spectator_progress=True)
            if args.no_spectator:
                config = replace(config, spectator_progress=False)
            if args.strict_controllers:
                config = replace(config, strict_controllers=True)
            if args.allow_fallback:
                config = replace(config, strict_controllers=False)
            if args.transcript:
                config = replace(config, public_transcript_path=args.transcript)
            if args.controller_retries is not None:
                config = replace(config, controller_retries=args.controller_retries)
            if args.checkpoint:
                config = replace(config, checkpoint_path=args.checkpoint)
            if args.strategy_notes:
                config = replace(config, human_strategy_notes=True)
            if args.no_confirm:
                config = replace(config, confirm_critical_actions=False)
            if args.sequential_votes:
                config = replace(config, parallel_llm_votes=False)
            resume_checkpoint = args.resume
            _validate_play_modes(resume_checkpoint, args.force_new)
            active_checkpoint = resume_checkpoint or config.checkpoint_path
        _validate_provider_credentials(config)
        result = Game(
            config,
            resume_checkpoint=resume_checkpoint,
            force_new=(args.command == "play" and args.force_new),
        ).run()
        winner = result.winner.value if result.winner else "draw"
        duration = f"{result.duration_seconds:.1f}"
        seat_labels = dict(result.seat_labels)
        survivor_labels = [seat_labels.get(name, name) for name in result.survivors]
        if config.language == "en":
            print(
                f"\nMatch complete: {result.days} days, {duration}s; "
                f"survivors: {', '.join(survivor_labels) or 'none'}; "
                f"safe fallbacks: {result.controller_fallbacks}.",
            )
        else:
            print(
                f"\n本局完成：共 {result.days} 天，用时 {duration} 秒；"
                f"存活：{'、'.join(survivor_labels) or '无'}；"
                f"系统安全后备：{result.controller_fallbacks} 次。",
            )
        if args.command == "play" and args.json_result:
            print(
                json.dumps(
                    {
                        "winner": winner,
                        "winning_players": list(result.winning_players),
                        "prize_shares": dict(result.prize_shares),
                        "days": result.days,
                        "survivors": list(result.survivors),
                        "reason": result.reason,
                        "duration_seconds": result.duration_seconds,
                        "controller_actions": result.controller_actions,
                        "controller_attempts": result.controller_attempts,
                        "controller_failures": result.controller_failures,
                        "controller_retries": result.controller_retries,
                        "controller_fallbacks": result.controller_fallbacks,
                        "seat_labels": seat_labels,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
    except (EOFError, KeyboardInterrupt) as exc:
        interrupted_by_eof = isinstance(exc, EOFError)
        if in_configurator:
            message = (
                "\n输入已关闭，未保存的配置修改已放弃。"
                if interrupted_by_eof
                else "\n配置已取消，未保存的修改已放弃。"
            )
            print(message, file=sys.stderr)
            raise SystemExit(130) from None
        language = getattr(config, "language", "zh-CN")
        if language == "en":
            message = (
                "\nInput closed; the game was interrupted safely."
                if interrupted_by_eof
                else "\nGame interrupted."
            )
        else:
            message = (
                "\n输入已关闭，游戏已安全中止。"
                if interrupted_by_eof
                else "\n游戏已中止。"
            )
        print(message, file=sys.stderr)
        _print_resume_hint(active_checkpoint, config_path)
        raise SystemExit(130) from None
    except RuntimeError as exc:
        print(f"错误：{_safe_cli_error(exc)}", file=sys.stderr)
        _print_resume_hint(active_checkpoint, config_path)
        raise SystemExit(2) from exc
    except OSError as exc:
        print(f"错误：{_safe_cli_error(exc)}", file=sys.stderr)
        _print_resume_hint(active_checkpoint, config_path)
        raise SystemExit(2) from exc
    except (
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"错误：{_safe_cli_error(exc)}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
