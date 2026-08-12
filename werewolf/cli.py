"""Command-line entry point for configuring and running games."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from .agents import Terminal
from .config import (
    ROLE_PRESET_SIZES,
    GameConfig,
    demo_config,
    load_config,
    write_config,
    write_example_config,
)
from .engine import Game
from .rendering import sanitize_rendered_text
from .training import (
    apply_skill_improvement_manifest,
    build_skill_improvement_manifest,
    load_skill_leaderboard,
    write_skill_improvement_manifest,
)
from .tui import run_config_tui


class _ArenaTerminal(Terminal):
    """Suppress per-turn rendering while a batch arena collects trajectories."""

    def __init__(self) -> None:
        super().__init__(clear_screen=False)

    def announce(self, _text: str) -> None:
        """Discard public game narration; the judge still records observations."""

    def progress(self, _text: str) -> None:
        """Discard durable spectator progress during batch execution."""

    def metric(self, _text: str, *, label: str = "统计") -> None:
        """Discard per-match metrics; the arena prints aggregate results."""

    def notice(self, _text: str, *, label: str = "提示") -> None:
        """Discard repeated preflight notices in a validated batch."""

    def transient_progress(self, _text: str) -> None:
        """Discard transient action status during batch execution."""

    def say(
        self,
        _player_name: str,
        _text: str,
        *,
        fallback_label: str | None = None,
    ) -> None:
        """Discard public speech rendering without altering boundary delivery."""


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


def _validate_arena_config(config: GameConfig, games: int) -> None:
    """Reject batch configurations that cannot run without interaction."""
    if games < 1:
        msg = "--games must be positive"
        raise ValueError(msg)
    if any(player.controller == "human" for player in config.players):
        msg = "Arena configurations cannot contain human controllers"
        raise ValueError(msg)


def _validate_improvement_outputs(
    output_config: str,
    manifest: str,
    *,
    force: bool,
) -> tuple[Path, Path]:
    """Resolve both generated files before any user data can be replaced."""
    output_path = Path(output_config)
    manifest_path = Path(manifest)
    if output_path.resolve() == manifest_path.resolve():
        msg = "Candidate config and manifest must use different paths"
        raise ValueError(msg)
    if not force:
        if output_path.exists():
            msg = f"Configuration already exists: {output_path}"
            raise FileExistsError(msg)
        if manifest_path.exists():
            msg = f"Skill improvement manifest already exists: {manifest_path}"
            raise FileExistsError(msg)
    return output_path, manifest_path


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
    play_parser.add_argument(
        "--trajectory",
        help="将含个人视角、动作和终局奖励的私密训练轨迹追加到 JSONL 文件",
    )

    leaderboard_parser = subparsers.add_parser(
        "leaderboard",
        help="从训练轨迹统计精确 skill 版本的胜率和平均回报",
    )
    leaderboard_parser.add_argument("trajectory_path", help="训练轨迹 JSONL 文件")
    leaderboard_parser.add_argument(
        "--skill",
        help="只比较指定 skill 名称，例如 role_seer",
    )
    leaderboard_parser.add_argument(
        "--json",
        action="store_true",
        help="输出机器可读 JSON",
    )
    leaderboard_parser.add_argument(
        "--selection",
        choices=("mean", "ucb"),
        default="mean",
        help="按平均回报利用，或按 UCB 为低样本候选保留探索机会",
    )

    improve_parser = subparsers.add_parser(
        "improve-skills",
        aliases=["train-skills"],
        help="从奖励轨迹生成版本化 role skill 候选并写入新配置",
    )
    improve_parser.add_argument("trajectory_path", help="私密训练轨迹 JSONL 文件")
    improve_parser.add_argument("config_path", help="要附加候选 skill 的基础配置")
    improve_parser.add_argument(
        "--output-config",
        required=True,
        help="写入候选 skill_overrides 的新配置路径",
    )
    improve_parser.add_argument(
        "--manifest",
        required=True,
        help="保存候选来源、指标、指纹与完整内容的私密 JSON 清单",
    )
    improve_parser.add_argument(
        "--skill",
        action="append",
        dest="skills",
        help="只更新指定 skill；可重复，例如 role_seer、role_werewolf",
    )
    improve_parser.add_argument(
        "--version-prefix",
        default="selfplay-v1",
        help="候选版本前缀；默认 selfplay-v1",
    )
    improve_parser.add_argument(
        "--selection",
        choices=("mean", "ucb"),
        default="mean",
        help="按平均回报选择源版本，或用 UCB 继续探索低样本版本",
    )
    improve_parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的候选配置和 manifest",
    )

    arena_parser = subparsers.add_parser(
        "arena",
        help="用连续随机种子批量运行无真人自博弈并写入训练轨迹",
    )
    arena_parser.add_argument("config_path", help="竞技场 JSON 配置")
    arena_parser.add_argument(
        "--games",
        type=int,
        default=10,
        help="批量运行的对局数；默认 10",
    )
    arena_parser.add_argument(
        "--seed-start",
        type=int,
        default=0,
        help="第一局使用的随机种子；后续逐局加一",
    )
    arena_parser.add_argument(
        "--trajectory",
        required=True,
        help="追加所有私密训练轨迹的 JSONL 文件",
    )

    demo_parser = subparsers.add_parser("demo", help="运行无需 API 的本地机器人演示")
    demo_parser.add_argument("--players", type=int, choices=range(6, 17))
    demo_parser.add_argument("--seed", type=int, default=7)
    demo_parser.add_argument(
        "--preset",
        default="classic",
        choices=tuple(
            preset for preset in ROLE_PRESET_SIZES if preset != "ghost_blank"
        ),
        help="游戏模式：classic、killer、ghost_similar 或电影系列预设",
    )
    return parser


def main(argv: list[str] | None = None) -> None:  # noqa: PLR0911
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
        if args.command == "leaderboard":
            rows = load_skill_leaderboard(
                args.trajectory_path,
                skill_name=args.skill,
                selection=args.selection,
            )
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, sort_keys=True))
            elif not rows:
                print("没有找到符合条件的 skill 轨迹。")
            else:
                print(
                    "skill\tversion\tfingerprint\tsamples\twin\tsurvival\treturn\tucb",
                )
                for row in rows:
                    print(
                        f"{row['name']}\t{row['version']}\t{row['fingerprint']}\t"
                        f"{row['samples']}\t{row['win_rate']:.3f}\t"
                        f"{row['survival_rate']:.3f}\t{row['mean_return']:.3f}\t"
                        f"{row['ucb_score']:.3f}",
                    )
            return
        if args.command in {"improve-skills", "train-skills"}:
            output_path, manifest_path = _validate_improvement_outputs(
                args.output_config,
                args.manifest,
                force=args.force,
            )
            manifest = build_skill_improvement_manifest(
                args.trajectory_path,
                skill_names=args.skills,
                version_prefix=args.version_prefix,
                selection=args.selection,
            )
            base_config = load_config(Path(args.config_path))
            improved_config = apply_skill_improvement_manifest(base_config, manifest)
            # Write the runtime config first. If it cannot be replaced, an
            # existing private manifest remains untouched even under --force.
            write_config(
                improved_config,
                output_path,
                overwrite=args.force,
            )
            try:
                write_skill_improvement_manifest(
                    manifest,
                    manifest_path,
                    overwrite=args.force,
                )
            except BaseException:
                # Without --force both destinations were proven absent, so
                # removing the newly created config restores the original state.
                if not args.force:
                    output_path.unlink(missing_ok=True)
                raise
            print(
                json.dumps(
                    {
                        "candidates": [
                            {
                                "name": candidate["name"],
                                "version": candidate["version"],
                                "fingerprint": candidate["fingerprint"],
                                "samples": candidate["samples"],
                                "mean_return": candidate["mean_return"],
                            }
                            for candidate in manifest["candidates"]
                        ],
                        "corpus_fingerprint": manifest["corpus_fingerprint"],
                        "episode_count": manifest["episode_count"],
                        "manifest": str(manifest_path.resolve()),
                        "output_config": str(output_path.resolve()),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            return
        if args.command == "arena":
            config_path = args.config_path
            arena_config = load_config(Path(config_path))
            _validate_arena_config(arena_config, args.games)
            _validate_provider_credentials(arena_config)
            wins: dict[str, int] = {}
            fallbacks = 0
            for offset in range(args.games):
                seed = args.seed_start + offset
                game_config = replace(
                    arena_config,
                    seed=seed,
                    clear_screen=False,
                    memory_directory=None,
                    public_transcript_path=None,
                    checkpoint_path=None,
                    spectator_progress=False,
                    training_trajectory_path=args.trajectory,
                )
                result = Game(
                    game_config,
                    terminal=_ArenaTerminal(),
                ).run()
                winner = result.winner.value if result.winner is not None else "draw"
                wins[winner] = wins.get(winner, 0) + 1
                fallbacks += result.controller_fallbacks
                print(
                    f"arena {offset + 1}/{args.games}: seed={seed} "
                    f"winner={winner} days={result.days}",
                )
            print(
                json.dumps(
                    {
                        "games": args.games,
                        "seed_start": args.seed_start,
                        "wins": wins,
                        "controller_fallbacks": fallbacks,
                        "trajectory_path": args.trajectory,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            return
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
            if args.trajectory:
                config = replace(
                    config,
                    training_trajectory_path=args.trajectory,
                )
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
                        "trajectory_path": result.trajectory_path,
                        "trajectory_match_id": result.trajectory_match_id,
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
