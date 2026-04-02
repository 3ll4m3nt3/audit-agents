"""Extract defined terms from governance document sections.

Three extraction modes are supported:

- ``glossary``  (default) — Detect centralised glossary headings and parse
  structured entries (colon/dash/table rows, inline definition verbs).  For
  non-glossary sections the strict inline patterns (_INLINE_DEF_RE,
  _SCOPED_DEF_RE) are also applied so scoped formal definitions are captured
  wherever they appear.  This is the original behaviour.

- ``inline`` — Apply an extended set of loose patterns to *every* section
  regardless of heading.  Catches distributed definitions that never appear in
  a dedicated glossary: parenthetical glosses, "hereafter referred to as",
  emphasis-first entries (**Term**: ...), and "also known as" aliases.

- ``semantic`` — Send section text to the Claude API and ask it to identify
  terms that are contextually defined without explicit linguistic markers.
  Requires an ``anthropic.Anthropic`` client to be passed as ``client=``.
  Falls back silently (returns an empty list) when no client is provided.

Modes can be combined by passing a list, e.g.
``mode=[ExtractionMode.GLOSSARY, ExtractionMode.SEMANTIC]``.  Results are
merged and de-duplicated by (term_lower, section_id).
"""

import json
import re
import textwrap
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------

@dataclass
class Definition:
    term: str
    definition_text: str    # full sentence/paragraph containing the definition
    section_heading: str
    section_id: Optional[int]
    doc_id: str
    position: int           # section position in document, for ordering
    extraction_mode: str = field(default="glossary")  # which mode produced this


class ExtractionMode(str, Enum):
    """Controls how ``extract_definitions`` searches for defined terms."""
    GLOSSARY = "glossary"
    INLINE   = "inline"
    SEMANTIC = "semantic"


# ---------------------------------------------------------------------------
# Section heading patterns
# ---------------------------------------------------------------------------

