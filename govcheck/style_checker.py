"""
Check writing style consistency and quality across governance documents.

Four check types (all configurable via .govcheck-style.yaml):

  modal_consistency  — mixed mandatory modals (e.g. shall + must for the same intent)
  readability        — complex sentences, passive-voice overuse, ambiguous pronouns
  terminology        — synonymous terms used for the same concept
  parent_consistency — child uses a different mandatory modal than its parent

Regex-only checks (modal, banned words, preferred terms) run without any API calls.
Claude is used for readability and terminology analysis.
"""
from __future__ import annotations

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
from .hierarchy import load_hierarchy, build_node_map, get_immutable_docs
from .style_config import StyleConfig


def _get_immutable_ids() -> set[str]:
    """Return the set of immutable document IDs from hierarchy.yaml, or empty set on failure."""
    try:
        nodes_list = load_hierarchy(Path.cwd() / "hierarchy.yaml")
        node_map = build_node_map(nodes_list)
        return set(get_immutable_docs(node_map))
    except Exception:
        return set()

MODEL = "claude-sonnet-4-20250514"
READABILITY_BATCH = 5    # sections per Claude readability call
TERMINOLOGY_BATCH = 8    # sections per Claude terminology call
SECTION_EXCERPT = 1200   # max chars per section excerpt sent to Claude


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class StyleFinding:
    finding_type: str    # modal_inconsistency | readability_* | terminology_inconsistency
                         # | banned_word | preferred_term | parent_consistency
    severity: str        # high | medium | low
    doc_id: str
    doc_title: str
    section_heading: Optional[str]
    sentence: Optional[str]
    message: str
    detail: dict


# ---------------------------------------------------------------------------
# Modal-verb analysis  (no Claude)
# ---------------------------------------------------------------------------

_MODAL_RE = re.compile(
    r'\b(shall|must|should|may|can|will|would)\b',
    re.IGNORECASE,
)


def _count_modals(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in _MODAL_RE.finditer(text):
        modal = m.group(0).lower()
        counts[modal] = counts.get(modal, 0) + 1
    return counts


def _dominant(counts: dict[str, int], group: list[str]) -> Optional[str]:
    """Return the most-used modal from *group*, or None if none appear."""
    found = {m: counts.get(m, 0) for m in group if counts.get(m, 0) > 0}
    return max(found, key=lambda m: found[m]) if found else None


def _check_modal_consistency(
    doc_id: str,
    doc_title: str,
    sections: list[dict],
    config: StyleConfig,
) -> list[StyleFinding]:
    full_text = "\n".join((s.get("content") or "") for s in sections)
    counts = _count_modals(full_text)
    mandatory = config.mandatory_modals()
    preferred = config.preferred_modal

    used = {m: counts[m] for m in mandatory if counts.get(m, 0) > 0}
    findings: list[StyleFinding] = []

    if len(used) <= 1:
        # Only one mandatory modal — check against preference
        if preferred and used:
            used_modal = next(iter(used))
            if used_modal != preferred:
                findings.append(StyleFinding(
                    finding_type="modal_inconsistency",
                    severity="low",
                    doc_id=doc_id,
                    doc_title=doc_title,
                    section_heading=None,
                    sentence=None,
                    message=(
                        f'Uses "{used_modal}" ({used[used_modal]}x) '
                        f'but config prefers "{preferred}".'
                    ),
                    detail={
                        "modal_counts": used,
                        "preferred": preferred,
                        "found": used_modal,
                    },
                ))
        return findings

    # Multiple mandatory modals used
    total = sum(used.values())
    dominant = max(used, key=lambda m: used[m])

    if preferred and preferred in used:
        # Flag each non-preferred modal
        for np_modal, np_count in used.items():
            if np_modal == preferred:
                continue
            severity = "high" if np_count / total > 0.3 else "medium"
            findings.append(StyleFinding(
                finding_type="modal_inconsistency",
                severity=severity,
                doc_id=doc_id,
                doc_title=doc_title,
                section_heading=None,
                sentence=None,
                message=(
                    f'Uses "{np_modal}" ({np_count}x) alongside preferred '
                    f'"{preferred}" ({used[preferred]}x). '
                    f"Standardise on \"{preferred}\"."
                ),
                detail={
                    "modal_counts": used,
                    "preferred": preferred,
                    "non_preferred": np_modal,
                },
            ))
    else:
        # No preference configured — flag the minority modals
        minor = {m: c for m, c in used.items() if c / total < 0.25}
        severity = "high" if any(c / total > 0.15 for c in minor.values()) else "medium"
        minority_str = ", ".join(f'"{m}" ({c}x)' for m, c in minor.items())
        findings.append(StyleFinding(
            finding_type="modal_inconsistency",
            severity=severity,
            doc_id=doc_id,
            doc_title=doc_title,
            section_heading=None,
            sentence=None,
            message=(
                f'Mixed mandatory modals: dominant "{dominant}" '
                f'({used[dominant]}x) but also {minority_str}. '
                f"Consider standardising on one mandatory modal."
            ),
            detail={"modal_counts": used, "dominant": dominant},
        ))

    return findings


# ---------------------------------------------------------------------------
# Config-based terminology checks  (no Claude)
# ---------------------------------------------------------------------------

def _check_banned_words(
    doc_id: str,
    doc_title: str,
    sections: list[dict],
    config: StyleConfig,
) -> list[StyleFinding]:
    if not config.banned_words:
        return []
    findings: list[StyleFinding] = []
    for section in sections:
        content = section.get("content") or ""
        heading = section.get("heading")
        for word in config.banned_words:
            pattern = re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)
            matches = pattern.findall(content)
            if matches:
                findings.append(StyleFinding(
                    finding_type="banned_word",
                    severity="medium",
                    doc_id=doc_id,
                    doc_title=doc_title,
                    section_heading=heading,
                    sentence=None,
                    message=f'Banned word "{word}" appears {len(matches)}x.',
                    detail={"word": word, "count": len(matches)},
                ))
    return findings


