"""Safe terminal text normalization and line framing."""

from __future__ import annotations

import re

MAX_PLAYER_NAME_CHARS = 80
MAX_PLAYER_NAME_BYTES = 240
MAX_RENDERED_TEXT_CHARS = 8000

_OSC_SEQUENCE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_CSI_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC_SEQUENCE = re.compile(r"\x1b(?:[@-_]|.)", re.DOTALL)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def contains_terminal_control(text: str) -> bool:
    """Return whether text contains terminal controls or unsafe whitespace."""
    return bool(
        _OSC_SEQUENCE.search(text)
        or _CSI_SEQUENCE.search(text)
        or _ESC_SEQUENCE.search(text)
        or _CONTROL_CHARACTERS.search(text)
        or "\n" in text
        or "\r" in text
        or "\t" in text
    )


def sanitize_rendered_text(
    text: object,
    *,
    limit: int = MAX_RENDERED_TEXT_CHARS,
) -> str:
    """Remove terminal controls while retaining ordinary line breaks."""
    rendered = str(text).replace("\r\n", "\n").replace("\r", "\n")
    rendered = rendered.replace("\t", "    ")
    rendered = _OSC_SEQUENCE.sub("", rendered)
    rendered = _CSI_SEQUENCE.sub("", rendered)
    rendered = _ESC_SEQUENCE.sub("", rendered)
    rendered = _CONTROL_CHARACTERS.sub("", rendered)
    if len(rendered) > limit:
        rendered = rendered[: limit - 1] + "…"
    return rendered


def frame_rendered_lines(label: object, text: object) -> str:
    """Render one labeled block while marking every authenticated continuation."""
    safe_label = sanitize_rendered_text(label, limit=160).replace("\n", " ").strip()
    safe_label = safe_label or "output"
    safe_text = sanitize_rendered_text(text)
    first_line, *continuation_lines = safe_text.split("\n")
    rendered_lines = [f"[{safe_label}] {first_line}"]
    # Keep continuation lines inside a visible gutter. This avoids repeating the
    # speaker label while ensuring player-controlled text cannot forge a new
    # top-level judge, spectator, or player message after a newline.
    rendered_lines.extend(f"  │ {line}" for line in continuation_lines)
    return "\n".join(rendered_lines)
