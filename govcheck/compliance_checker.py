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
from pathlib import Path
from typing import Callable, Optional

import anthropic

from .audit_cache import compute_hash, get_cached, store_cached
from .db import get_all_documents, get_hierarchy, get_sections
from .hierarchy import load_hierarchy, build_node_map, get_immutable_docs, get_mutable_docs, get_siblings
from .requirements_extractor import Requirement, extract_requirements

MODEL = "claude-sonnet-4-20250514"
BATCH_SIZE = 8          # requirements per Claude API call
TOP_K_SECTIONS = 3      # most-relevant child sections to include per requirement
SECTION_EXCERPT = 800   # max characters per section excerpt sent to Claude
MIN_RELEVANCE = 0.04    # minimum keyword-overlap score to include a section
DOC_RELEVANCE_MIN = 0.06  # minimum max-section score for a doc to be "responsible" for a req

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
    status: str                  # covered | partially_covered | not_covered | contradicted | no_policy
    evidence: str                # brief quote or reference from target document
    evidence_sections: list[str] # headings of sections searched
    explanation: str
    source_doc_id: str           # source of requirements (immutable or sibling)
    source_doc_title: str
    target_doc_id: str           # document being checked (mutable); "" for no_policy gaps
    target_doc_title: str
    check_type: str = "conformance"  # "conformance" or "sibling_consistency"
    gap_type: str = ""           # "" | "not_covered_by_relevant_policy" | "no_relevant_policy"


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


def _doc_max_relevance(requirement: Requirement, sections: list[dict]) -> float:
    """
    Return the maximum keyword-overlap score across all sections of a document.
    Used to decide whether a document is topically responsible for a requirement.
    """
    req_tokens = _tokenize(requirement.text)
    if not req_tokens or not sections:
        return 0.0
    return max(
        (_score_section(req_tokens, s.get("content") or "") for s in sections),
        default=0.0,
    )


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    return text.strip()


