"""Extract defined terms from governance document sections."""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class Definition:
    term: str
    definition_text: str   # full sentence/paragraph that contains the definition
    section_heading: str
    section_id: Optional[int]
    doc_id: str
    position: int          # section position in document, for ordering


# Section headings that indicate a definitions / glossary block
_GLOSSARY_HEADING_RE = re.compile(
    r"^(?:[A-Z0-9]+(?:\.[0-9]+)*\.?\s+|[A-Z]\.\s+)?"
    r"(?:definitions?(?:\s+and\s+(?:abbreviations?|terms?))?|"
    r"glossary(?:\s+of\s+terms?)?|"
    r"terms?\s+and\s+definitions?|"
    r"terms?\s+(?:and\s+)?abbreviations?|"
    r"abbreviations?\s+(?:and\s+)?definitions?)\s*$",
    re.IGNORECASE,
)

# Inline: "X means Y", "X refers to Y", "X is defined as Y", "X shall mean Y"
_INLINE_DEF_RE = re.compile(
    r'(?P<term>["\u201c\u2018]?[A-Z][A-Za-z0-9 \-/]{1,60}?["\u201d\u2019]?)'
    r'\s+(?:means?\b|refers?\s+to\b|is\s+defined\s+as\b|shall\s+mean\b)\s+'
    r'(?P<defn>[^\n]{5,})',
    re.MULTILINE,
)

# Glossary entry: "term: definition" or "term — definition" (inside glossary sections)
_GLOSSARY_ENTRY_RE = re.compile(
    r'^(?:[*\-•]\s+|\d+\.\s+)?'
    r'(?P<term>["\u201c\u2018]?\*{0,2}[A-Za-z][A-Za-z0-9 \-/()\u201c\u201d]{1,60}?\*{0,2}["\u201d\u2019]?)'
    r'\s*(?:[:—\u2014\u2013])\s+'
    r'(?P<defn>.{5,})',
    re.MULTILINE,
)


def _is_glossary_section(heading: str) -> bool:
    return bool(_GLOSSARY_HEADING_RE.match(heading.strip()))


def _clean_term(raw: str) -> str:
    return raw.strip(' "\u201c\u201d\u2018\u2019*_')


def _parse_glossary_content(content: str) -> list[tuple[str, str]]:
    """Return (term, definition_text) pairs from a glossary section body."""
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    for m in _GLOSSARY_ENTRY_RE.finditer(content):
        term = _clean_term(m.group("term"))
        defn = m.group("defn").strip()
        key = term.lower()
        if len(term) >= 2 and key not in seen:
            seen.add(key)
            results.append((term, defn))

    # Also pick up "means / refers to" patterns within glossary sections
    for m in _INLINE_DEF_RE.finditer(content):
        term = _clean_term(m.group("term"))
        defn = m.group("defn").strip()
        key = term.lower()
        if len(term) >= 2 and key not in seen:
            seen.add(key)
            results.append((term, defn))

    return results


def _parse_inline_definitions(content: str) -> list[tuple[str, str]]:
    """Return (term, definition_text) pairs from inline definitions in any section."""
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _INLINE_DEF_RE.finditer(content):
        term = _clean_term(m.group("term"))
        defn = m.group("defn").strip()
        key = term.lower()
        if len(term) >= 2 and key not in seen:
            seen.add(key)
            results.append((term, defn))
    return results


def extract_definitions(doc_id: str, sections: list) -> list[Definition]:
    """
    Extract all defined terms from a document's sections.

    sections: list of sqlite3.Row (or dict) with keys: section_id, heading, content, position
    """
    defs: list[Definition] = []
    seen_terms: set[str] = set()

    for section in sections:
        heading = section["heading"] or ""
        content = section["content"] or ""
        section_id = section["section_id"]
        position = section["position"]

        if _is_glossary_section(heading):
            pairs = _parse_glossary_content(content)
        else:
            pairs = _parse_inline_definitions(content)

        for term, defn_text in pairs:
            key = term.lower()
            if key not in seen_terms:
                seen_terms.add(key)
                defs.append(Definition(
                    term=term,
                    definition_text=defn_text,
                    section_heading=heading,
                    section_id=section_id,
                    doc_id=doc_id,
                    position=position,
                ))

    return defs
