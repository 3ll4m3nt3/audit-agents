"""
Generate a consolidated HTML or Markdown audit report from the three check results.
"""
from __future__ import annotations

import html as _html
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

SEVERITY_ORDER: dict[str, int] = {"critical": 4, "high": 3, "medium": 2, "low": 1}

_STATUS_TO_SEVERITY: dict[str, str] = {
    "contradicted": "critical",
    "not_covered": "high",
    "partially_covered": "medium",
}

_SEV_COLORS: dict[str, str] = {
    "critical": "#b91c1c",
    "high": "#c2410c",
    "medium": "#a16207",
    "low": "#1d4ed8",
}

_SEV_BG: dict[str, str] = {
    "critical": "#fee2e2",
    "high": "#ffedd5",
    "medium": "#fef9c3",
    "low": "#dbeafe",
}

_LEVEL_COLORS: dict[str, str] = {
    "standard": "#1e40af",
    "policy": "#166534",
    "procedure": "#92400e",
    "guideline": "#6b21a8",
}


def _compliance_severity(status: str) -> Optional[str]:
    return _STATUS_TO_SEVERITY.get(status)


def _passes_filter(severity: str, min_severity: Optional[str]) -> bool:
    if not min_severity:
        return True
    return SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER.get(min_severity, 0)


# ---------------------------------------------------------------------------
# Normalise findings from all three reports
# ---------------------------------------------------------------------------

def _normalise_findings(
    compliance_report: dict,
    definitions_report: dict,
    style_report: dict,
    severity_filter: Optional[str],
) -> list[dict]:
    """Return a flat list of normalised finding dicts across all agents."""
    out: list[dict] = []

    for f in compliance_report.get("findings", []):
        sev = _compliance_severity(f.get("status", ""))
        if sev and _passes_filter(sev, severity_filter):
            out.append({
                "agent": "compliance",
                "severity": sev,
                "doc_id": f.get("child_doc_id", ""),
                "doc_title": f.get("child_doc_title", ""),
                "section": f.get("requirement_section", ""),
                "summary": f.get("requirement_text", "")[:150],
                "explanation": f.get("explanation", ""),
                "extra": {
                    "parent": f.get("parent_doc_title", ""),
                    "status": f.get("status", ""),
                    "evidence": f.get("evidence", ""),
                    "evidence_sections": f.get("evidence_sections", []),
                },
            })

    for f in definitions_report.get("findings", []):
        sev = f.get("severity", "low")
        if _passes_filter(sev, severity_filter):
            cdef = f.get("child_definition") or {}
            out.append({
                "agent": "definitions",
                "severity": sev,
                "doc_id": f.get("child_doc_id", ""),
                "doc_title": f.get("child_doc_title", ""),
                "section": cdef.get("section", ""),
                "summary": f"Term: «{f.get('term', '')}» — {f.get('finding_type', '')}",
                "explanation": f.get("explanation", ""),
                "extra": {
                    "parent": f.get("parent_doc_title", ""),
                    "term": f.get("term", ""),
                    "finding_type": f.get("finding_type", ""),
                    "parent_definition": (f.get("parent_definition") or {}).get("text", ""),
                    "child_definition": cdef.get("text", ""),
                },
            })

    for f in style_report.get("findings", []):
        sev = f.get("severity", "low")
        if _passes_filter(sev, severity_filter):
            out.append({
                "agent": "style",
                "severity": sev,
                "doc_id": f.get("doc_id", ""),
                "doc_title": f.get("doc_title", ""),
                "section": f.get("section_heading") or "",
                "summary": f.get("message", ""),
                "explanation": f.get("sentence") or "",
                "extra": {
                    "finding_type": f.get("finding_type", ""),
                },
            })

    return out


# ---------------------------------------------------------------------------
# Hierarchy tree helpers
# ---------------------------------------------------------------------------

