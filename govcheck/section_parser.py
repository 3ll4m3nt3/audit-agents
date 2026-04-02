"""Parse documents into logical sections based on heading patterns."""
import re
from typing import NamedTuple

HINT_NUMBERED = "numbered"
HINT_MARKDOWN = "markdown"
HINT_CAPS = "caps"
HINT_AUTO = "auto"

_NUMBERED_RE = re.compile(r'^([A-Z]?\d+(?:\.\d+)*)\s+(.+)')
_MARKDOWN_RE = re.compile(r'^(#{1,6})\s+(.*)')
_CAPS_RE = re.compile(r'^[A-Z][A-Z0-9 \t,.\-:/]{3,}$')


class Section(NamedTuple):
    heading: str
    level: int
    content: str
    position: int


def parse_sections(content: str, hint: str = HINT_AUTO) -> list[Section]:
    """Split document content into sections. Returns [] if no headings detected."""
    if not content or not content.strip():
        return []

    resolved = _detect_hint(content) if hint == HINT_AUTO else hint

    if resolved == HINT_NUMBERED:
        return _parse_numbered(content)
    elif resolved == HINT_MARKDOWN:
        return _parse_markdown(content)
    elif resolved == HINT_CAPS:
        return _parse_caps(content)
    return []


def _detect_hint(content: str) -> str:
    lines = content.splitlines()
    numbered = sum(1 for l in lines if _NUMBERED_RE.match(l.strip()))
    markdown = sum(1 for l in lines if _MARKDOWN_RE.match(l))
    caps = sum(1 for l in lines if l.strip() and _CAPS_RE.match(l.strip()))

    best = max(numbered, markdown, caps)
    if best == 0:
        return HINT_NUMBERED  # will produce no sections
    if numbered == best:
        return HINT_NUMBERED
    if markdown == best:
        return HINT_MARKDOWN
    return HINT_CAPS


def _parse_with_pattern(content: str, is_heading, get_heading_info) -> list[Section]:
    lines = content.splitlines()
    sections: list[Section] = []
    current_heading: str | None = None
    current_level = 1
    current_lines: list[str] = []
    position = 0

    for line in lines:
        match = is_heading(line)
        if match:
            # flush previous section
            if current_heading is not None:
                sections.append(Section(
                    heading=current_heading,
                    level=current_level,
                    content="\n".join(current_lines).strip(),
                    position=position,
                ))
                position += 1
            current_heading, current_level = get_heading_info(match)
            current_lines = []
        else:
            current_lines.append(line)

    if current_heading is not None:
        sections.append(Section(
            heading=current_heading,
            level=current_level,
            content="\n".join(current_lines).strip(),
            position=position,
        ))

    return sections


def _parse_numbered(content: str) -> list[Section]:
    def is_heading(line: str):
        return _NUMBERED_RE.match(line.strip())

    def get_info(match) -> tuple[str, int]:
        number = match.group(1)
        title = match.group(2).strip()
        level = number.count('.') + 1
        return f"{number} {title}", level

    return _parse_with_pattern(content, is_heading, get_info)


def _parse_markdown(content: str) -> list[Section]:
    def is_heading(line: str):
        return _MARKDOWN_RE.match(line)

    def get_info(match) -> tuple[str, int]:
        level = len(match.group(1))
        title = match.group(2).strip()
        return title, level

    return _parse_with_pattern(content, is_heading, get_info)


def _parse_caps(content: str) -> list[Section]:
    def is_heading(line: str):
        stripped = line.strip()
        return _CAPS_RE.match(stripped) if stripped else None

    def get_info(match) -> tuple[str, int]:
        return match.group(0).strip(), 1

    return _parse_with_pattern(content, is_heading, get_info)