def _check_preferred_terms(
    doc_id: str,
    doc_title: str,
    sections: list[dict],
    config: StyleConfig,
) -> list[StyleFinding]:
    if not config.preferred_terms:
        return []
    findings: list[StyleFinding] = []
    for section in sections:
        content = section.get("content") or ""
        heading = section.get("heading")
        for non_preferred, preferred in config.preferred_terms.items():
            np_pattern = re.compile(r'\b' + re.escape(non_preferred) + r'\b', re.IGNORECASE)
            p_pattern = re.compile(r'\b' + re.escape(preferred) + r'\b', re.IGNORECASE)
            np_matches = np_pattern.findall(content)
            if np_matches:
                p_count = len(p_pattern.findall(content))
                findings.append(StyleFinding(
                    finding_type="preferred_term",
                    severity="low",
                    doc_id=doc_id,
                    doc_title=doc_title,
                    section_heading=heading,
                    sentence=None,
                    message=(
                        f'Non-preferred term "{non_preferred}" ({len(np_matches)}x); '
                        f'prefer "{preferred}" ({p_count}x in this section).'
                    ),
                    detail={
                        "non_preferred": non_preferred,
                        "preferred": preferred,
                        "non_preferred_count": len(np_matches),
                        "preferred_count": p_count,
                    },
                ))
    return findings


# ---------------------------------------------------------------------------
# Claude helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    return text.strip()


# ---------------------------------------------------------------------------
# Readability check  (Claude)
# ---------------------------------------------------------------------------

