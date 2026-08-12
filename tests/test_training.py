"""Self-play trajectory and skill-evaluation tests."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from werewolf.cli import build_parser, main
from werewolf.config import (
    GameConfig,
    PlayerConfig,
    RuleConfig,
    SkillOverrideConfig,
    config_to_dict,
    load_config,
)
from werewolf.engine import Game
from werewolf.models import ActionKind, ActionOption, ActionRequest, Role
from werewolf.skills import ROLE_SKILLS, SELFPLAY_V20_VERSION
from werewolf.training import (
    apply_skill_improvement_manifest,
    build_skill_improvement_manifest,
    load_skill_leaderboard,
    skill_fingerprint,
)


def training_config(trajectory_path) -> GameConfig:
    """Return one deterministic offline table with a versioned skill candidate."""
    roles = (
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.SEER,
        Role.WITCH,
        Role.VILLAGER,
        Role.VILLAGER,
    )
    players = []
    for index, role in enumerate(roles, start=1):
        overrides = (
            (
                SkillOverrideConfig(
                    name="role_seer",
                    version="candidate-a",
                    instructions="优先保存准确查验表，并在查杀出现时及时公开。",
                ),
            )
            if role is Role.SEER
            else ()
        )
        players.append(
            PlayerConfig(
                name=f"玩家{index}",
                controller="bot",
                fixed_role=role,
                skill_overrides=overrides,
            ),
        )
    return GameConfig(
        language="zh-CN",
        players=tuple(players),
        seed=4,
        clear_screen=False,
        memory_directory=None,
        spectator_progress=False,
        rules=RuleConfig(max_days=4, randomize_seating=False),
        training_trajectory_path=str(trajectory_path),
    )


def test_promoted_role_skills_use_the_v20_selfplay_version() -> None:
    """The evaluated five-role policy should be the built-in default."""
    promoted_roles = (
        Role.HUNTER,
        Role.SEER,
        Role.VILLAGER,
        Role.WEREWOLF,
        Role.WITCH,
    )

    assert all(
        ROLE_SKILLS[role].version == SELFPLAY_V20_VERSION for role in promoted_roles
    )
    assert "不要重复查验同一名" in ROLE_SKILLS[Role.SEER].instructions
    assert "绝不能承认狼人身份" in ROLE_SKILLS[Role.WEREWOLF].instructions
    assert "优先完成生存辩护" in ROLE_SKILLS[Role.WITCH].instructions
    assert {role: skill_fingerprint(ROLE_SKILLS[role]) for role in promoted_roles} == {
        Role.HUNTER: "6d763351a1c5935c",
        Role.SEER: "66a57322f5a8f0d5",
        Role.VILLAGER: "42d75ec3db928810",
        Role.WEREWOLF: "16e6f11182e2e5ca",
        Role.WITCH: "625d9083fc0c3682",
    }


def test_skill_overrides_round_trip_and_replace_only_the_active_skill(tmp_path) -> None:
    """Candidate metadata should survive config IO and reach the correct role."""
    trajectory = tmp_path / "trajectory.jsonl"
    config = training_config(trajectory)
    config_path = tmp_path / "game.json"
    config_path.write_text(
        json.dumps(config_to_dict(config), ensure_ascii=False),
        encoding="utf-8",
    )

    loaded = load_config(config_path)
    game = Game(loaded)
    seer = next(player for player in game.players if player.role is Role.SEER)
    villager = next(player for player in game.players if player.role is Role.VILLAGER)
    candidate = next(skill for skill in seer.skills if skill.name == "role_seer")

    assert candidate.version == "candidate-a"
    assert "准确查验表" in candidate.instructions
    assert all(skill.name != "role_seer" for skill in villager.skills)
    assert loaded.players[2].skill_overrides[0].version == "candidate-a"


def test_completed_match_exports_private_rewarded_trajectory(tmp_path) -> None:
    """A terminal match should append one reproducible owner-only episode."""
    trajectory = tmp_path / "trajectory.jsonl"
    config = training_config(trajectory)

    result = Game(config).run()

    episodes = [
        json.loads(line) for line in trajectory.read_text(encoding="utf-8").splitlines()
    ]
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["match_id"] == result.trajectory_match_id
    assert episode["game"]["seed"] == 4
    assert episode["game"]["winner"] == (
        result.winner.value if result.winner is not None else None
    )
    assert episode["steps"]
    assert [step["step_sequence"] for step in episode["steps"]] == sorted(
        step["step_sequence"] for step in episode["steps"]
    )
    stage_keys = {
        (step["stage_sequence"], step["action_index"]) for step in episode["steps"]
    }
    assert len(stage_keys) == len(episode["steps"])
    assert max(step["stage_sequence"] for step in episode["steps"]) > 1
    assert all(
        event["visibility"] in {"public", "private", "werewolf", "police", "lovers"}
        for step in episode["steps"]
        for event in step["observation"]["events"]
    )
    seer_summary = next(
        player for player in episode["players"] if player["role"] == "seer"
    )
    candidate = next(
        skill for skill in seer_summary["skills_used"] if skill["name"] == "role_seer"
    )
    assert candidate["version"] == "candidate-a"
    assert len(candidate["fingerprint"]) == 16
    assert candidate["fingerprint"] == skill_fingerprint(
        next(
            skill
            for skill in next(
                player for player in Game(config).players if player.role is Role.SEER
            ).skills
            if skill.name == "role_seer"
        ),
    )
    for player in episode["players"]:
        player_steps = [
            step
            for step in episode["steps"]
            if step["player_id"] == player["player_id"]
        ]
        terminal_rewards = [
            step["reward"]["terminal"]
            for step in player_steps
            if step["reward"]["terminal"] != 0
        ]
        if player_steps and player["outcome_reward"] != 0:
            assert terminal_rewards == [player["outcome_reward"]]
            assert player_steps[-1]["reward"]["terminal"] == player["outcome_reward"]
    assert trajectory.stat().st_mode & 0o777 == 0o600


def test_leaderboard_groups_by_skill_content_not_only_version(tmp_path) -> None:
    """Reused labels must remain separate when their prompt content differs."""
    path = tmp_path / "trajectory.jsonl"
    episodes = []
    for fingerprint, won, return_value in (("aaa", True, 1.0), ("bbb", False, -1.0)):
        episodes.append(
            {
                "schema_version": 1,
                "players": [
                    {
                        "winner": won,
                        "alive": won,
                        "actions": 3,
                        "fallbacks": 0,
                        "return": return_value,
                        "skills_used": [
                            {
                                "name": "role_seer",
                                "version": "candidate",
                                "fingerprint": fingerprint,
                            },
                        ],
                    },
                ],
            },
        )
    path.write_text(
        "".join(json.dumps(episode) + "\n" for episode in episodes),
        encoding="utf-8",
    )

    rows = load_skill_leaderboard(path, skill_name="role_seer")

    assert [row["fingerprint"] for row in rows] == ["aaa", "bbb"]
    assert rows[0]["win_rate"] == 1.0
    assert rows[1]["mean_return"] == -1.0
    assert all("ucb_score" in row for row in rows)


def test_checkpoint_restores_trajectory_without_duplicate_actions(tmp_path) -> None:
    """Replayed journal actions must not duplicate training transitions."""
    trajectory = tmp_path / "trajectory.jsonl"
    checkpoint = tmp_path / "checkpoint.json"
    config = replace(
        training_config(trajectory),
        checkpoint_path=str(checkpoint),
    )
    request = ActionRequest(
        ActionKind.VOTE,
        "测试投票",
        (ActionOption("p2", "2号 玩家2"),),
    )
    game = Game(config)
    game._save_checkpoint(next_day=1, next_step="daytime")  # noqa: SLF001
    game._act(game.players[0], request)  # noqa: SLF001
    recorder = game._trajectory_recorder  # noqa: SLF001
    assert recorder is not None
    match_id = recorder.match_id
    assert len(recorder.steps) == 1

    resumed = Game(config, resume_checkpoint=checkpoint)
    restored = resumed._trajectory_recorder  # noqa: SLF001
    assert restored is not None
    assert restored.match_id == match_id
    assert len(restored.steps) == 1

    resumed._act(resumed.players[0], request)  # noqa: SLF001
    assert len(restored.steps) == 1
    resumed._save_checkpoint(next_day=2, next_step="daytime")  # noqa: SLF001
    resumed._act(resumed.players[0], request)  # noqa: SLF001
    assert len(restored.steps) == 2
    assert {
        (step["stage_sequence"], step["action_index"]) for step in restored.steps
    } == {(1, 0), (2, 0)}


def test_arena_cli_runs_noninteractive_seed_series(tmp_path, capsys) -> None:
    """The batch command should produce one private episode per requested seed."""
    trajectory = tmp_path / "arena.jsonl"
    base = replace(training_config(trajectory), training_trajectory_path=None)
    config_path = tmp_path / "arena-config.json"
    config_path.write_text(
        json.dumps(config_to_dict(base), ensure_ascii=False),
        encoding="utf-8",
    )

    main(
        [
            "arena",
            str(config_path),
            "--games",
            "2",
            "--seed-start",
            "10",
            "--trajectory",
            str(trajectory),
        ],
    )

    output = capsys.readouterr().out
    episodes = [
        json.loads(line) for line in trajectory.read_text(encoding="utf-8").splitlines()
    ]
    assert [episode["game"]["seed"] for episode in episodes] == [10, 11]
    assert '"games": 2' in output
    assert build_parser().parse_args(["leaderboard", str(trajectory)]).command == (
        "leaderboard"
    )


def test_arena_rejects_human_controllers(tmp_path, capsys) -> None:
    """Batch self-play must fail before a hidden interactive prompt can block it."""
    trajectory = tmp_path / "arena.jsonl"
    config = training_config(trajectory)
    players = list(config.players)
    players[0] = replace(players[0], controller="human")
    config_path = tmp_path / "human.json"
    config_path.write_text(
        json.dumps(
            config_to_dict(replace(config, players=tuple(players))), ensure_ascii=False
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as captured:
        main(
            [
                "arena",
                str(config_path),
                "--trajectory",
                str(trajectory),
            ],
        )

    assert captured.value.code == 2
    assert "human controllers" in capsys.readouterr().err
    assert not trajectory.exists()


def test_reward_guided_improvement_builds_auditable_role_candidates(tmp_path) -> None:
    """Winning and losing role behavior should become bounded prompt edits."""
    trajectory = tmp_path / "selfplay.jsonl"
    config = training_config(trajectory)
    Game(config).run()

    manifest = build_skill_improvement_manifest(
        trajectory,
        skill_names=("role_werewolf", "role_seer", "role_witch", "role_villager"),
        version_prefix="test-v2",
    )
    improved = apply_skill_improvement_manifest(config, manifest)

    assert manifest["schema_version"] == 1
    assert manifest["episode_count"] == 1
    assert manifest["selection"] == "mean"
    assert len(manifest["corpus_fingerprint"]) == 16
    candidates = {item["name"]: item for item in manifest["candidates"]}
    assert set(candidates) == {
        "role_werewolf",
        "role_seer",
        "role_witch",
        "role_villager",
    }
    assert all(item["version"].startswith("test-v2-") for item in candidates.values())
    assert "私聊" not in "".join(
        evidence for item in candidates.values() for evidence in item["evidence"]
    )
    assert "自博弈强化候选" in candidates["role_werewolf"]["instructions"]
    for player in improved.players:
        overrides = {override.name: override for override in player.skill_overrides}
        assert set(candidates).issubset(overrides)
        assert overrides["role_seer"].version == candidates["role_seer"]["version"]


def test_improvement_selects_the_best_parent_and_replaces_generated_appendix(
    tmp_path,
) -> None:
    """Repeated generations should use evaluation and avoid prompt appendix growth."""
    path = tmp_path / "mixed.jsonl"
    episodes = []
    for version, fingerprint, return_value, instruction in (
        ("loser", "lose-fp", -1.0, "基线失败策略"),
        (
            "winner",
            "win-fp",
            1.0,
            "获胜策略\n\n【自博弈强化候选 old】\n- 已有可复用规则。",
        ),
    ):
        episodes.append(
            {
                "schema_version": 1,
                "match_id": version,
                "players": [
                    {
                        "player_id": f"p-{version}",
                        "winner": return_value > 0,
                        "alive": return_value > 0,
                        "return": return_value,
                        "role": "werewolf",
                        "faction": "werewolf",
                        "skills_used": [
                            {
                                "name": "role_werewolf",
                                "version": version,
                                "fingerprint": fingerprint,
                                "description": "狼人策略",
                                "instructions": instruction,
                            },
                        ],
                    },
                ],
                "steps": [],
            },
        )
    path.write_text(
        "".join(json.dumps(episode, ensure_ascii=False) + "\n" for episode in episodes),
        encoding="utf-8",
    )

    manifest = build_skill_improvement_manifest(path)
    candidate = manifest["candidates"][0]

    assert candidate["source_version"] == "winner"
    assert "已有可复用规则" in candidate["instructions"]
    assert candidate["instructions"].count("【自博弈强化候选") == 1
    assert candidate["description"].count("（自博弈强化候选）") == 1


def test_improvement_rejects_tampered_candidate_content(tmp_path) -> None:
    """A manifest fingerprint must bind the exact prompt applied at runtime."""
    trajectory = tmp_path / "selfplay.jsonl"
    config = training_config(trajectory)
    Game(config).run()
    manifest = build_skill_improvement_manifest(
        trajectory,
        skill_names=("role_seer",),
    )
    manifest["candidates"][0]["instructions"] += "篡改"

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        apply_skill_improvement_manifest(config, manifest)


def test_second_generation_uses_all_variant_evidence_for_new_failure_rules(
    tmp_path,
) -> None:
    """A winning parent should still learn from failures observed in its child variant."""
    path = tmp_path / "generations.jsonl"
    episodes = []
    for index, (version, fingerprint, return_value, text) in enumerate(
        (
            ("parent", "parent-fp", 1.0, "继续伪装，不公开身份。"),
            (
                "child",
                "child-fp",
                -1.0,
                "我确实是狼人，队友已经全部出局，预言家的查验是真的。",
            ),
        ),
        start=1,
    ):
        player_id = f"p{index}"
        episodes.append(
            {
                "schema_version": 1,
                "match_id": version,
                "players": [
                    {
                        "player_id": player_id,
                        "winner": return_value > 0,
                        "alive": False,
                        "return": return_value,
                        "role": "werewolf",
                        "faction": "werewolf",
                        "skills_used": [
                            {
                                "name": "role_werewolf",
                                "version": version,
                                "fingerprint": fingerprint,
                                "description": "狼人策略",
                                "instructions": "保持身份伪装。",
                            },
                        ],
                    },
                ],
                "steps": [
                    {
                        "player_id": player_id,
                        "observation": {"day": 3},
                        "request": {"kind": "last_words"},
                        "response": {"text": text, "attempts": 1},
                    },
                ],
            },
        )
    path.write_text(
        "".join(json.dumps(episode, ensure_ascii=False) + "\n" for episode in episodes),
        encoding="utf-8",
    )

    manifest = build_skill_improvement_manifest(path)
    candidate = manifest["candidates"][0]

    assert candidate["source_version"] == "parent"
    assert candidate["samples"] == 2
    assert "绝不能承认狼人身份" in candidate["instructions"]
    assert "wolf_self_admissions=1" in candidate["evidence"]
    assert "wolf_teammate_disclosures=1" in candidate["evidence"]


def test_improvement_learns_from_hidden_check_and_true_claim_failures(tmp_path) -> None:
    """Offline signals should catch repeated checks and unsupported power-role votes."""
    path = tmp_path / "role-failures.jsonl"
    players = []
    steps = []
    for player_id, role, skill_name, instructions in (
        ("seer", "seer", "role_seer", "记录并公开真实查验。"),
        ("witch", "witch", "role_witch", "管理两瓶药并保护身份。"),
        ("villager", "villager", "role_villager", "根据公开信息投票。"),
    ):
        players.append(
            {
                "player_id": player_id,
                "winner": False,
                "alive": False,
                "return": -1.0,
                "role": role,
                "faction": "villager",
                "skills_used": [
                    {
                        "name": skill_name,
                        "version": "failed",
                        "fingerprint": f"{role}-fp",
                        "description": f"{role}策略",
                        "instructions": instructions,
                    },
                ],
            },
        )
    steps.extend(
        [
            {
                "player_id": "seer",
                "observation": {"day": 1},
                "request": {"kind": "seer_inspect"},
                "response": {"choice": "wolf", "attempts": 1},
                "transition": {"private_result": "查验结果：狼人侧。"},
            },
            {
                "player_id": "seer",
                "observation": {"day": 1},
                "request": {"kind": "speak"},
                "response": {"text": "信息不足，暂时隐藏。", "attempts": 1},
            },
            {
                "player_id": "witch",
                "observation": {"day": 1},
                "request": {"kind": "speak"},
                "response": {"text": "昨夜我确实用了解药救人。", "attempts": 1},
            },
            {
                "player_id": "villager",
                "observation": {"day": 1},
                "request": {"kind": "vote"},
                "response": {"choice": "witch", "attempts": 1},
            },
            {
                "player_id": "witch",
                "observation": {"day": 1},
                "request": {"kind": "last_words"},
                "response": {"text": "我是女巫。", "attempts": 1},
            },
            {
                "player_id": "seer",
                "observation": {"day": 2},
                "request": {"kind": "seer_inspect"},
                "response": {"choice": "wolf", "attempts": 1},
                "transition": {"private_result": "查验结果：狼人侧。"},
            },
            {
                "player_id": "seer",
                "observation": {"day": 2},
                "request": {"kind": "last_words"},
                "response": {"text": "我是预言家，查杀是狼。", "attempts": 1},
            },
        ],
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "match_id": "role-failures",
                "players": players,
                "steps": steps,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = build_skill_improvement_manifest(path, version_prefix="test-v3")
    candidates = {item["name"]: item for item in manifest["candidates"]}

    assert "拿到狼人侧查验后" in candidates["role_seer"]["instructions"]
    assert "不要重复查验同一名" in candidates["role_seer"]["instructions"]
    assert "repeated_inspections=1" in candidates["role_seer"]["evidence"]
    assert "优先完成生存辩护" in candidates["role_witch"]["instructions"]
    assert "不能仅以“身份无法核验”" in candidates["role_villager"]["instructions"]
    assert "votes_against_true_claims=1" in candidates["role_villager"]["evidence"]


def test_improve_skills_cli_writes_private_manifest_and_loadable_config(
    tmp_path,
    capsys,
) -> None:
    """The CLI should close the trajectory-to-runtime-override loop."""
    trajectory = tmp_path / "selfplay.jsonl"
    base = training_config(trajectory)
    Game(base).run()
    capsys.readouterr()
    config_path = tmp_path / "base.json"
    config_path.write_text(
        json.dumps(config_to_dict(base), ensure_ascii=False),
        encoding="utf-8",
    )
    output_config = tmp_path / "improved.json"
    manifest_path = tmp_path / "candidates.json"

    main(
        [
            "improve-skills",
            str(trajectory),
            str(config_path),
            "--output-config",
            str(output_config),
            "--manifest",
            str(manifest_path),
            "--skill",
            "role_seer",
            "--skill",
            "role_werewolf",
        ],
    )

    loaded = load_config(output_config)
    output = json.loads(capsys.readouterr().out)
    assert output["episode_count"] == 1
    assert {item["name"] for item in output["candidates"]} == {
        "role_seer",
        "role_werewolf",
    }
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    assert output_config.stat().st_mode & 0o777 == 0o600
    assert all(len(player.skill_overrides) >= 2 for player in loaded.players)
    assert (
        build_parser()
        .parse_args(
            [
                "train-skills",
                str(trajectory),
                str(config_path),
                "--output-config",
                str(output_config),
                "--manifest",
                str(manifest_path),
            ],
        )
        .command
        == "train-skills"
    )


def test_improve_skills_force_preserves_manifest_when_config_write_fails(
    tmp_path,
    capsys,
) -> None:
    """A failed forced config replacement must not destroy prior training data."""
    trajectory = tmp_path / "selfplay.jsonl"
    base = training_config(trajectory)
    Game(base).run()
    capsys.readouterr()
    config_path = tmp_path / "base.json"
    config_path.write_text(
        json.dumps(config_to_dict(base), ensure_ascii=False),
        encoding="utf-8",
    )
    output_config = tmp_path / "existing-directory"
    output_config.mkdir()
    manifest_path = tmp_path / "candidates.json"
    original_manifest = '{"private": "keep"}\n'
    manifest_path.write_text(original_manifest, encoding="utf-8")

    with pytest.raises(SystemExit) as captured:
        main(
            [
                "improve-skills",
                str(trajectory),
                str(config_path),
                "--output-config",
                str(output_config),
                "--manifest",
                str(manifest_path),
                "--force",
            ],
        )

    assert captured.value.code == 2
    assert manifest_path.read_text(encoding="utf-8") == original_manifest