_GLOSSARY_HEADING_RE = re.compile(
    r"^(?:[A-Z0-9]+(?:\.[0-9]+)*\.?\s+|[A-Z]\.\s+)?"
    r"(?:definitions?\s*(?:and\s+(?:abbreviations?|terms?))?(?:\s+used\s+in\s+this\s+\w+)?|"
    r"key\s+(?:definitions?|terms?)|"
    r"glossary(?:\s+of\s+(?:key\s+)?terms?)?|"
    r"terms?\s+and\s+definitions?|"
    r"terms?\s+(?:and\s+)?abbreviations?|"
    r"abbreviations?\s+(?:and\s+)?(?:definitions?|terms?|acronyms?)|"
    r"defined\s+terms?|"
    r"terminology|"
    r"nomenclature|"
    r"interpretations?)\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Shared inline patterns (used in GLOSSARY and INLINE modes)
# ---------------------------------------------------------------------------

_INLINE_VERBS_PAT = (
    r"means?\b|"
    r"refers?\s+to\b|"
    r"is\s+defined\s+(?:as|to\s+mean)\b|"
    r"shall\s+(?:mean|be\s+(?:construed|interpreted)\s+as)\b|"
    r"is\s+(?:understood|taken)\s+to\s+mean\b|"
    r"has\s+the\s+meaning\s+of\b|"
    r"is\s+used\s+to\s+(?:mean|describe|refer\s+to)\b"
)

# Core inline: "Term means/refers to/is defined as ..."
_INLINE_DEF_RE = re.compile(
    r'(?P<term>["\u201c\u2018]?[A-Z][A-Za-z0-9 \-/]{1,60}?["\u201d\u2019]?)'
    r"\s+(?:" + _INLINE_VERBS_PAT + r")\s+"
    r"(?P<defn>[^\n]{5,})",
    re.MULTILINE,
)

# Scoped: "For the purposes of this [document/section/policy], Term means ..."
_SCOPED_DEF_RE = re.compile(
    r"[Ff]or\s+(?:the\s+)?purposes?\s+of\s+this\s+\w+[^,\n]*,\s+"
    r'(?P<term>["\u201c\u2018]?[A-Z][A-Za-z0-9 \-/]{1,60}?["\u201d\u2019]?)'
    r"\s+(?:" + _INLINE_VERBS_PAT + r")\s+"
    r"(?P<defn>[^\n]{5,})",
    re.MULTILINE,
)

# Glossary entry: "term: definition" or "term — definition" (structured lists)
_GLOSSARY_ENTRY_RE = re.compile(
    r"^(?:[*\-•]\s+|\d+\.\s+)?"
    r'(?P<term>["\u201c\u2018]?\*{0,2}[A-Za-z][A-Za-z0-9 \-/()\u201c\u201d]{1,60}?\*{0,2}["\u201d\u2019]?)'
    r"\s*(?:[:—\u2014\u2013])\s+"
    r"(?P<defn>.{5,})",
    re.MULTILINE,
)

# Markdown table rows: | Term | Definition |
_MD_TABLE_ROW_RE = re.compile(
    r"^\|\s*(?P<term>[A-Za-z][A-Za-z0-9 \-/]{0,59}?)\s*\|\s*(?P<defn>[^|\n]{5,}?)\s*\|",
    re.MULTILINE,
)

_TABLE_HEADER_WORDS = frozenset({
    "term", "terms", "terminology", "name", "concept", "phrase", "word",
    "abbreviation", "acronym", "definition", "description", "meaning", "explanation",
})


# ---------------------------------------------------------------------------
# Extended patterns for INLINE mode
# ---------------------------------------------------------------------------

# Parenthetical gloss: "Term (meaning X)" / "Term (i.e., X)" / "Term (that is, X)"
_PAREN_DEF_RE = re.compile(
    r'(?P<term>["\u201c]?[A-Z][A-Za-z0-9 \-/]{1,60}?["\u201d]?)'
    r"\s*\((?:meaning|i\.e\.,?|that is,?|also\s+(?:called|known\s+as))\s+"
    r"(?P<defn>[^)]{5,})\)",
    re.MULTILINE,
)

# Hereafter: "(hereafter referred to as 'Term')" or '(the "Term")'
# Captures what a previously-described thing is *named*, treating the name as
# the term and the surrounding sentence as the definition text.
_HEREAFTER_RE = re.compile(
    r'(?P<context>[^.!?\n]{10,}?)'
    r'\s*\(\s*(?:hereafter\s+)?(?:referred\s+to\s+as\s+)?(?:the\s+)?'
    r'["\u201c\u2018](?P<term>[A-Za-z][A-Za-z0-9 \-/]{1,60})["\u201d\u2019]\s*\)',
    re.MULTILINE | re.IGNORECASE,
)

# Emphasis-first entry at line start: **Term**: definition  or  *Term*: definition
_EMPHASIS_ENTRY_RE = re.compile(
    r"^[\*_]{1,2}(?P<term>[A-Za-z][A-Za-z0-9 \-/]{1,60}?)[\*_]{1,2}"
    r"\s*[:—\u2014]\s+"
    r"(?P<defn>[^\n]{5,})",
    re.MULTILINE,
)

# "Also known as" / "commonly known as" — captures the alias as the canonical term
# and the preceding noun phrase as the definition context.
_AKA_RE = re.compile(
    r'(?P<defn>[A-Z][A-Za-z0-9 ,\-/]{5,80}?)'
    r"\s*[,(]\s*(?:also|commonly|sometimes|otherwise)\s+(?:known|referred\s+to)\s+as\s+"
    r'["\u201c\u2018]?(?P<term>[A-Za-z][A-Za-z0-9 \-/]{1,60})["\u201d\u2019]?[,)]?',
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Semantic mode — Claude API prompt
# ---------------------------------------------------------------------------

_SEMANTIC_PROMPT = textwrap.dedent("""
    You are a governance document analyst. Your task is to extract terms that are
    being *defined* (explicitly or implicitly) in the text below.

    Include:
    - Terms with explicit linguistic markers ("means", "refers to", "is defined as", etc.)
    - Terms explained via parenthetical descriptions or appositive clauses
    - Technical or domain terms whose meaning is established contextually in the text,
      even without a formal "X means Y" structure

    Exclude:
    - Terms that are merely *used* but not defined here
    - Headings, section numbers, or document references
    - Terms shorter than 2 characters

    Return ONLY a JSON array — no markdown fences, no surrounding text:
    [
      {
        "term": "<the defined term>",
        "definition_text": "<the sentence or phrase that establishes its meaning>",
        "confidence": "<high|medium|low>"
      },
      ...
    ]

    If no definitions are found, return an empty array [].

    Text to analyse:
    ---
    {text}
    ---
""").strip()

_SEMANTIC_MIN_CONFIDENCE = {"high", "medium"}   # "low" hits are discarded
_SEMANTIC_SECTION_BATCH  = 4                     # sections per API call
_SEMANTIC_MAX_CHARS      = 6000                  # truncate very long sections


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_glossary_section(heading: str) -> bool:
    return bool(_GLOSSARY_HEADING_RE.match(heading.strip()))


def _clean_term(raw: str) -> str:
    return raw.strip(' "\u201c\u201d\u2018\u2019*_')


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    return text.strip()


# ---------------------------------------------------------------------------
# GLOSSARY mode parsers
# ---------------------------------------------------------------------------

def _parse_glossary_content(content: str) -> list[tuple[str, str]]:
    """Return (term, definition_text) pairs from a structured glossary section."""
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(term: str, defn: str) -> None:
        term = _clean_term(term)
        key = term.lower()
        if len(term) >= 2 and key not in seen:
            seen.add(key)
            results.append((term, defn.strip()))

    for m in _MD_TABLE_ROW_RE.finditer(content):
        raw_term = m.group("term").strip()
        if re.match(r"^[\-:\s]+$", raw_term):
            continue
        if raw_term.lower() in _TABLE_HEADER_WORDS:
            continue
        _add(raw_term, m.group("defn"))

    for m in _GLOSSARY_ENTRY_RE.finditer(content):
        _add(m.group("term"), m.group("defn"))

    for m in _INLINE_DEF_RE.finditer(content):
        _add(m.group("term"), m.group("defn"))

    return results


def _parse_inline_definitions(content: str) -> list[tuple[str, str]]:
    """Return (term, definition_text) pairs using strict inline patterns."""
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(term: str, defn: str) -> None:
        term = _clean_term(term)
        key = term.lower()
        if len(term) >= 2 and key not in seen:
            seen.add(key)
            results.append((term, defn.strip()))

    for m in _INLINE_DEF_RE.finditer(content):
        _add(m.group("term"), m.group("defn"))

    for m in _SCOPED_DEF_RE.finditer(content):
        _add(m.group("term"), m.group("defn"))

    return results


# ---------------------------------------------------------------------------
# INLINE mode parser — extended loose patterns applied to all sections
# ---------------------------------------------------------------------------

def _parse_inline_loose(content: str) -> list[tuple[str, str]]:
    """
    Return (term, definition_text) pairs using all strict *and* loose patterns.

    Applies _INLINE_DEF_RE, _SCOPED_DEF_RE, _PAREN_DEF_RE, _HEREAFTER_RE,
    _EMPHASIS_ENTRY_RE, and _AKA_RE.  Intended for use on every section,
    regardless of whether its heading signals a glossary.
    """
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(term: str, defn: str) -> None:
        term = _clean_term(term)
        key = term.lower()
        if len(term) >= 2 and key not in seen:
            seen.add(key)
            results.append((term, defn.strip()))

    # Strict patterns
    for m in _INLINE_DEF_RE.finditer(content):
        _add(m.group("term"), m.group("defn"))

    for m in _SCOPED_DEF_RE.finditer(content):
        _add(m.group("term"), m.group("defn"))

    # Extended loose patterns
    for m in _EMPHASIS_ENTRY_RE.finditer(content):
        _add(m.group("term"), m.group("defn"))

    for m in _PAREN_DEF_RE.finditer(content):
        _add(m.group("term"), m.group("defn"))

    for m in _HEREAFTER_RE.finditer(content):
        # The "term" is the name introduced; the context clause is the definition
        _add(m.group("term"), m.group("context").strip())

    for m in _AKA_RE.finditer(content):
        # Treat the alias as the canonical term; preceding phrase is the definition
        _add(m.group("term"), m.group("defn").strip())

    return results


# ---------------------------------------------------------------------------
# SEMANTIC mode — Claude API extraction
# ---------------------------------------------------------------------------

def _parse_semantic_definitions(
    sections: list,
    client,
) -> list[tuple[str, str, str, Optional[int], int]]:
    """
    Use the Claude API to extract contextually-defined terms from section text.

    Returns a list of (term, defn_text, heading, section_id, position) tuples.
    Requires a live ``anthropic.Anthropic`` client.  Returns an empty list if
    ``client`` is None.
    """
    if client is None:
        return []

    results: list[tuple[str, str, str, Optional[int], int]] = []
    seen: set[str] = set()

    # Process sections in batches to reduce API round-trips
    batches: list[list] = [
        sections[i : i + _SEMANTIC_SECTION_BATCH]
        for i in range(0, len(sections), _SEMANTIC_SECTION_BATCH)
    ]

    for batch in batches:
        # Build a combined text block with section markers so we can map results
        # back to individual sections.  We keep a lookup by term (case-insensitive)
        # to assign heading / section_id / position from the first matching section.
        section_lookup: dict[str, tuple[str, Optional[int], int]] = {}
        text_parts: list[str] = []

        for sec in batch:
            heading  = sec["heading"] or ""
            content  = (sec["content"] or "")[:_SEMANTIC_MAX_CHARS]
            sec_id   = sec["section_id"]
            position = sec["position"]

            header_line = f"[Section: {heading}]" if heading else "[Section]"
            text_parts.append(f"{header_line}\n{content}")

            # Register this section for all words in its content (rough heuristic;
            # the first section wins for any given term)
            for word in re.findall(r"[A-Za-z][A-Za-z0-9 \-/]{1,60}", content):
                key = word.strip().lower()
                if key not in section_lookup:
                    section_lookup[key] = (heading, sec_id, position)

        combined_text = "\n\n".join(text_parts)
        prompt = _SEMANTIC_PROMPT.format(text=combined_text)

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = _strip_fences(response.content[0].text)
            items = json.loads(raw)
        except Exception:
            continue

        for item in items:
            confidence = item.get("confidence", "low")
            if confidence not in _SEMANTIC_MIN_CONFIDENCE:
                continue
            term = _clean_term(str(item.get("term", "")))
            defn = str(item.get("definition_text", "")).strip()
            if len(term) < 2 or not defn:
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)

            # Look up the source section using the first word of the term
            heading, sec_id, position = section_lookup.get(
                key, (batch[0]["heading"] or "", batch[0]["section_id"], batch[0]["position"])
            )
            results.append((term, defn, heading, sec_id, position))

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_definitions(
    doc_id: str,
    sections: list,
    *,
    mode: "ExtractionMode | list[ExtractionMode]" = ExtractionMode.GLOSSARY,
    deduplicate: bool = False,
    client=None,
) -> list[Definition]:
    """
    Extract all defined terms from a document's sections.

    Parameters
    ----------
    doc_id:
        Identifier of the document being processed.
    sections:
        List of sqlite3.Row (or dict) with keys:
        ``section_id``, ``heading``, ``content``, ``position``.
    mode:
        One ``ExtractionMode`` value or a list of them.

        - ``ExtractionMode.GLOSSARY`` (default) — detect glossary headings and
          apply structured parsing; fall back to strict inline patterns for
          other sections.  Original behaviour.
        - ``ExtractionMode.INLINE`` — apply extended loose patterns to every
          section regardless of heading.
        - ``ExtractionMode.SEMANTIC`` — use the Claude API to surface
          contextually-defined terms.  Requires ``client``.

        When a list is supplied all requested modes run and their results are
        merged, de-duplicated by (term_lower, section_id).

    deduplicate:
        When ``True``, only the first occurrence of each term is kept across the
        entire document (original behaviour).  The default (``False``) preserves
        every occurrence so that terms defined across multiple sections are all
        captured.

    client:
        An ``anthropic.Anthropic`` instance.  Required only for
        ``ExtractionMode.SEMANTIC``; ignored for other modes.

    Returns
    -------
    list[Definition]
        Ordered by (position, term).
    """
    modes: list[ExtractionMode] = (
        mode if isinstance(mode, list) else [mode]
    )

    all_defs: list[Definition] = []
    # De-dup key: (term_lower, section_id) to avoid duplicates across modes
    seen_multi: set[tuple[str, Optional[int]]] = set()

    for active_mode in modes:
        mode_defs = _extract_for_mode(doc_id, sections, active_mode, client)

        seen_global: set[str] = set()   # per-mode, only when deduplicate=True

        for d in mode_defs:
            multi_key = (d.term.lower(), d.section_id)
            if multi_key in seen_multi:
                continue

            if deduplicate and d.term.lower() in seen_global:
                continue
            if deduplicate:
                seen_global.add(d.term.lower())

            seen_multi.add(multi_key)
            all_defs.append(d)

    return sorted(all_defs, key=lambda d: (d.position, d.term.lower()))


def _extract_for_mode(
    doc_id: str,
    sections: list,
    mode: ExtractionMode,
    client,
) -> list[Definition]:
    """Run a single extraction mode and return raw (possibly duplicate) Definition list."""

    if mode is ExtractionMode.SEMANTIC:
        raw_tuples = _parse_semantic_definitions(sections, client)
        return [
            Definition(
                term=term,
                definition_text=defn,
                section_heading=heading,
                section_id=sec_id,
                doc_id=doc_id,
                position=position,
                extraction_mode="semantic",
            )
            for term, defn, heading, sec_id, position in raw_tuples
        ]

    defs: list[Definition] = []

    for section in sections:
        heading   = section["heading"] or ""
        content   = section["content"] or ""
        section_id = section["section_id"]
        position  = section["position"]

        if mode is ExtractionMode.GLOSSARY:
            if _is_glossary_section(heading):
                pairs = _parse_glossary_content(content)
            else:
                pairs = _parse_inline_definitions(content)
        else:  # INLINE
            pairs = _parse_inline_loose(content)

        seen_section: set[str] = set()
        for term, defn_text in pairs:
            key = term.lower()
            if key in seen_section:
                continue
            seen_section.add(key)
            defs.append(Definition(
                term=term,
                definition_text=defn_text,
                section_heading=heading,
                section_id=section_id,
                doc_id=doc_id,
                position=position,
                extraction_mode=mode.value,
            ))

    return defs