def _batch_readability(
    doc_id: str,
    doc_title: str,
    sections: list[dict],
    client: anthropic.Anthropic,
) -> list[StyleFinding]:
    findings: list[StyleFinding] = []

    for i in range(0, len(sections), READABILITY_BATCH):
        batch = sections[i: i + READABILITY_BATCH]
        payload = [
            {
                "heading": s.get("heading", f"Section {i + j + 1}"),
                "content": (s.get("content") or "")[:SECTION_EXCERPT],
            }
            for j, s in enumerate(batch)
        ]

        prompt = textwrap.dedent(f"""
            You are a technical writing analyst reviewing a governance document.
            Identify readability problems in the sections below.

            Flag these issue types only when genuinely problematic:
            - "complex_sentence": sentences that are unnecessarily long (>35 words) or
              so convoluted that the requirement is hard to parse; quote the sentence
            - "passive_voice": sections where passive voice is so frequent that
              responsibility becomes unclear; quote one representative sentence
            - "ambiguous_pronoun": sentences where "it", "they", "this", "these", or
              similar pronouns have an unclear or disputed referent; quote the sentence

            Severity:
            - "high"  : could cause misinterpretation of a requirement
            - "medium": noticeable quality issue that should be corrected
            - "low"   : minor style concern

            Return ONLY a JSON array — no markdown fences, no extra text:
            [
              {{
                "section_heading": "<heading from input>",
                "issue_type": "<complex_sentence|passive_voice|ambiguous_pronoun>",
                "severity": "<high|medium|low>",
                "sentence": "<the offending sentence or phrase, verbatim>",
                "message": "<brief explanation of the problem>"
              }}
            ]

            Return [] if there are no issues.

            Sections:
            {json.dumps(payload, indent=2)}
        """).strip()

        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _strip_fences(response.content[0].text)
        try:
            results = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for r in results:
            issue_type = r.get("issue_type", "unknown")
            findings.append(StyleFinding(
                finding_type=f"readability_{issue_type}",
                severity=r.get("severity", "medium"),
                doc_id=doc_id,
                doc_title=doc_title,
                section_heading=r.get("section_heading"),
                sentence=r.get("sentence"),
                message=r.get("message", ""),
                detail={"issue_type": issue_type},
            ))

    return findings


# ---------------------------------------------------------------------------
# Terminology consistency check  (Claude)
# ---------------------------------------------------------------------------

def _batch_terminology(
    doc_id: str,
    doc_title: str,
    sections: list[dict],
    client: anthropic.Anthropic,
) -> list[StyleFinding]:
    findings: list[StyleFinding] = []

    for i in range(0, len(sections), TERMINOLOGY_BATCH):
        batch = sections[i: i + TERMINOLOGY_BATCH]
        payload = [
            {
                "heading": s.get("heading", f"Section {i + j + 1}"),
                "content": (s.get("content") or "")[:SECTION_EXCERPT],
            }
            for j, s in enumerate(batch)
        ]

        prompt = textwrap.dedent(f"""
            You are a technical writing analyst reviewing a governance document.
            Identify terminology inconsistencies across the sections below.

            A terminology inconsistency is when two or more different terms are used
            to mean the same concept — for example, using "data subject" in one section
            and "individual" in another to refer to the same type of person.

            Return ONLY a JSON array — no markdown fences, no extra text:
            [
              {{
                "terms": ["<term1>", "<term2>"],
                "severity": "<high|medium|low>",
                "message": "<brief explanation>",
                "examples": [
                  {{
                    "term": "<term>",
                    "section_heading": "<heading>",
                    "context": "<short quote showing usage>"
                  }}
                ]
              }}
            ]

            Severity:
            - "high"  : ambiguity could lead to different interpretations of a requirement
            - "medium": inconsistency should be standardised
            - "low"   : minor variation that may be stylistic

            Return [] if there are no inconsistencies.

            Do NOT flag:
            - Terms that are clearly different concepts
            - Standard industry synonyms used deliberately
            - Pronoun variation (he/she/they)

            Sections:
            {json.dumps(payload, indent=2)}
        """).strip()

        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _strip_fences(response.content[0].text)
        try:
            results = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for r in results:
            terms = r.get("terms", [])
            examples = r.get("examples", [])
            first_section = examples[0].get("section_heading") if examples else None
            findings.append(StyleFinding(
                finding_type="terminology_inconsistency",
                severity=r.get("severity", "medium"),
                doc_id=doc_id,
                doc_title=doc_title,
                section_heading=first_section,
                sentence=None,
                message=r.get("message", f"Inconsistent terms: {', '.join(terms)}"),
                detail={"terms": terms, "examples": examples},
            ))

    return findings


