"""Command-line entry point for configuring and running games."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .config import demo_config, load_config, write_example_config
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

    demo_parser = subparsers.add_parser("demo", help="运行无需 API 的本地机器人演示")
    demo_parser.add_argument("--players", type=int, default=8, choices=range(6, 17))
    demo_parser.add_argument("--seed", type=int, default=7)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Execute a CLI subcommand and provide concise terminal errors."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            path = write_example_config(args.path, force=args.force)
            print(f"已生成配置：{path}")
            print("请设置 OPENAI_API_KEY，并按需修改 base_url、model 和玩家列表。")
            return
        if args.command == "demo":
            config = demo_config(args.players, args.seed)
        else:
            config = load_config(Path(args.config))
            if args.no_clear:
                config = replace(config, clear_screen=False)
            if args.no_memory:
                config = replace(config, memory_directory=None)
        result = Game(config).run()
        winner = result.winner.value if result.winner else "draw"
        print(
            f"\n游戏结束：winner={winner}, days={result.days}, survivors={list(result.survivors)}",
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
