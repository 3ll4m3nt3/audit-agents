"""
Compare definitions across the governance document hierarchy.

Uses the Claude API (batched) for semantic comparison of definition pairs.
Text-based heuristics are used for structural checks (usage violations).
"""

import json
import re
import textwrap
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic

from .audit_cache import compute_hash, get_cached, store_cached
from .db import get_all_documents, get_hierarchy, get_sections
from .definitions_extractor import Definition, ExtractionMode, extract_definitions
from .hierarchy import load_hierarchy, build_node_map, get_immutable_docs


def _get_immutable_ids() -> set[str]:
    """Return the set of immutable document IDs from hierarchy.yaml, or empty set on failure."""
    try:
        nodes_list = load_hierarchy(Path.cwd() / "hierarchy.yaml")
        node_map = build_node_map(nodes_list)
        return set(get_immutable_docs(node_map))
    except Exception:
        return set()

MODEL = "claude-sonnet-4-20250514"
BATCH_SIZE = 20    # definition pairs per API call


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    finding_type: str           # contradiction | narrowing | usage_violation
    severity: str               # high | medium | low
    term: str
    child_doc_id: str
    child_doc_title: str
    parent_doc_id: str
    parent_doc_title: str
    child_definition: Optional[dict]    # {text, section, section_id} or None
    parent_definition: Optional[dict]   # {text, section, section_id} or None
    explanation: str


# ---------------------------------------------------------------------------
# Hierarchy helpers
# ---------------------------------------------------------------------------

def _build_parent_map(hierarchy_rows) -> dict[str, list[str]]:
    """Return {child_id: [parent_id, ...]} for all non-root nodes."""
    parent_map: dict[str, list[str]] = {}
    for row in hierarchy_rows:
        parent_id = row["parent_id"]
        if parent_id:   # empty string == root
            parent_map.setdefault(row["id"], []).append(parent_id)
    return parent_map


def _build_ancestor_map(parent_map: dict[str, list[str]]) -> dict[str, set[str]]:
    """Return {doc_id: {all ancestor ids}} using memoised DFS."""
    cache: dict[str, set[str]] = {}

    def _ancestors(doc_id: str) -> set[str]:
        if doc_id in cache:
            return cache[doc_id]
        result: set[str] = set()
        for pid in parent_map.get(doc_id, []):
            result.add(pid)
            result |= _ancestors(pid)
        cache[doc_id] = result
        return result

    for doc_id in parent_map:
        _ancestors(doc_id)
    return cache


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    return text.strip()


def _batch_compare(pairs: list[dict], client: anthropic.Anthropic) -> list[dict]:
    """
    Send up to BATCH_SIZE definition pairs to Claude for semantic comparison.

    Each pair dict must have:
        pair_id, term, parent_doc, parent_definition, child_doc, child_definition

    Returns a list of dicts with:
        pair_id, relationship, severity, explanation
    """
    if not pairs:
        return []

    prompt = textwrap.dedent(f"""
        You are a governance document expert reviewing definition consistency across a
        policy hierarchy (parent documents set policy; child documents implement it).

        For each pair below, compare the parent definition against the child definition
        for the same term and classify the relationship:

        - "same"         : essentially identical meaning
        - "consistent"   : child is compatible and does not conflict
        - "narrowing"    : child restricts or specialises the parent definition
                           without explicitly acknowledging the restriction
        - "contradiction": the definitions are materially different or contradictory
        - "unrelated"    : the same word is used in unrelated contexts; comparison
                           is not meaningful

        Also assess severity of any issue found:
        - "high"   : significant compliance or governance risk
        - "medium" : potential for confusion or misapplication
        - "low"    : minor difference, unlikely to cause real problems
        - "none"   : no issue (use for "same", "consistent", "unrelated")

        Return ONLY a JSON array — no markdown fences, no surrounding text:
        [
          {{
            "pair_id": "<pair_id from input>",
            "relationship": "<same|consistent|narrowing|contradiction|unrelated>",
            "severity": "<high|medium|low|none>",
            "explanation": "<1-2 sentence explanation>"
          }},
          ...
        ]

        Pairs:
        {json.dumps(pairs, indent=2)}
    """).strip()

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _strip_fences(response.content[0].text)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Structural checks (text-based, no API call)
# ---------------------------------------------------------------------------

