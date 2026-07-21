"""Command-line entry point for configuring and running games."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from .config import ROLE_PRESET_SIZES, demo_config, load_config, write_example_config
from .engine import Game


def _config_path_from_args(args: argparse.Namespace) -> str:
    """Resolve the concise positional path while retaining --config compatibility."""
    if args.config_path and args.config_option:
        msg = "配置路径只能通过位置参数或 --config 指定一次"
        raise ValueError(msg)
    return str(args.config_path or args.config_option or "werewolf.json")


def build_parser() -> argparse.ArgumentParser:
    """Create the public CLI parser."""
    parser = argparse.ArgumentParser(
        prog="werewolf",
        description="纯终端的真人/LLM 狼人杀游戏",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

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
        help="不清屏，适合日志与调试",
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
        help="身份牌组：classic 或电影系列预设",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Execute a CLI subcommand and provide concise terminal errors."""
    args = build_parser().parse_args(argv)
    resume_checkpoint: str | None = None
    active_checkpoint: str | None = None
    config_path = "werewolf.json"
    try:
        if args.command == "init":
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
            active_checkpoint = resume_checkpoint or config.checkpoint_path
        result = Game(config, resume_checkpoint=resume_checkpoint).run()
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
    except KeyboardInterrupt:
        print("\n游戏已中止。", file=sys.stderr)
        raise SystemExit(130) from None
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        if active_checkpoint and Path(active_checkpoint).exists():
            print(
                f"恢复点已保留，可运行：werewolf play {config_path} "
                f"--resume {active_checkpoint}",
                file=sys.stderr,
            )
        raise SystemExit(2) from exc
    except (
        FileNotFoundError,
        FileExistsError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