def _build_hierarchy_tree(
    docs: list[dict],
    hierarchy_rows: list[dict],
    findings: list[dict],
) -> list[dict]:
    """
    Build a tree list of nodes for display.
    Each node: {id, title, level, parent_id, children, worst_severity, finding_count}
    """
    # Severity per doc
    doc_severity: dict[str, str] = {}
    doc_count: dict[str, int] = defaultdict(int)
    for f in findings:
        doc_id = f["doc_id"]
        sev = f["severity"]
        doc_count[doc_id] += 1
        if SEVERITY_ORDER.get(sev, 0) > SEVERITY_ORDER.get(doc_severity.get(doc_id, ""), 0):
            doc_severity[doc_id] = sev

    doc_map = {d["id"]: dict(d) for d in docs}
    children_map: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []

    for row in hierarchy_rows:
        node_id = row["id"]
        parent_id = row["parent_id"] or ""
        if parent_id:
            children_map[parent_id].append(node_id)
        else:
            roots.append(node_id)

    # Remove duplicates while preserving order
    seen: set[str] = set()
    unique_roots: list[str] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            unique_roots.append(r)

    def _node(doc_id: str) -> dict:
        doc = doc_map.get(doc_id, {})
        node = {
            "id": doc_id,
            "title": doc.get("title", doc_id),
            "level": doc.get("level", ""),
            "worst_severity": doc_severity.get(doc_id, ""),
            "finding_count": doc_count.get(doc_id, 0),
            "children": [_node(c) for c in children_map.get(doc_id, [])],
        }
        return node

    return [_node(r) for r in unique_roots]


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       font-size: 14px; color: #1f2937; background: #f9fafb; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }

/* Nav */
nav { background: #1e293b; color: #f1f5f9; padding: 12px 24px;
      display: flex; align-items: center; gap: 24px;
      position: sticky; top: 0; z-index: 100; flex-wrap: wrap; }