# ---------------------------------------------------------------------------
# Parent–child modal consistency  (no Claude)
# ---------------------------------------------------------------------------

def _check_parent_consistency(
    doc_id: str,
    doc_title: str,
    sections: list[dict],
    parent_id: str,
    parent_title: str,
    parent_sections: list[dict],
    config: StyleConfig,
) -> list[StyleFinding]:
    mandatory = config.mandatory_modals()

    doc_text = "\n".join((s.get("content") or "") for s in sections)
    parent_text = "\n".join((s.get("content") or "") for s in parent_sections)

    doc_counts = _count_modals(doc_text)
    parent_counts = _count_modals(parent_text)

    doc_dominant = _dominant(doc_counts, mandatory)
    parent_dominant = _dominant(parent_counts, mandatory)

    if parent_dominant and doc_dominant and parent_dominant != doc_dominant:
        return [StyleFinding(
            finding_type="parent_consistency",
            severity="medium",
            doc_id=doc_id,
            doc_title=doc_title,
            section_heading=None,
            sentence=None,
            message=(
                f'Parent [{parent_id}] "{parent_title}" uses "{parent_dominant}" '
                f'for mandatory requirements; this document uses "{doc_dominant}". '
                f"Consider aligning the mandatory modal across the hierarchy."
            ),
            detail={
                "parent_doc_id": parent_id,
                "parent_doc_title": parent_title,
                "parent_dominant_modal": parent_dominant,
                "child_dominant_modal": doc_dominant,
                "parent_modal_counts": {m: parent_counts.get(m, 0) for m in mandatory},
                "child_modal_counts": {m: doc_counts.get(m, 0) for m in mandatory},
            },
        )]
    return []


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_report(
    findings: list[StyleFinding],
    docs_checked: list[dict],
    config: StyleConfig,
) -> dict:
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_doc: dict[str, int] = {}

    for f in findings:
        by_type[f.finding_type] = by_type.get(f.finding_type, 0) + 1
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_doc[f.doc_id] = by_doc.get(f.doc_id, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "check_type": "style",
        "documents_checked": docs_checked,
        "config_used": {
            "preferred_modal": config.preferred_modal,
            "banned_words": config.banned_words,
            "preferred_terms": config.preferred_terms,
            "rules": config.rules,
            "readability": config.readability,
        },
        "findings": [asdict(f) for f in findings],
        "summary": {
            "total_findings": len(findings),
            "by_type": by_type,
            "by_severity": by_severity,
            "by_document": by_doc,
        },
    }


# ---------------------------------------------------------------------------
# Core check for a single document
# ---------------------------------------------------------------------------

