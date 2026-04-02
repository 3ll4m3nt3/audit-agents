"""
Check whether child documents adequately address requirements from their parent documents.

Uses keyword-based relevance scoring to pre-filter child sections, then calls the Claude
API in batches to classify each requirement as covered / partially_covered / not_covered /
contradicted.
"""

import json
import re
import textwrap
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import anthropic

from .audit_cache import compute_hash, get_cached, store_cached
from .db import get_all_documents, get_hierarchy, get_sections
from .requirements_extractor import Requirement, extract_requirements

MODEL = "claude-sonnet-4-20250514"
BATCH_SIZE = 8          # requirements per Claude API call
TOP_K_SECTIONS = 3      # most-relevant child sections to include per requirement
SECTION_EXCERPT = 800   # max characters per section excerpt sent to Claude
MIN_RELEVANCE = 0.04    # minimum keyword-overlap score to include a section

_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but",
    "by", "do", "does", "did", "for", "from", "had", "has", "have",
    "he", "her", "him", "his", "how", "if", "in", "is", "it", "its",
    "me", "my", "not", "of", "on", "or", "our", "she", "so", "some",
    "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "those", "to", "too", "was", "we", "were", "what", "when",
    "where", "which", "who", "will", "with", "would", "you", "your",
    "shall", "must", "may", "also", "such", "all", "any", "each",
    "more", "most", "no", "other", "than", "well", "into", "about",
    "can", "only", "within", "per", "both", "each", "upon", "used",
})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ComplianceFinding:
    req_id: str
    requirement_text: str
    requirement_section: str
    status: str                  # covered | partially_covered | not_covered | contradicted
    evidence: str                # brief quote or reference from child document
    evidence_sections: list[str] # headings of sections searched
    explanation: str
    parent_doc_id: str
    parent_doc_title: str
    child_doc_id: str
    child_doc_title: str


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    """Return meaningful word tokens (length ≥ 4, not stop words)."""
    words = re.findall(r'\b[a-zA-Z]{4,}\b', text.lower())
    return {w for w in words if w not in _STOP_WORDS}


def _score_section(req_tokens: set[str], section_content: str) -> float:
    """Fraction of requirement tokens present in the section content."""
    if not req_tokens or not section_content:
        return 0.0
    section_tokens = _tokenize(section_content)
    return len(req_tokens & section_tokens) / len(req_tokens)


def _relevant_sections(
    requirement: Requirement,
    child_sections: list[dict],
) -> list[dict]:
    """Return the top-K child sections most relevant to this requirement."""
    req_tokens = _tokenize(requirement.text)
    scored = sorted(
        child_sections,
        key=lambda s: _score_section(req_tokens, s.get("content") or ""),
        reverse=True,
    )
    # Take top-K with at least minimum relevance; always include at least 1
    filtered = [s for s in scored[:TOP_K_SECTIONS]
                if _score_section(req_tokens, s.get("content") or "") >= MIN_RELEVANCE]
    return filtered if filtered else scored[:1]


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    return text.strip()


