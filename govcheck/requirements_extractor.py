"""Extract requirement statements from governance document sections."""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class Requirement:
    req_id: str
    text: str
    section_heading: str
    section_id: Optional[int]
    doc_id: str
    position: int   # sequential position within the document


# Matches obligation keywords that signal a normative requirement.
# Covers normative (shall/must) and recommended (should/will) modals
# used across ISO, GDPR, NIST, DAMA, and general policy frameworks.
_REQUIREMENT_RE = re.compile(
    r'\b('
    r'shall(?:\s+not)?'
    r'|must(?:\s+not)?'
    r'|should(?:\s+not)?'
    r'|will(?:\s+not)?'
    r'|(?:is|are)\s+required\s+to'
    r'|(?:is|are)\s+prohibited\s+from'
    r')\b',
    re.IGNORECASE,
)

# Control/article identifiers at the start of a line. Supports:
#   ISO:   A.5.1, 8.2.3
#   GDPR:  Article 5, Article 5(1)(a), Recital 39
#   NIST:  AC-1, AU-2, PM-31
#   DAMA / custom frameworks: DG-01, DM-12, Chapter 3, Section 4, Clause 6, Annex A
_CONTROL_PREFIX_RE = re.compile(
    r'^(?:'
    r'[A-Z]?\d+(?:\.\d+){1,3}'                             # ISO: A.5.1 / 8.2.3
    r'|(?:Article|Recital|Section|Chapter|Clause|Annex)\s+\d+(?:\(\w+\))*'  # GDPR/legal
    r'|[A-Z]{2,6}-\d+'                                      # NIST/alphanumeric: AC-1, DG-01
    r')\s+',
    re.IGNORECASE,
)


def extract_requirements(
    doc_id: str,
    sections: list,
    fallback_content: str = "",
) -> list[Requirement]:
    """
    Extract normative requirement statements from document sections.

    sections: list of sqlite3.Row or dict with keys: heading, content, section_id, position
    fallback_content: raw document text used when sections is empty
    """
    if sections:
        source_chunks = [
            {
                "heading": row["heading"],
                "content": row["content"] or "",
                "section_id": row["section_id"],
            }
            for row in sections
        ]
    elif fallback_content:
        source_chunks = [
            {"heading": "(document)", "content": fallback_content, "section_id": None}
        ]
    else:
        return []

    requirements: list[Requirement] = []
    req_pos = 0

    for chunk in source_chunks:
        heading = chunk["heading"]
        content = chunk["content"]
        section_id = chunk["section_id"]

        if not content:
            continue

        for sentence in _split_to_sentences(content):
            sentence = sentence.strip()
            # Skip very short fragments and pure headings
            if len(sentence) < 15:
                continue
            if _REQUIREMENT_RE.search(sentence) or _is_control_statement(sentence):
                requirements.append(Requirement(
                    req_id=f"{doc_id}__r{req_pos}",
                    text=sentence,
                    section_heading=heading,
                    section_id=section_id,
                    doc_id=doc_id,
                    position=req_pos,
                ))
                req_pos += 1

    return requirements


def _is_control_statement(sentence: str) -> bool:
    """
    Return True for lines that look like a framework control or article heading
    with significant content (not a bare section title).

    Matches ISO (A.5.1), GDPR (Article 5(1)(a)), NIST (AC-1), DAMA (DG-01),
    and legal/policy structures (Chapter 3, Clause 6, Annex A).
    """
    return bool(
        _CONTROL_PREFIX_RE.match(sentence)
        and len(sentence) > 30
        and not sentence.endswith(":")
    )


def _split_to_sentences(text: str) -> list[str]:
    """
    Split text into sentence-like fragments for requirement extraction.
    Handles prose, numbered lists, and bullet points.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Split on blank lines first (paragraph boundaries)
    paragraphs = re.split(r"\n{2,}", text)

    sentences: list[str] = []
    for para in paragraphs:
        # Collapse single newlines to spaces within a paragraph so that
        # multi-line sentences are not fragmented (e.g. "access rights\nshall be…")
        para = re.sub(r"\n(?=[a-z])", " ", para)

        # Split list items on newlines that start a new item marker
        list_items = re.split(r"\n(?=[-•*]|\d+\.)", para)

        for item in list_items:
            # Within each item, split on sentence-ending punctuation
            parts = re.split(r"(?<=[.;!?])\s+", item.replace("\n", " "))
            sentences.extend(parts)

    return sentences