def _check_document(
    doc_id: str,
    all_docs: dict,
    all_hierarchy: list[dict],
    conn,
    client: anthropic.Anthropic,
    config: StyleConfig,
    progress: Optional[Callable[[str], None]] = None,
) -> list[StyleFinding]:
    doc = all_docs.get(doc_id)
    if not doc:
        return []

    doc_title = doc["title"]

    # --- cache lookup ---
    parent_row = next(
        (r for r in all_hierarchy if r["id"] == doc_id and r["parent_id"]),
        None,
    )
    parent_content = ""
    if parent_row:
        parent_doc = all_docs.get(parent_row["parent_id"])
        if parent_doc:
            parent_content = parent_doc.get("content") or ""

    config_sig = json.dumps({
        "preferred_modal": config.preferred_modal,
        "banned_words": sorted(config.banned_words),
        "preferred_terms": dict(sorted(config.preferred_terms.items())),
        "rules": dict(sorted(config.rules.items())),
    }, sort_keys=True)
    cache_key = (
        f"style:{compute_hash(doc.get('content') or '')}:"
        f"{compute_hash(parent_content)}:{compute_hash(config_sig)}"
    )
    cached = get_cached(conn, cache_key)
    if cached is not None:
        if progress:
            progress(f"  [cached] [{doc_id}] {doc_title}")
        return [StyleFinding(**f) for f in cached]

    if progress:
        progress(f"  Checking style: [{doc_id}] {doc_title}")

    sections = [dict(row) for row in get_sections(conn, doc_id)]
    if not sections:
        content = doc.get("content") or ""
        sections = [{"heading": "(document)", "level": 1, "content": content, "position": 0}]

    findings: list[StyleFinding] = []

    # ── Regex-only checks ──────────────────────────────────────────────────
    if config.rules.get("modal_consistency", True):
        findings.extend(_check_modal_consistency(doc_id, doc_title, sections, config))

    if config.rules.get("terminology", True):
        findings.extend(_check_banned_words(doc_id, doc_title, sections, config))
        findings.extend(_check_preferred_terms(doc_id, doc_title, sections, config))

    # ── Claude checks ──────────────────────────────────────────────────────
    if config.rules.get("readability", True):
        if progress:
            progress(f"    Readability check via Claude ...")
        findings.extend(_batch_readability(doc_id, doc_title, sections, client))

    if config.rules.get("terminology", True):
        if progress:
            progress(f"    Terminology check via Claude ...")
        findings.extend(_batch_terminology(doc_id, doc_title, sections, client))

    # ── Parent-consistency check ───────────────────────────────────────────
    if config.rules.get("parent_consistency", True):
        parent_row = next(
            (r for r in all_hierarchy if r["id"] == doc_id and r["parent_id"]),
            None,
        )
        if parent_row:
            parent_id = parent_row["parent_id"]
            parent_doc = all_docs.get(parent_id)
            if parent_doc:
                parent_sections = [dict(row) for row in get_sections(conn, parent_id)]
                if not parent_sections:
                    parent_sections = [{
                        "heading": "(document)",
                        "level": 1,
                        "content": parent_doc.get("content") or "",
                        "position": 0,
                    }]
                findings.extend(_check_parent_consistency(
                    doc_id, doc_title, sections,
                    parent_id, parent_doc["title"], parent_sections,
                    config,
                ))

    store_cached(conn, cache_key, "style", [asdict(f) for f in findings])
    return findings


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run_check(
    conn,
    doc_id: str,
    config: StyleConfig,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Run all enabled style checks for a single document."""
    all_docs = {row["id"]: dict(row) for row in get_all_documents(conn)}
    if doc_id not in all_docs:
        raise ValueError(f"Document '{doc_id}' not found in the database.")

    if doc_id in _get_immutable_ids():
        doc = all_docs[doc_id]
        return _build_report([], [{"id": doc_id, "title": doc["title"]}], config)

    all_hierarchy = list(get_hierarchy(conn))
    client = anthropic.Anthropic()

    findings = _check_document(doc_id, all_docs, all_hierarchy, conn, client, config, progress=progress)
    doc = all_docs[doc_id]
    return _build_report(findings, [{"id": doc_id, "title": doc["title"]}], config)


def run_check_all(
    conn,
    config: StyleConfig,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Run style checks for every mutable document in the hierarchy."""
    all_docs = {row["id"]: dict(row) for row in get_all_documents(conn)}
    all_hierarchy = list(get_hierarchy(conn))
    client = anthropic.Anthropic()
    immutable_ids = _get_immutable_ids()

    all_findings: list[StyleFinding] = []
    docs_checked = [
        {"id": doc_id, "title": doc["title"]}
        for doc_id, doc in all_docs.items()
        if doc_id not in immutable_ids
    ]

    for doc_id in all_docs:
        if doc_id in immutable_ids:
            continue
        all_findings.extend(
            _check_document(doc_id, all_docs, all_hierarchy, conn, client, config, progress=progress)
        )

    return _build_report(all_findings, docs_checked, config)
