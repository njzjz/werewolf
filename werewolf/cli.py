"""Command-line entry point for configuring and running games."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .config import ROLE_PRESET_SIZES, demo_config, load_config, write_example_config
from .engine import Game


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

    play_parser = subparsers.add_parser("play", help="按配置开始游戏")
    play_parser.add_argument("--config", default="werewolf.json", help="JSON 配置路径")
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
    play_parser.add_argument(
        "--spectator",
        action="store_true",
        help="实时显示不泄密的 LLM 行动与推理进度",
    )
    play_parser.add_argument(
        "--strict-controllers",
        action="store_true",
        help="控制器失败或非法选择时终止，不使用本地机器人后备",
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
    try:
        if args.command == "init":
            path = write_example_config(args.path, force=args.force)
            print(f"已生成配置：{path}")
            print("请设置 OPENAI_API_KEY，并按需修改 base_url、model 和玩家列表。")
            return
        if args.command == "demo":
            preset_size = ROLE_PRESET_SIZES[args.preset]
            player_count = args.players or preset_size or 8
            config = demo_config(player_count, args.seed, args.preset)
        else:
            config = load_config(Path(args.config))
            if args.no_clear:
                config = replace(config, clear_screen=False)
            if args.no_memory:
                config = replace(config, memory_directory=None)
            if args.spectator:
                config = replace(config, spectator_progress=True)
            if args.strict_controllers:
                config = replace(config, strict_controllers=True)
            if args.transcript:
                config = replace(config, public_transcript_path=args.transcript)
            if args.controller_retries is not None:
                config = replace(config, controller_retries=args.controller_retries)
            if args.checkpoint:
                config = replace(config, checkpoint_path=args.checkpoint)
            resume_checkpoint = args.resume
        result = Game(config, resume_checkpoint=resume_checkpoint).run()
        winner = result.winner.value if result.winner else "draw"
        print(
            f"\n游戏结束：winner={winner}, winners={list(result.winning_players)}, "
            f"prize_shares={dict(result.prize_shares)}, days={result.days}, "
            f"survivors={list(result.survivors)}",
        )
    except KeyboardInterrupt:
        print("\n游戏已中止。", file=sys.stderr)
        raise SystemExit(130) from None
    except (
        FileNotFoundError,
        FileExistsError,
        KeyError,
        ValueError,
        RuntimeError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