def _batch_assess(items: list[dict], client: anthropic.Anthropic) -> list[dict]:
    """
    Send a batch of requirements with their pre-filtered child sections to Claude.

    Each item in `items`:
        req_id, requirement, requirement_section,
        child_doc, child_sections: [{heading, content}, ...]

    Returns a list of dicts:
        req_id, status, evidence, explanation
    """
    if not items:
        return []

    prompt = textwrap.dedent(f"""
        You are a governance compliance analyst. Your task is to determine whether
        a child governance document adequately addresses each requirement from its
        parent document.

        For each requirement, you are given the most relevant sections from the
        child document (pre-filtered by keyword relevance). Classify each as:

        - "covered"           : child clearly and adequately addresses the requirement
        - "partially_covered" : child mentions the topic but lacks specificity,
                                completeness, or concrete implementation detail
        - "not_covered"       : no corresponding content found in the provided sections
        - "contradicted"      : child contains content that conflicts with the requirement

        Return ONLY a JSON array — no markdown fences, no surrounding text:
        [
          {{
            "req_id": "<req_id from input>",
            "status": "<covered|partially_covered|not_covered|contradicted>",
            "evidence": "<brief quote or section reference from child content, or empty string>",
            "explanation": "<1-2 sentences explaining the classification>"
          }},
          ...
        ]

        Requirements to assess:
        {json.dumps(items, indent=2)}
    """).strip()

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _strip_fences(response.content[0].text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Return a not_covered fallback for each item so the batch isn't silently dropped
        return [
            {
                "req_id": item["req_id"],
                "status": "not_covered",
                "evidence": "",
                "explanation": "Could not parse Claude response for this batch.",
            }
            for item in items
        ]


# ---------------------------------------------------------------------------
# Hierarchy helpers
# ---------------------------------------------------------------------------

def _build_parent_child_pairs(hierarchy_rows) -> list[tuple[str, str]]:
    """Return [(parent_id, child_id)] for every non-root relationship."""
    return [
        (row["parent_id"], row["id"])
        for row in hierarchy_rows
        if row["parent_id"]
    ]


def _sections_or_paragraphs(doc_row, conn) -> list[dict]:
    """
    Return child sections from DB, falling back to paragraph-split of raw content
    if no sections were parsed.
    """
    rows = list(get_sections(conn, doc_row["id"]))
    if rows:
        return [
            {
                "heading": row["heading"],
                "content": row["content"] or "",
                "section_id": row["section_id"],
            }
            for row in rows
        ]
    # Fallback: split raw content on blank lines
    content = doc_row["content"] or ""
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    return [
        {"heading": f"(paragraph {i + 1})", "content": p, "section_id": None}
        for i, p in enumerate(paragraphs)
    ]


# ---------------------------------------------------------------------------
# Core check for one parent-child pair
# ---------------------------------------------------------------------------

def _check_pair(
    parent_id: str,
    child_id: str,
    all_docs: dict,
    conn,
    client: anthropic.Anthropic,
    progress: Optional[Callable[[str], None]] = None,
) -> list[ComplianceFinding]:
    """Assess compliance of child_id against requirements extracted from parent_id."""
    parent_doc = all_docs.get(parent_id)
    child_doc = all_docs.get(child_id)
    if not parent_doc or not child_doc:
        return []

    parent_title = parent_doc["title"]
    child_title = child_doc["title"]

    # --- cache lookup ---
    cache_key = (
        f"compliance:{compute_hash(parent_doc['content'] or '')}:"
        f"{compute_hash(child_doc['content'] or '')}"
    )
    cached = get_cached(conn, cache_key)
    if cached is not None:
        if progress:
            progress(f"  [cached] [{child_id}] {child_title} vs [{parent_id}] {parent_title}")
        return [ComplianceFinding(**f) for f in cached]

    if progress:
        progress(f"  Checking [{child_id}] {child_title} against [{parent_id}] {parent_title}")

    # Extract requirements from parent
    parent_sections = list(get_sections(conn, parent_id))
    requirements = extract_requirements(
        parent_id, parent_sections, fallback_content=parent_doc["content"] or ""
    )
    if not requirements:
        if progress:
            progress(f"    No requirements found in parent '{parent_id}' — skipping.")
        return []

    if progress:
        progress(f"    {len(requirements)} requirement(s) extracted from parent.")

    # Prepare child sections for relevance matching
    child_sections = _sections_or_paragraphs(child_doc, conn)

    # Build batch items: one entry per requirement with pre-filtered child sections
    batch_items: list[dict] = []
    req_map: dict[str, tuple[Requirement, list[dict]]] = {}

    for req in requirements:
        relevant = _relevant_sections(req, child_sections)
        item = {
            "req_id": req.req_id,
            "requirement": req.text,
            "requirement_section": req.section_heading,
            "child_doc": child_title,
            "child_sections": [
                {
                    "heading": s["heading"],
                    "content": (s["content"] or "")[:SECTION_EXCERPT],
                }
                for s in relevant
            ],
        }
        batch_items.append(item)
        req_map[req.req_id] = (req, relevant)

    # Call Claude in batches
    findings: list[ComplianceFinding] = []

    for i in range(0, len(batch_items), BATCH_SIZE):
        batch = batch_items[i: i + BATCH_SIZE]
        results = _batch_assess(batch, client)

        for result in results:
            req_id = result.get("req_id", "")
            entry = req_map.get(req_id)
            if not entry:
                continue
            req, relevant_secs = entry

            findings.append(ComplianceFinding(
                req_id=req_id,
                requirement_text=req.text,
                requirement_section=req.section_heading,
                status=result.get("status", "not_covered"),
                evidence=result.get("evidence", ""),
                evidence_sections=[s["heading"] for s in relevant_secs],
                explanation=result.get("explanation", ""),
                parent_doc_id=parent_id,
                parent_doc_title=parent_title,
                child_doc_id=child_id,
                child_doc_title=child_title,
            ))

    store_cached(conn, cache_key, "compliance", [asdict(f) for f in findings])
    return findings


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_report(findings: list[ComplianceFinding]) -> dict:
    """Serialize findings into a JSON-ready compliance report."""
    by_status: dict[str, int] = {}
    pairs_seen: dict[tuple[str, str], int] = {}

    serialised = []
    for f in findings:
        by_status[f.status] = by_status.get(f.status, 0) + 1
        key = (f.parent_doc_id, f.child_doc_id)
        pairs_seen[key] = pairs_seen.get(key, 0) + 1
        serialised.append(asdict(f))

    pairs_summary = [
        {
            "parent_doc_id": pid,
            "child_doc_id": cid,
            "requirements_checked": count,
        }
        for (pid, cid), count in pairs_seen.items()
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "check_type": "compliance",
        "pairs_checked": pairs_summary,
        "findings": serialised,
        "summary": {
            "total_requirements_checked": len(findings),
            "by_status": by_status,
        },
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run_check_pair(
    conn,
    parent_id: str,
    child_id: str,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Check compliance for a single parent-child document pair."""
    all_docs = {row["id"]: dict(row) for row in get_all_documents(conn)}
    client = anthropic.Anthropic()
    findings = _check_pair(parent_id, child_id, all_docs, conn, client, progress=progress)
    return _build_report(findings)


def run_check_all(
    conn,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Check compliance for every parent-child pair in the hierarchy."""
    all_docs = {row["id"]: dict(row) for row in get_all_documents(conn)}
    hierarchy_rows = get_hierarchy(conn)
    pairs = _build_parent_child_pairs(hierarchy_rows)
    client = anthropic.Anthropic()

    all_findings: list[ComplianceFinding] = []
    for parent_id, child_id in pairs:
        all_findings.extend(
            _check_pair(parent_id, child_id, all_docs, conn, client, progress=progress)
        )

    return _build_report(all_findings)
