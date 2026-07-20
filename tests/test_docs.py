"""Documentation and publishable case-study integrity tests."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]

COMPLETED_CASES = {
    "16_llm_2026-07-20_public_transcript.txt": "狼人阵营获胜",
    "movie_crazy_fox_20260721_public_transcript.txt": "好人阵营获胜",
    "movie_lovers_20260723_public_transcript.txt": "狼人阵营获胜",
    "movie_mad_land_20260724_public_transcript.txt": "狼人阵营获胜",
    "movie_prison_break_20260722_public_transcript.txt": "好人阵营获胜",
}


def test_completed_case_studies_are_public_and_referenced() -> None:
    """Every documented game must have a complete, privacy-safe transcript."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    case_directory = ROOT / "examples" / "case_studies"

    for filename, outcome in COMPLETED_CASES.items():
        transcript = (case_directory / filename).read_text(encoding="utf-8")
        assert outcome in transcript
        assert "全部身份：" in transcript
        assert "OPENAI_API_KEY" not in transcript
        assert "Authorization" not in transcript
        assert "狼人队友名单" not in transcript
        assert "私密策略" not in transcript
        assert "控制器调用失败" not in transcript
        assert "本地后备" not in transcript
        assert "系统安全后备" not in transcript
        assert f"examples/case_studies/{filename}" in readme
        if filename.startswith("movie_"):
            assert "[观战" not in transcript


def test_readme_local_links_exist() -> None:
    """Keep the reorganized README free from stale repository-local links."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    local_targets = re.findall(
        r"\]\(((?:docs|examples)/[^)#]+)(?:#[^)]+)?\)",
        readme,
    )

    assert local_targets
    for target in local_targets:
        assert (ROOT / target).exists(), target