nav .brand { font-weight: 700; font-size: 15px; color: #f8fafc; }
nav a { color: #94a3b8; font-size: 13px; }
nav a:hover { color: #f1f5f9; text-decoration: none; }

/* Layout */
main { max-width: 1200px; margin: 0 auto; padding: 24px 16px; }
section { margin-bottom: 40px; }
h2 { font-size: 20px; font-weight: 700; color: #111827; margin-bottom: 16px;
     padding-bottom: 8px; border-bottom: 2px solid #e5e7eb; }
h3 { font-size: 15px; font-weight: 600; color: #374151; margin: 20px 0 10px; }

/* Cards */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
         gap: 12px; margin-bottom: 24px; }
.card { border-radius: 8px; padding: 16px; border: 1px solid; }
.card .count { font-size: 32px; font-weight: 800; line-height: 1; }
.card .label { font-size: 12px; font-weight: 600; margin-top: 4px;
               text-transform: uppercase; letter-spacing: .05em; }
.card.critical { background:#fee2e2; border-color:#fca5a5; color:#7f1d1d; }
.card.high     { background:#ffedd5; border-color:#fdba74; color:#7c2d12; }
.card.medium   { background:#fef9c3; border-color:#fde047; color:#713f12; }
.card.low      { background:#dbeafe; border-color:#93c5fd; color:#1e3a8a; }
.card.ok       { background:#dcfce7; border-color:#86efac; color:#14532d; }

/* Badge */
.badge { display:inline-block; padding:2px 8px; border-radius:12px;
         font-size:11px; font-weight:700; text-transform:uppercase; }
.badge.critical { background:#fee2e2; color:#b91c1c; }
.badge.high     { background:#ffedd5; color:#c2410c; }
.badge.medium   { background:#fef9c3; color:#a16207; }
.badge.low      { background:#dbeafe; color:#1d4ed8; }
.badge.compliance  { background:#ede9fe; color:#5b21b6; }
.badge.definitions { background:#e0f2fe; color:#0369a1; }
.badge.style       { background:#f0fdf4; color:#15803d; }

/* Agent summary table */
.agent-summary { width:100%; border-collapse:collapse; margin-bottom:24px; }
.agent-summary th, .agent-summary td {
  text-align:left; padding:8px 12px; border-bottom:1px solid #e5e7eb; }
.agent-summary th { background:#f3f4f6; font-weight:600; font-size:12px;
                    text-transform:uppercase; letter-spacing:.05em; }
.agent-summary tr:hover td { background:#f9fafb; }

/* Findings table */
.findings-table { width:100%; border-collapse:collapse; font-size:13px; }
.findings-table th { background:#f3f4f6; text-align:left; padding:8px 10px;
                     font-size:11px; text-transform:uppercase; letter-spacing:.05em;
                     font-weight:600; border-bottom:2px solid #d1d5db; }
.findings-table td { padding:8px 10px; border-bottom:1px solid #e5e7eb;
                     vertical-align:top; }
.findings-table tr:hover td { background:#f9fafb; }
.findings-table .summary { max-width:340px; }
.findings-table .explanation { max-width:320px; color:#4b5563; font-style:italic; }
.monospace { font-family: ui-monospace, monospace; font-size:12px; color:#374151; }

/* Hierarchy tree */
.hier-tree { list-style:none; padding-left:0; }
.hier-tree ul { list-style:none; padding-left:20px; border-left:2px solid #e5e7eb; margin-left:8px; }
.hier-tree li { padding:4px 0; }
.hier-node { display:inline-flex; align-items:center; gap:8px; }
.doc-level { font-size:10px; font-weight:700; text-transform:uppercase; padding:1px 6px;
             border-radius:4px; color:#fff; }
.doc-level.standard  { background:#1e40af; }
.doc-level.policy    { background:#166534; }
.doc-level.procedure { background:#92400e; }
.doc-level.guideline { background:#6b21a8; }
.finding-dot { display:inline-flex; align-items:center; gap:3px; font-size:11px;
               font-weight:600; padding:1px 6px; border-radius:10px; }
.finding-dot.critical { background:#fee2e2; color:#b91c1c; }
.finding-dot.high     { background:#ffedd5; color:#c2410c; }
.finding-dot.medium   { background:#fef9c3; color:#a16207; }
.finding-dot.low      { background:#dbeafe; color:#1d4ed8; }

/* Per-doc table */
.perdoc-table { width:100%; border-collapse:collapse; font-size:13px; }
.perdoc-table th { background:#f3f4f6; text-align:left; padding:8px 10px;
                   font-size:11px; text-transform:uppercase; letter-spacing:.05em;
                   font-weight:600; border-bottom:2px solid #d1d5db; }
.perdoc-table td { padding:8px 10px; border-bottom:1px solid #e5e7eb; }
.perdoc-table tr:hover td { background:#f9fafb; }
.num { text-align:right; }

/* Empty state */
.empty { color:#6b7280; font-style:italic; padding:16px 0; }
"""


def _e(text: str) -> str:
    """HTML-escape a string."""
    return _html.escape(str(text or ""), quote=True)


def _sev_badge(sev: str) -> str:
    if not sev:
        return ""
    return f'<span class="badge {_e(sev)}">{_e(sev)}</span>'


def _agent_badge(agent: str) -> str:
    return f'<span class="badge {_e(agent)}">{_e(agent)}</span>'


def _render_tree_html(nodes: list[dict]) -> str:
    if not nodes:
        return "<p class='empty'>No documents found.</p>"

    def _li(node: dict) -> str:
        level = node.get("level", "")
        level_span = f'<span class="doc-level {_e(level)}">{_e(level)}</span>' if level else ""
        sev = node.get("worst_severity", "")
        count = node.get("finding_count", 0)
        dot = (
            f'<span class="finding-dot {_e(sev)}">&#9679; {count}</span>'
            if count else ""
        )
        title = _e(node.get("title", node["id"]))
        inner = f'<span class="hier-node">{level_span} {title} {dot}</span>'
        children = node.get("children", [])
        if children:
            inner += "\n<ul>\n" + "".join(_li(c) for c in children) + "</ul>\n"
        return f"<li>{inner}</li>\n"

    return '<ul class="hier-tree">\n' + "".join(_li(n) for n in nodes) + "</ul>"


def _findings_table_html(findings: list[dict]) -> str:
    if not findings:
        return "<p class='empty'>No findings at this severity level.</p>"

    rows = []
    for f in findings:
        sev = f["severity"]
        agent = f["agent"]
        section = _e(f["section"])
        summary = _e(f["summary"])
        explanation = _e(f["explanation"])
        doc = _e(f["doc_title"] or f["doc_id"])

        extra = f.get("extra", {})
        detail_parts = []
        if agent == "compliance":
            if extra.get("parent"):
                detail_parts.append(f"<small>Parent: {_e(extra['parent'])}</small>")
            if extra.get("evidence"):
                detail_parts.append(f"<small>Evidence: {_e(extra['evidence'][:100])}</small>")
            ev_secs = extra.get("evidence_sections", [])
            if ev_secs:
                detail_parts.append(f"<small>Sections searched: {_e(', '.join(ev_secs[:4]))}</small>")
        elif agent == "definitions":
            if extra.get("parent"):
                detail_parts.append(f"<small>vs. {_e(extra['parent'])}</small>")
            if extra.get("parent_definition"):
                detail_parts.append(
                    f"<small>Parent def: {_e(extra['parent_definition'][:100])}</small>"
                )
            if extra.get("child_definition"):
                detail_parts.append(
                    f"<small>Child def: {_e(extra['child_definition'][:100])}</small>"
                )
        elif agent == "style":
            if extra.get("finding_type"):
                detail_parts.append(f"<small>Type: {_e(extra['finding_type'])}</small>")

        detail_html = "<br>".join(detail_parts)
        rows.append(
            f"<tr>"
            f"<td>{_sev_badge(sev)}</td>"
            f"<td>{_agent_badge(agent)}</td>"
            f"<td>{doc}</td>"
            f"<td class='monospace'>{section}</td>"
            f"<td class='summary'>{summary}</td>"
            f"<td class='explanation'>{explanation}</td>"
            f"<td>{detail_html}</td>"
            f"</tr>"
        )

    header = (
        "<tr>"
        "<th>Severity</th><th>Agent</th><th>Document</th>"
        "<th>Section</th><th>Summary</th><th>Detail</th><th>References</th>"
        "</tr>"
    )
    return (
        '<table class="findings-table">'
        + header
        + "".join(rows)
        + "</table>"
    )


def _per_doc_table_html(
    findings: list[dict],
    docs: list[dict],
) -> str:
    counts: dict[str, dict] = {}
    for f in findings:
        did = f["doc_id"]
        if did not in counts:
            counts[did] = {
                "title": f["doc_title"] or did,
                "compliance": 0, "definitions": 0, "style": 0,
                "critical": 0, "high": 0, "medium": 0, "low": 0,
            }
        counts[did][f["agent"]] += 1
        counts[did][f["severity"]] = counts[did].get(f["severity"], 0) + 1

    if not counts:
        return "<p class='empty'>No findings to break down.</p>"

    rows = []
    for did, c in sorted(counts.items(), key=lambda x: -sum(
        x[1].get(s, 0) for s in SEVERITY_ORDER
    )):
        total = c["compliance"] + c["definitions"] + c["style"]
        rows.append(
            f"<tr>"
            f"<td>{_e(c['title'])}</td>"
            f"<td class='num'>{total}</td>"
            f"<td class='num'>"
            + (f'<span class="badge critical">{c["critical"]}</span>' if c["critical"] else "—")
            + "</td>"
            f"<td class='num'>"
            + (f'<span class="badge high">{c["high"]}</span>' if c["high"] else "—")
            + "</td>"
            f"<td class='num'>"
            + (f'<span class="badge medium">{c["medium"]}</span>' if c["medium"] else "—")
            + "</td>"
            f"<td class='num'>"
            + (f'<span class="badge low">{c["low"]}</span>' if c["low"] else "—")
            + "</td>"
            f"<td class='num'>{c['compliance'] or '—'}</td>"
            f"<td class='num'>{c['definitions'] or '—'}</td>"
            f"<td class='num'>{c['style'] or '—'}</td>"
            f"</tr>"
        )

    header = (
        "<tr><th>Document</th><th class='num'>Total</th>"
        "<th class='num'>Critical</th><th class='num'>High</th>"
        "<th class='num'>Medium</th><th class='num'>Low</th>"
        "<th class='num'>Compliance</th><th class='num'>Definitions</th>"
        "<th class='num'>Style</th></tr>"
    )
    return (
        '<table class="perdoc-table">' + header + "".join(rows) + "</table>"
    )


def generate_html_report(
    compliance_report: dict,
    definitions_report: dict,
    style_report: dict,
    docs: list[dict],
    hierarchy_rows: list[dict],
    severity_filter: Optional[str],
    generated_at: str,
    cached_flags: dict[str, bool],
) -> str:
    findings = _normalise_findings(
        compliance_report, definitions_report, style_report, severity_filter
    )

    # Summary counts
    sev_counts: dict[str, int] = defaultdict(int)
    agent_counts: dict[str, int] = defaultdict(int)
    agent_sev: dict[str, dict[str, int]] = {
        "compliance": defaultdict(int),
        "definitions": defaultdict(int),
        "style": defaultdict(int),
    }
    for f in findings:
        sev_counts[f["severity"]] += 1
        agent_counts[f["agent"]] += 1
        agent_sev[f["agent"]][f["severity"]] += 1

    total = len(findings)
    filter_note = (
        f' <small style="color:#6b7280">(filtered to <b>{_e(severity_filter)}+</b>)</small>'
        if severity_filter else ""
    )

    # Summary cards
    cards_html = '<div class="cards">'
    for sev in ("critical", "high", "medium", "low"):
        cnt = sev_counts.get(sev, 0)
        cards_html += (
            f'<div class="card {sev}">'
            f'<div class="count">{cnt}</div>'
            f'<div class="label">{sev}</div>'
            f'</div>'
        )
    cards_html += (
        f'<div class="card ok">'
        f'<div class="count">{total}</div>'
        f'<div class="label">total findings</div>'
        f'</div>'
    )
    cards_html += "</div>"

    # Agent breakdown table
    agent_rows = ""
    for agent in ("compliance", "definitions", "style"):
        s = agent_sev[agent]
        agent_rows += (
            f"<tr>"
            f"<td>{_agent_badge(agent)}</td>"
            f"<td class='num'>{agent_counts.get(agent, 0)}</td>"
            + "".join(
                f"<td class='num'>"
                + (f'<span class="badge {sev}">{s.get(sev, 0)}</span>' if s.get(sev, 0) else "—")
                + "</td>"
                for sev in ("critical", "high", "medium", "low")
            )
            + ("  <td><small style='color:#16a34a'>cached</small></td>" if cached_flags.get(agent) else "<td></td>")
            + "</tr>"
        )
    agent_table = (
        '<table class="agent-summary">'
        "<tr><th>Agent</th><th class='num'>Total</th>"
        "<th class='num'>Critical</th><th class='num'>High</th>"
        "<th class='num'>Medium</th><th class='num'>Low</th>"
        "<th>Cache</th></tr>"
        + agent_rows
        + "</table>"
    )

    # Hierarchy tree
    tree_nodes = _build_hierarchy_tree(docs, hierarchy_rows, findings)
    tree_html = _render_tree_html(tree_nodes)

    # Per-agent findings sections
    def _agent_section(agent: str, section_id: str, title: str) -> str:
        af = [f for f in findings if f["agent"] == agent]
        return (
            f'<section id="{section_id}">'
            f"<h2>{title}</h2>"
            + _findings_table_html(af)
            + "</section>"
        )

    compliance_sec = _agent_section("compliance", "compliance", "Compliance Findings")
    definitions_sec = _agent_section("definitions", "definitions", "Definitions Findings")
    style_sec = _agent_section("style", "style", "Style Findings")

    per_doc_html = _per_doc_table_html(findings, docs)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Governance Audit Report</title>
<style>{_CSS}</style>
</head>
<body>
<nav>
  <span class="brand">govcheck audit</span>
  <a href="#summary">Summary</a>
  <a href="#hierarchy">Hierarchy</a>
  <a href="#compliance">Compliance</a>
  <a href="#definitions">Definitions</a>
  <a href="#style">Style</a>
  <a href="#per-document">By Document</a>
</nav>
<main>
  <section id="summary">
    <h2>Executive Summary{filter_note}</h2>
    <p style="color:#6b7280;margin-bottom:16px">Generated: {_e(generated_at)}</p>
    {cards_html}
    <h3>Findings by Agent</h3>
    {agent_table}
  </section>

  <section id="hierarchy">
    <h2>Document Hierarchy</h2>
    <p style="color:#6b7280;margin-bottom:12px;font-size:12px">
      Coloured dot = worst-severity issue &nbsp;&#9679;&nbsp; number = finding count
    </p>
    {tree_html}
  </section>

  {compliance_sec}
  {definitions_sec}
  {style_sec}

  <section id="per-document">
    <h2>Per-Document Breakdown</h2>
    {per_doc_html}
  </section>
</main>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------

def generate_markdown_report(
    compliance_report: dict,
    definitions_report: dict,
    style_report: dict,
    docs: list[dict],
    hierarchy_rows: list[dict],
    severity_filter: Optional[str],
    generated_at: str,
    cached_flags: dict[str, bool],
) -> str:
    findings = _normalise_findings(
        compliance_report, definitions_report, style_report, severity_filter
    )

    sev_counts: dict[str, int] = defaultdict(int)
    agent_counts: dict[str, dict[str, int]] = {
        "compliance": defaultdict(int),
        "definitions": defaultdict(int),
        "style": defaultdict(int),
    }
    for f in findings:
        sev_counts[f["severity"]] += 1
        agent_counts[f["agent"]][f["severity"]] += 1

    filter_note = f" *(filtered to {severity_filter}+)*" if severity_filter else ""
    lines: list[str] = [
        f"# Governance Audit Report{filter_note}",
        "",
        f"**Generated:** {generated_at}  ",
        "",
        "---",
        "",
        "## Executive Summary",
        "",
        f"| Severity | Count |",
        f"|----------|-------|",
    ]
    for sev in ("critical", "high", "medium", "low"):
        lines.append(f"| {sev.capitalize()} | {sev_counts.get(sev, 0)} |")
    lines += [
        f"| **Total** | **{len(findings)}** |",
        "",
        "### By Agent",
        "",
        "| Agent | Total | Critical | High | Medium | Low | Cached |",
        "|-------|-------|----------|------|--------|-----|--------|",
    ]
    for agent in ("compliance", "definitions", "style"):
        s = agent_counts[agent]
        total_a = agent_counts[agent].get("critical", 0) + agent_counts[agent].get("high", 0) + \
                  agent_counts[agent].get("medium", 0) + agent_counts[agent].get("low", 0)
        cached_str = "yes" if cached_flags.get(agent) else ""
        lines.append(
            f"| {agent} | {total_a} | {s.get('critical',0)} | "
            f"{s.get('high',0)} | {s.get('medium',0)} | {s.get('low',0)} | {cached_str} |"
        )

    # Hierarchy tree (text)
    lines += ["", "---", "", "## Document Hierarchy", ""]

    doc_severity: dict[str, str] = {}
    doc_count_map: dict[str, int] = defaultdict(int)
    for f in findings:
        did = f["doc_id"]
        sev = f["severity"]
        doc_count_map[did] += 1
        if SEVERITY_ORDER.get(sev, 0) > SEVERITY_ORDER.get(doc_severity.get(did, ""), 0):
            doc_severity[did] = sev

    tree_nodes = _build_hierarchy_tree(docs, hierarchy_rows, findings)

    def _tree_md(nodes: list[dict], indent: int = 0) -> list[str]:
        result = []
        for node in nodes:
            prefix = "  " * indent + "- "
            did = node["id"]
            sev = doc_severity.get(did, "")
            count = doc_count_map.get(did, 0)
            indicator = f" ⚠ {count} {sev}" if count else " ✓"
            result.append(
                f"{prefix}**{node['title']}** `[{node['level']}]`{indicator}"
            )
            result.extend(_tree_md(node.get("children", []), indent + 1))
        return result

    lines.extend(_tree_md(tree_nodes))

    # Per-agent findings
    for agent, heading in (
        ("compliance", "Compliance Findings"),
        ("definitions", "Definitions Findings"),
        ("style", "Style Findings"),
    ):
        af = [f for f in findings if f["agent"] == agent]
        lines += ["", "---", "", f"## {heading}", ""]
        if not af:
            lines.append("*No findings at this severity level.*")
            continue

        lines += [
            "| Severity | Document | Section | Summary | Detail |",
            "|----------|----------|---------|---------|--------|",
        ]
        for f in af:
            sev = f["severity"]
            doc = (f["doc_title"] or f["doc_id"]).replace("|", "\\|")
            section = (f["section"] or "").replace("|", "\\|")
            summary = f["summary"][:100].replace("|", "\\|").replace("\n", " ")
            extra = f.get("extra", {})
            detail = ""
            if agent == "compliance":
                detail = f"parent: {extra.get('parent','')}"
            elif agent == "definitions":
                detail = f"type: {extra.get('finding_type','')}"
            elif agent == "style":
                detail = f"type: {extra.get('finding_type','')}"
            detail = detail.replace("|", "\\|")
            lines.append(f"| **{sev}** | {doc} | {section} | {summary} | {detail} |")

    # Per-document breakdown
    lines += ["", "---", "", "## Per-Document Breakdown", ""]
    counts: dict[str, dict] = {}
    for f in findings:
        did = f["doc_id"]
        if did not in counts:
            counts[did] = {
                "title": f["doc_title"] or did,
                "compliance": 0, "definitions": 0, "style": 0,
                "critical": 0, "high": 0, "medium": 0, "low": 0,
            }
        counts[did][f["agent"]] += 1
        counts[did][f["severity"]] = counts[did].get(f["severity"], 0) + 1

    if counts:
        lines += [
            "| Document | Total | Critical | High | Medium | Low | Compliance | Definitions | Style |",
            "|----------|-------|----------|------|--------|-----|------------|-------------|-------|",
        ]
        for did, c in sorted(counts.items(), key=lambda x: -(
            x[1].get("critical", 0) * 8 + x[1].get("high", 0) * 4 +
            x[1].get("medium", 0) * 2 + x[1].get("low", 0)
        )):
            total = c["compliance"] + c["definitions"] + c["style"]
            lines.append(
                f"| {c['title']} | {total} | {c.get('critical',0)} | "
                f"{c.get('high',0)} | {c.get('medium',0)} | {c.get('low',0)} | "
                f"{c['compliance']} | {c['definitions']} | {c['style']} |"
            )
    else:
        lines.append("*No findings to break down.*")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_report(
    compliance_report: dict,
    definitions_report: dict,
    style_report: dict,
    docs: list[dict],
    hierarchy_rows: list[dict],
    severity_filter: Optional[str] = None,
    fmt: str = "html",
) -> str:
    """
    Generate a consolidated audit report.

    Args:
        compliance_report:  Result dict from compliance_checker.run_check_all
        definitions_report: Result dict from definitions_checker.run_check
        style_report:       Result dict from style_checker.run_check_all
        docs:               List of document rows from db.get_all_documents
        hierarchy_rows:     List of hierarchy rows from db.get_hierarchy
        severity_filter:    If set, only include findings >= this severity
                            (one of: critical, high, medium, low)
        fmt:                Output format: "html" or "markdown"

    Returns:
        Rendered report string.
    """
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cached_flags = {
        "compliance": bool(compliance_report.get("cached")),
        "definitions": bool(definitions_report.get("cached")),
        "style": bool(style_report.get("cached")),
    }
    doc_list = [dict(d) for d in docs]

    if fmt == "markdown":
        return generate_markdown_report(
            compliance_report, definitions_report, style_report,
            doc_list, list(hierarchy_rows),
            severity_filter, generated_at, cached_flags,
        )
    return generate_html_report(
        compliance_report, definitions_report, style_report,
        doc_list, list(hierarchy_rows),
        severity_filter, generated_at, cached_flags,
    )