def _batch_assess(items: list[dict], client: anthropic.Anthropic, check_type: str = "conformance") -> list[dict]:
    """
    Send a batch of requirements with their pre-filtered target-document sections to Claude.

    Each item in `items`:
        req_id, requirement, requirement_section,
        target_doc, target_sections: [{heading, content}, ...]

    Returns a list of dicts:
        req_id, status, evidence, explanation
    
    check_type: "conformance" or "sibling_consistency"
    """
    if not items:
        return []

    if check_type == "conformance":
        task_description = "determine whether a target document adequately addresses each requirement from a source (immutable reference) document"
        status_covered = "clearly and adequately addresses the requirement"
        status_partially = "mentions the topic but lacks specificity, completeness, or concrete implementation detail"
        status_not = "no corresponding content found in the provided sections"
        status_contradicted = "contains content that conflicts with the requirement"
    else:  # sibling_consistency
        task_description = "compare two sibling documents and assess whether they are consistent with each other on each topic/requirement"
        status_covered = "both documents consistently address the topic with compatible approaches"
        status_partially = "documents mention the topic but with some discrepancies or inconsistencies"
        status_not = "the second document does not address this topic found in the first"
        status_contradicted = "the documents contain contradictory statements on this topic"

    prompt = textwrap.dedent(f"""
        You are a governance compliance analyst. Your task is to {task_description}.

        For each requirement/topic, you are given the most relevant sections from the
        target document (pre-filtered by keyword relevance). Classify each as:

        - "covered"           : {status_covered}
        - "partially_covered" : {status_partially}
        - "not_covered"       : {status_not}
        - "contradicted"      : {status_contradicted}

        Return ONLY a JSON array — no markdown fences, no surrounding text:
        [
          {{
            "req_id": "<req_id from input>",
            "status": "<covered|partially_covered|not_covered|contradicted>",
            "evidence": "<brief quote or section reference from target content, or empty string>",
            "explanation": "<1-2 sentences explaining the classification>"
          }},
          ...
        ]

        Requirements/Topics to assess:
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

def _build_conformance_and_sibling_pairs(node_map: dict[str, dict]) -> list[tuple[str, str, str]]:
    """
    Build pairs of documents to check: (source_id, target_id, check_type)
    - check_type: "conformance" (target must conform to source) or "sibling_consistency"
    
    Strategy:
    1. Each MUTABLE doc is checked against all IMMUTABLE docs (conformance)
    2. Mutable sibling pairs are checked against each other (sibling_consistency)
    """
    pairs: list[tuple[str, str, str]] = []
    
    immutable_ids = set(get_immutable_docs(node_map))
    mutable_ids = set(get_mutable_docs(node_map))
    
    # 1. Each mutable doc checked against all immutable docs
    for mutable_id in mutable_ids:
        for immutable_id in immutable_ids:
            pairs.append((immutable_id, mutable_id, "conformance"))
    
    # 2. Sibling consistency: for each mutable doc, check against its mutable siblings
    for doc_id in mutable_ids:
        siblings = get_siblings(doc_id, node_map)
        mutable_siblings = [s for s in siblings if s in mutable_ids]
        
        # Only check each sibling pair once (one direction)
        for sibling_id in mutable_siblings:
            if doc_id < sibling_id:  # Lexicographic ordering to avoid duplicates
                pairs.append((doc_id, sibling_id, "sibling_consistency"))
    
    return pairs


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
    source_id: str,
    target_id: str,
    all_docs: dict,
    conn,
    client: anthropic.Anthropic,
    check_type: str = "conformance",
    progress: Optional[Callable[[str], None]] = None,
) -> list[ComplianceFinding]:
    """
    Assess compliance of target_id against requirements from source_id.
    
    check_type: "conformance" (target conforms to source) or "sibling_consistency"
    """
    source_doc = all_docs.get(source_id)
    target_doc = all_docs.get(target_id)
    if not source_doc or not target_doc:
        return []

    source_title = source_doc["title"]
    target_title = target_doc["title"]

    # --- cache lookup ---
    cache_key = (
        f"compliance:{compute_hash(source_doc['content'] or '')}:"
        f"{compute_hash(target_doc['content'] or '')}:{check_type}"
    )
    cached = get_cached(conn, cache_key)
    if cached is not None:
        if progress:
            check_label = f"conformance [{target_id}->{source_id}]" if check_type == "conformance" else f"sibling [{target_id}<->{source_id}]"
            progress(f"  [cached] {check_label}")
        return [ComplianceFinding(**f) for f in cached]

    if progress:
        check_label = f"conformance [{target_id}->{source_id}]" if check_type == "conformance" else f"sibling [{target_id}<->{source_id}]"
        progress(f"  Checking {check_label}")

    # Extract requirements from source
    source_sections = list(get_sections(conn, source_id))
    requirements = extract_requirements(
        source_id, source_sections, fallback_content=source_doc["content"] or ""
    )
    if not requirements:
        if progress:
            progress(f"    No requirements found in source '{source_id}' — skipping.")
        return []

    if progress:
        progress(f"    {len(requirements)} requirement(s) extracted from source.")

    # Prepare target sections for relevance matching
    target_sections = _sections_or_paragraphs(target_doc, conn)

    # Build batch items: one entry per requirement with pre-filtered target sections
    batch_items: list[dict] = []
    req_map: dict[str, tuple[Requirement, list[dict]]] = {}

    for req in requirements:
        relevant = _relevant_sections(req, target_sections)
        item = {
            "req_id": req.req_id,
            "requirement": req.text,
            "requirement_section": req.section_heading,
            "target_doc": target_title,
            "target_sections": [
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
        results = _batch_assess(batch, client, check_type=check_type)

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
                source_doc_id=source_id,
                source_doc_title=source_title,
                target_doc_id=target_id,
                target_doc_title=target_title,
                check_type=check_type,
            ))

    store_cached(conn, cache_key, "compliance", [asdict(f) for f in findings])
    return findings


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_report(findings: list[ComplianceFinding]) -> dict:
    """Serialize findings into a JSON-ready compliance report."""
    by_status: dict[str, int] = {}
    by_check_type: dict[str, int] = {}
    pairs_seen: dict[tuple[str, str], int] = {}

    serialised = []
    for f in findings:
        by_status[f.status] = by_status.get(f.status, 0) + 1
        by_check_type[f.check_type] = by_check_type.get(f.check_type, 0) + 1
        key = (f.source_doc_id, f.target_doc_id)
        pairs_seen[key] = pairs_seen.get(key, 0) + 1
        serialised.append(asdict(f))

    pairs_summary = [
        {
            "source_doc_id": sid,
            "target_doc_id": tid,
            "requirements_checked": count,
        }
        for (sid, tid), count in pairs_seen.items()
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
            "by_check_type": by_check_type,
        },
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run_check_pair(
    conn,
    source_id: str,
    target_id: str,
    check_type: str = "conformance",
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Check compliance for a single source-target document pair."""
    all_docs = {row["id"]: dict(row) for row in get_all_documents(conn)}

    # Never report findings about immutable documents
    try:
        nodes_list = load_hierarchy(Path.cwd() / "hierarchy.yaml")
        node_map = build_node_map(nodes_list)
        if target_id in set(get_immutable_docs(node_map)):
            return _build_report([])
    except Exception:
        pass

    client = anthropic.Anthropic()
    findings = _check_pair(source_id, target_id, all_docs, conn, client, check_type=check_type, progress=progress)
    return _build_report(findings)


def run_check_all(
    conn,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Check compliance for all mutable docs against immutable docs and siblings.

    Conformance logic (immutable → mutable):
    - A requirement is CLOSED if any mutable document covers it.
    - A requirement with contradictions is always reported, even if covered elsewhere.
    - Uncovered requirements are classified by whether a relevant document exists:
        - Type A (gap_type="not_covered_by_relevant_policy"): at least one mutable doc is
          topically relevant but does not cover the requirement.  Attributed to the most
          relevant doc.
        - Type B (gap_type="no_relevant_policy", status="no_policy"): no mutable doc in
          the hierarchy is sufficiently related to this requirement topic.

    Sibling consistency checks are unchanged and reported as-is.
    """
    all_docs = {row["id"]: dict(row) for row in get_all_documents(conn)}
    nodes_list = load_hierarchy(Path.cwd() / "hierarchy.yaml")
    node_map = build_node_map(nodes_list)

    immutable_ids = set(get_immutable_docs(node_map))
    mutable_ids = set(get_mutable_docs(node_map))

    pairs = _build_conformance_and_sibling_pairs(node_map)
    client = anthropic.Anthropic()

    # --- Step 1: run all pair-based checks (preserves per-pair caching) ---
    conformance_pair_findings: dict[tuple[str, str], list[ComplianceFinding]] = {}
    sibling_findings: list[ComplianceFinding] = []

    for source_id, target_id, check_type in pairs:
        raw = _check_pair(
            source_id, target_id, all_docs, conn, client,
            check_type=check_type, progress=progress,
        )
        if check_type == "conformance":
            conformance_pair_findings[(source_id, target_id)] = raw
        else:
            sibling_findings.extend(raw)

    # --- Step 2: pre-compute sections for all mutable docs (for relevance scoring) ---
    mutable_doc_sections: dict[str, list[dict]] = {
        doc_id: _sections_or_paragraphs(all_docs[doc_id], conn)
        for doc_id in mutable_ids
        if doc_id in all_docs
    }

    # --- Step 3: re-extract requirements from immutable sources ---
    # extract_requirements is deterministic: req_ids will match those in pair findings.
    imm_requirements: dict[str, list[Requirement]] = {}
    for imm_id in immutable_ids:
        source_sections = list(get_sections(conn, imm_id))
        imm_requirements[imm_id] = extract_requirements(
            imm_id, source_sections,
            fallback_content=all_docs.get(imm_id, {}).get("content", ""),
        )

    # --- Step 4: aggregate conformance findings requirement-by-requirement ---
    aggregated: list[ComplianceFinding] = []

    for imm_id, reqs in imm_requirements.items():
        imm_title = all_docs.get(imm_id, {}).get("title", imm_id)

        for req in reqs:
            # Collect this requirement's finding from every mutable doc
            req_findings: dict[str, ComplianceFinding] = {}
            for mut_id in mutable_ids:
                for f in conformance_pair_findings.get((imm_id, mut_id), []):
                    if f.req_id == req.req_id:
                        req_findings[mut_id] = f
                        break

            # Compute document-level relevance first so contradiction and gap analysis
            # both operate only on documents that are topically related to this requirement.
            relevance: dict[str, float] = {
                mut_id: _doc_max_relevance(req, mutable_doc_sections.get(mut_id, []))
                for mut_id in mutable_ids
            }
            relevant_mut_ids = [
                mid for mid, score in relevance.items() if score >= DOC_RELEVANCE_MIN
            ]

            # Work only with relevant-doc findings from here on
            relevant_findings = {
                mid: f for mid, f in req_findings.items() if mid in relevant_mut_ids
            }

            # Surface contradictions from relevant docs (always, even if covered elsewhere)
            contradicted_ids: set[str] = set()
            for mut_id, f in relevant_findings.items():
                if f.status == "contradicted":
                    contradicted_ids.add(mut_id)
                    aggregated.append(ComplianceFinding(
                        req_id=f.req_id,
                        requirement_text=f.requirement_text,
                        requirement_section=f.requirement_section,
                        status=f.status,
                        evidence=f.evidence,
                        evidence_sections=f.evidence_sections,
                        explanation=f.explanation,
                        source_doc_id=f.source_doc_id,
                        source_doc_title=f.source_doc_title,
                        target_doc_id=f.target_doc_id,
                        target_doc_title=f.target_doc_title,
                        check_type=f.check_type,
                        gap_type="not_covered_by_relevant_policy",
                    ))

            # If any relevant doc covers the requirement → closed; no gap to report
            if any(f.status == "covered" for f in relevant_findings.values()):
                continue

            # Gap analysis: exclude docs already reported as contradictions
            gap_candidate_ids = [
                mid for mid in relevant_mut_ids if mid not in contradicted_ids
            ]

            if not gap_candidate_ids:
                if not relevant_mut_ids:
                    # Type B: no document in the hierarchy is related to this topic
                    aggregated.append(ComplianceFinding(
                        req_id=req.req_id,
                        requirement_text=req.text,
                        requirement_section=req.section_heading,
                        status="no_policy",
                        evidence="",
                        evidence_sections=[],
                        explanation=(
                            "No policy or procedure in the document set addresses this topic. "
                            "Consider designating or creating a document to cover this requirement."
                        ),
                        source_doc_id=imm_id,
                        source_doc_title=imm_title,
                        target_doc_id="",
                        target_doc_title="(no relevant policy in document set)",
                        check_type="conformance",
                        gap_type="no_relevant_policy",
                    ))
                # else: all relevant docs contradict — contradictions already reported above
                continue

            # Type A: relevant, non-contradicting doc(s) exist but requirement is not covered.
            # Report against the single most relevant candidate.
            best_mut_id = max(gap_candidate_ids, key=lambda mid: relevance[mid])

            if best_mut_id in relevant_findings:
                f = relevant_findings[best_mut_id]
                # status is not_covered or partially_covered at this point
                aggregated.append(ComplianceFinding(
                    req_id=f.req_id,
                    requirement_text=f.requirement_text,
                    requirement_section=f.requirement_section,
                    status=f.status,
                    evidence=f.evidence,
                    evidence_sections=f.evidence_sections,
                    explanation=f.explanation,
                    source_doc_id=f.source_doc_id,
                    source_doc_title=f.source_doc_title,
                    target_doc_id=f.target_doc_id,
                    target_doc_title=f.target_doc_title,
                    check_type=f.check_type,
                    gap_type="not_covered_by_relevant_policy",
                ))
            else:
                # Best doc had no pair finding (e.g. not in DB yet); emit a minimal gap.
                mut_doc = all_docs.get(best_mut_id, {})
                aggregated.append(ComplianceFinding(
                    req_id=req.req_id,
                    requirement_text=req.text,
                    requirement_section=req.section_heading,
                    status="not_covered",
                    evidence="",
                    evidence_sections=[],
                    explanation="Relevant document did not return a finding for this requirement.",
                    source_doc_id=imm_id,
                    source_doc_title=imm_title,
                    target_doc_id=best_mut_id,
                    target_doc_title=mut_doc.get("title", best_mut_id),
                    check_type="conformance",
                    gap_type="not_covered_by_relevant_policy",
                ))

    # Sibling consistency findings are reported as-is (not subject to the above logic)
    all_findings = aggregated + sibling_findings
    return _build_report(all_findings)