def _def_to_dict(d: Definition) -> dict:
    return {
        "text": d.definition_text,
        "section": d.section_heading,
        "section_id": d.section_id,
    }


def _extract_usage_sentences(content: str, term: str, max_sentences: int = 5) -> list[str]:
    """Return up to max_sentences sentences from content that contain the term."""
    term_lower = term.lower()
    # Split on sentence-ending punctuation followed by whitespace
    sentences = re.split(r'(?<=[.!?])\s+', content)
    result = []
    for s in sentences:
        s = s.strip()
        if s and term_lower in s.lower() and len(s) > 15:
            result.append(s)
            if len(result) >= max_sentences:
                break
    return result


def _batch_check_usage(items: list[dict], client: anthropic.Anthropic) -> list[dict]:
    """
    Check whether terms are used consistently with their parent definitions.

    Each item dict must have:
        item_id, term, parent_doc, parent_definition, child_doc, usage_sentences

    Returns a list of dicts with:
        item_id, consistent, severity, explanation
    """
    if not items:
        return []

    prompt = textwrap.dedent(f"""
        You are a governance document expert. For each item below, a term is formally
        defined in a parent document. The child document uses the term but does not
        formally re-define it.

        Review the usage sentences from the child document and determine whether the
        term is being used consistently with the parent definition.

        Classify each as:
        - "consistent"  : the child uses the term in a way that aligns with the parent
                          definition (no flag needed)
        - "inconsistent": the child uses the term in a way that contradicts or materially
                          differs from the parent definition

        Also assess severity of any inconsistency:
        - "high"   : significant compliance or governance risk
        - "medium" : potential for confusion or misapplication
        - "low"    : minor difference, unlikely to cause real problems
        - "none"   : use when consistent

        Return ONLY a JSON array — no markdown fences, no surrounding text:
        [
          {{
            "item_id": "<item_id from input>",
            "consistent": true,
            "severity": "none",
            "explanation": "<1-2 sentence explanation>"
          }},
          ...
        ]

        Items:
        {json.dumps(items, indent=2)}
    """).strip()

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _strip_fences(response.content[0].text)
    return json.loads(raw)


def _find_usage_violations(
    child_id: str,
    child_title: str,
    child_content: str,
    child_terms: set[str],          # lower-cased terms formally defined in child
    ancestor_defs_map: dict[str, list[Definition]],
    doc_titles: dict[str, str],
    client: anthropic.Anthropic,
) -> list[Finding]:
    """
    For each parent-defined term NOT formally defined in the child, extract sentences
    from the child that use it and ask Claude whether the usage is consistent with the
    parent definition. Flag case 3: term used but semantically differently from parent.
    """
    if not child_content:
        return []

    items: list[dict] = []
    item_meta: dict[str, dict] = {}

    for ancestor_id, ancestor_defs in ancestor_defs_map.items():
        ancestor_title = doc_titles.get(ancestor_id, ancestor_id)
        for adef in ancestor_defs:
            term_lower = adef.term.lower()
            if term_lower in child_terms:
                continue  # formally defined in child → handled by case 1
            usage_sentences = _extract_usage_sentences(child_content, adef.term)
            if not usage_sentences:
                continue  # term not used in child → nothing to check (case 2 passes silently)
            item_id = f"{child_id}__{ancestor_id}__{term_lower}"
            items.append({
                "item_id": item_id,
                "term": adef.term,
                "parent_doc": ancestor_title,
                "parent_definition": adef.definition_text,
                "child_doc": child_title,
                "usage_sentences": usage_sentences,
            })
            item_meta[item_id] = {
                "ancestor_id": ancestor_id,
                "ancestor_title": ancestor_title,
                "parent_def": adef,
            }

    if not items:
        return []

    findings: list[Finding] = []
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i : i + BATCH_SIZE]
        try:
            results = _batch_check_usage(batch, client)
        except Exception:
            continue

        for result in results:
            item_id = result.get("item_id", "")
            meta = item_meta.get(item_id)
            if not meta:
                continue
            if result.get("consistent", True):
                continue
            severity = result.get("severity", "none")
            if severity == "none":
                continue
            findings.append(Finding(
                finding_type="usage_violation",
                severity=severity,
                term=meta["parent_def"].term,
                child_doc_id=child_id,
                child_doc_title=child_title,
                parent_doc_id=meta["ancestor_id"],
                parent_doc_title=meta["ancestor_title"],
                child_definition=None,
                parent_definition=_def_to_dict(meta["parent_def"]),
                explanation=result.get("explanation", ""),
            ))

    return findings



# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_check(
    conn,
    *,
    mode: "ExtractionMode | list[ExtractionMode]" = ExtractionMode.GLOSSARY,
) -> dict:
    """
    Build a definitions consistency report for all documents in the DB.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    mode:
        Extraction mode(s) passed to ``extract_definitions``.
        See ``ExtractionMode`` for documentation on each mode.

    Returns a dict suitable for JSON serialisation.
    """
    all_docs = {row["id"]: dict(row) for row in get_all_documents(conn)}
    if not all_docs:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": "No documents found. Run 'govcheck ingest' first.",
            "findings": [],
            "summary": {"total_findings": 0, "by_type": {}, "by_severity": {}},
        }

    hierarchy_rows = get_hierarchy(conn)

    immutable_ids = _get_immutable_ids()

    # Normalise mode to a stable string for the cache key
    mode_list = mode if isinstance(mode, list) else [mode]
    mode_key = ",".join(sorted(m.value for m in mode_list))

    # --- cache lookup ---
    content_sig = "".join(
        f"{doc_id}:{doc['content'] or ''}"
        for doc_id, doc in sorted(all_docs.items())
    ) + "||" + "".join(
        f"{r['id']}:{r['parent_id']}"
        for r in sorted(hierarchy_rows, key=lambda r: (r["id"], r["parent_id"]))
    ) + "||immutable:" + ",".join(sorted(immutable_ids)) + "||mode:" + mode_key
    cache_key = f"definitions:{compute_hash(content_sig)}"
    cached = get_cached(conn, cache_key)
    if cached is not None:
        cached["generated_at"] = datetime.now(timezone.utc).isoformat()
        cached["cached"] = True
        return cached

    # Semantic mode needs a shared client so we can pass it to the extractor
    client = anthropic.Anthropic()
    extractor_client = client if ExtractionMode.SEMANTIC in mode_list else None

    parent_map = _build_parent_map(hierarchy_rows)
    ancestor_map = _build_ancestor_map(parent_map)
    doc_titles: dict[str, str] = {doc_id: row["title"] for doc_id, row in all_docs.items()}

    # Build definitions index for every document
    defs_index: dict[str, list[Definition]] = {}
    for doc_id in all_docs:
        sections = get_sections(conn, doc_id)
        defs_index[doc_id] = extract_definitions(
            doc_id, list(sections), mode=mode, client=extractor_client
        )

    # ------------------------------------------------------------------
    # Collect definition pairs for Claude comparison
    # ------------------------------------------------------------------
    comparison_pairs: list[dict] = []
    pair_meta: dict[str, dict] = {}   # pair_id -> metadata for later lookup

    for child_id, child_parent_ids in parent_map.items():
        if child_id in immutable_ids:
            continue  # never report findings about immutable documents
        # Middle-tier documents are both parents and children — always check them
        # as children against their own parents. Root documents never appear in
        # parent_map, so no guard is needed here.
        child_defs = defs_index.get(child_id, [])
        child_term_map: dict[str, Definition] = {d.term.lower(): d for d in child_defs}

        for parent_id in child_parent_ids:
            parent_defs = defs_index.get(parent_id, [])

            for pdef in parent_defs:
                term_lower = pdef.term.lower()
                if term_lower in child_term_map:
                    cdef = child_term_map[term_lower]
                    pair_id = f"{child_id}__{parent_id}__{term_lower}"
                    comparison_pairs.append({
                        "pair_id": pair_id,
                        "term": pdef.term,
                        "parent_doc": doc_titles.get(parent_id, parent_id),
                        "parent_definition": pdef.definition_text,
                        "child_doc": doc_titles.get(child_id, child_id),
                        "child_definition": cdef.definition_text,
                    })
                    pair_meta[pair_id] = {
                        "child_id": child_id,
                        "parent_id": parent_id,
                        "child_def": cdef,
                        "parent_def": pdef,
                    }

    # ------------------------------------------------------------------
    # Run batched Claude comparisons
    # ------------------------------------------------------------------
    findings: list[Finding] = []
    client = anthropic.Anthropic()

    for i in range(0, len(comparison_pairs), BATCH_SIZE):
        batch = comparison_pairs[i : i + BATCH_SIZE]
        results = _batch_compare(batch, client)

        for result in results:
            pair_id = result.get("pair_id", "")
            meta = pair_meta.get(pair_id)
            if not meta:
                continue

            relationship = result.get("relationship", "consistent")
            severity = result.get("severity", "none")

            if relationship in ("contradiction", "narrowing") and severity != "none":
                findings.append(Finding(
                    finding_type=relationship,
                    severity=severity,
                    term=meta["parent_def"].term,
                    child_doc_id=meta["child_id"],
                    child_doc_title=doc_titles.get(meta["child_id"], meta["child_id"]),
                    parent_doc_id=meta["parent_id"],
                    parent_doc_title=doc_titles.get(meta["parent_id"], meta["parent_id"]),
                    child_definition=_def_to_dict(meta["child_def"]),
                    parent_definition=_def_to_dict(meta["parent_def"]),
                    explanation=result.get("explanation", ""),
                ))

    # ------------------------------------------------------------------
    # Structural checks: usage violations
    # ------------------------------------------------------------------
    for child_id, child_defs in defs_index.items():
        if child_id not in parent_map:
            continue   # root document, no parents to compare against
        if child_id in immutable_ids:
            continue   # never report findings about immutable documents

        child_doc_row = all_docs[child_id]
        child_content = child_doc_row["content"] or ""
        child_title = doc_titles[child_id]
        child_terms = {d.term.lower() for d in child_defs}

        ancestor_ids = ancestor_map.get(child_id, set())
        ancestor_defs_map = {aid: defs_index.get(aid, []) for aid in ancestor_ids}

        findings.extend(_find_usage_violations(
            child_id, child_title, child_content, child_terms,
            ancestor_defs_map, doc_titles, client,
        ))

    # ------------------------------------------------------------------
    # Build report
    # ------------------------------------------------------------------
    pairs_analyzed = [
        {
            "child_doc": doc_titles.get(cid, cid),
            "parent_doc": doc_titles.get(pid, pid),
            "terms_compared": sum(
                1 for pid2 in pids for pdef in defs_index.get(pid2, [])
                if pdef.term.lower() in {d.term.lower() for d in defs_index.get(cid, [])}
            ),
        }
        for cid, pids in parent_map.items()
        for pid in pids
    ]

    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    serialised: list[dict] = []

    for f in findings:
        by_type[f.finding_type] = by_type.get(f.finding_type, 0) + 1
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        d = asdict(f)
        serialised.append(d)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "check_type": "definitions",
        "document_pairs_analyzed": pairs_analyzed,
        "definitions_found": {
            doc_id: len(defs) for doc_id, defs in defs_index.items()
        },
        "findings": serialised,
        "summary": {
            "total_findings": len(findings),
            "by_type": by_type,
            "by_severity": by_severity,
        },
    }
    store_cached(conn, cache_key, "definitions", result)
    return result
