import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv()

from .db import (
    DB_PATH,
    find_section,
    get_connection,
    get_all_documents,
    get_document,
    get_hierarchy,
    get_sections,
    init_db,
    upsert_document,
    upsert_hierarchy_node,
    upsert_sections,
)
from .extractor import extract_text
from .hierarchy import load_hierarchy, walk_nodes
from .section_parser import parse_sections
from .tree import build_tree, print_tree


def _write_report(report: dict, base_dir: str, stem: str) -> Path:
    """Write report JSON + Markdown into a timestamped subfolder of base_dir.

    Returns the folder path.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    folder = Path(base_dir) / ts
    folder.mkdir(parents=True, exist_ok=True)

    json_path = folder / f"{stem}.json"
    json_path.write_text(json.dumps(report, indent=2))

    md_path = folder / f"{stem}.md"
    md_path.write_text(_report_to_markdown(report))

    return folder


def _report_to_markdown(report: dict) -> str:
    check_type = report.get("check_type", "report")
    generated_at = report.get("generated_at", "")
    model = report.get("model", "")
    lines: list[str] = []

    if check_type == "compliance":
        lines.append("# Compliance Report")
        lines.append(f"\n**Generated:** {generated_at}  \n**Model:** {model}")

        summary = report.get("summary", {})
        total = summary.get("total_requirements_checked", 0)
        lines.append(f"\n## Summary\n\n**Total requirements checked:** {total}\n")
        by_status = summary.get("by_status", {})
        for status in ("covered", "partially_covered", "not_covered", "contradicted"):
            count = by_status.get(status, 0)
            lines.append(f"- **{status}:** {count}")

        findings = report.get("findings", [])
        if findings:
            lines.append("\n## Findings\n")
            for f in findings:
                status = f.get("status", "")
                req = f.get("requirement_text", "")
                section = f.get("requirement_section", "")
                parent = f.get("parent_doc_title", f.get("parent_doc_id", ""))
                child = f.get("child_doc_title", f.get("child_doc_id", ""))
                explanation = f.get("explanation", "")
                evidence = f.get("evidence", "")
                lines.append(f"### {section} — {status}")
                lines.append(f"\n**Requirement ({parent} → {child}):** {req}\n")
                if explanation:
                    lines.append(f"**Explanation:** {explanation}\n")
                if evidence:
                    lines.append(f"**Evidence:** {evidence}\n")
                evidence_sections = f.get("evidence_sections", [])
                if evidence_sections:
                    lines.append("**Evidence sections:** " + ", ".join(evidence_sections) + "\n")

    elif check_type == "definitions":
        lines.append("# Definitions Consistency Report")
        lines.append(f"\n**Generated:** {generated_at}  \n**Model:** {model}")

        summary = report.get("summary", {})
        total = summary.get("total_findings", 0)
        lines.append(f"\n## Summary\n\n**Total findings:** {total}\n")

        by_severity = summary.get("by_severity", {})
        if by_severity:
            lines.append("### By Severity\n")
            for sev in ("high", "medium", "low"):
                count = by_severity.get(sev, 0)
                lines.append(f"- **{sev}:** {count}")

        by_type = summary.get("by_type", {})
        if by_type:
            lines.append("\n### By Type\n")
            for ftype, count in sorted(by_type.items()):
                lines.append(f"- **{ftype}:** {count}")

        findings = report.get("findings", [])
        if findings:
            lines.append("\n## Findings\n")
            for f in findings:
                term = f.get("term", "")
                ftype = f.get("finding_type", "")
                severity = f.get("severity", "")
                parent = f.get("parent_doc_title", f.get("parent_doc_id", ""))
                child = f.get("child_doc_title", f.get("child_doc_id", ""))
                explanation = f.get("explanation", "")
                lines.append(f"### {term} ({ftype}, {severity})")
                lines.append(f"\n**Documents:** {parent} → {child}\n")
                child_def = f.get("child_definition")
                parent_def = f.get("parent_definition")
                if child_def:
                    lines.append(f"**Child definition** *(section: {child_def.get('section', '')})*: {child_def.get('text', '')}\n")
                if parent_def:
                    lines.append(f"**Parent definition** *(section: {parent_def.get('section', '')})*: {parent_def.get('text', '')}\n")
                if explanation:
                    lines.append(f"**Explanation:** {explanation}\n")
    elif check_type == "style":
        lines.append("# Style Report")
        lines.append(f"\n**Generated:** {generated_at}  \n**Model:** {model}")

        summary = report.get("summary", {})
        total = summary.get("total_findings", 0)
        lines.append(f"\n## Summary\n\n**Total findings:** {total}\n")

        by_severity = summary.get("by_severity", {})
        if by_severity:
            lines.append("### By Severity\n")
            for sev in ("high", "medium", "low"):
                count = by_severity.get(sev, 0)
                lines.append(f"- **{sev}:** {count}")

        by_type = summary.get("by_type", {})
        if by_type:
            lines.append("\n### By Type\n")
            for ftype, count in sorted(by_type.items()):
                lines.append(f"- **{ftype}:** {count}")

        by_doc = summary.get("by_document", {})
        if by_doc:
            lines.append("\n### By Document\n")
            for doc_id, count in sorted(by_doc.items()):
                lines.append(f"- **{doc_id}:** {count}")

        findings = report.get("findings", [])
        if findings:
            lines.append("\n## Findings\n")
            for f in findings:
                ftype = f.get("finding_type", "")
                severity = f.get("severity", "")
                doc_id = f.get("doc_id", "")
                section = f.get("section", "")
                text = f.get("text", "")
                explanation = f.get("explanation", "")
                suggestion = f.get("suggestion", "")
                header = f"{ftype} ({severity})"
                if doc_id:
                    header += f" — {doc_id}"
                if section:
                    header += f" § {section}"
                lines.append(f"### {header}")
                if text:
                    lines.append(f"\n> {text}\n")
                if explanation:
                    lines.append(f"**Explanation:** {explanation}\n")
                if suggestion:
                    lines.append(f"**Suggestion:** {suggestion}\n")
    else:
        lines.append(f"# Report: {check_type}")
        lines.append(f"\n**Generated:** {generated_at}  \n**Model:** {model}")
        lines.append("\n```json")
        lines.append(json.dumps(report, indent=2))
        lines.append("```")

    return "\n".join(lines) + "\n"


def _db_option():
    return click.option(
        "--db",
        default=str(DB_PATH),
        show_default=True,
        help="Path to the SQLite database.",
        type=click.Path(dir_okay=False),
    )


@click.group()
def cli():
    """govcheck — governance document hierarchy checker."""


@cli.command()
@click.option("--docs", required=True, type=click.Path(exists=True, file_okay=False), help="Directory of governance documents.")
@click.option("--hierarchy", "hierarchy_file", required=True, type=click.Path(exists=True, dir_okay=False), help="Path to hierarchy.yaml.")
@_db_option()
def ingest(docs: str, hierarchy_file: str, db: str):
    """Parse documents and store them in the database."""
    docs_dir = Path(docs)
    hierarchy_path = Path(hierarchy_file)
    db_path = Path(db)

    click.echo(f"Loading hierarchy from {hierarchy_path} ...")
    try:
        nodes = load_hierarchy(hierarchy_path)
    except (ValueError, OSError) as e:
        click.echo(f"Error reading hierarchy file: {e}", err=True)
        sys.exit(1)

    conn = get_connection(db_path)
    init_db(conn)

    errors: list[str] = []
    ingested = 0

    for node, parent_id, position in walk_nodes(nodes):
        node_id = node["id"]
        filename = node["file"]
        filepath = docs_dir / filename
        parsing_hint = node.get("parsing_hints", "auto")

        click.echo(f"  Ingesting [{node_id}] {node['title']} ...")

        content: str | None = None
        if not filepath.exists():
            msg = f"    WARNING: file not found: {filepath}"
            click.echo(msg, err=True)
            errors.append(msg)
        else:
            try:
                content = extract_text(filepath)
            except (ValueError, RuntimeError, OSError, Exception) as e:
                msg = f"    WARNING: could not extract text from {filepath}: {e}"
                click.echo(msg, err=True)
                errors.append(msg)

        suffix = filepath.suffix.lower().lstrip(".")
        doc_type = suffix if suffix else "unknown"

        upsert_document(conn, {
            "id": node_id,
            "title": node["title"],
            "filename": filename,
            "doc_type": doc_type,
            "level": node.get("level"),
            "content": content,
        })
        upsert_hierarchy_node(conn, node_id, parent_id, position)

        if content:
            sections = parse_sections(content, hint=parsing_hint)
            upsert_sections(conn, node_id, [
                {"heading": s.heading, "level": s.level, "content": s.content, "position": s.position}
                for s in sections
            ])
            click.echo(f"    Parsed {len(sections)} section(s) (hint: {parsing_hint})")

        ingested += 1

    conn.commit()
    conn.close()

    click.echo(f"\nDone. Ingested {ingested} document(s) into {db_path}.")
    if errors:
        click.echo(f"{len(errors)} warning(s) encountered (see above).", err=True)


@cli.command("tree")
@_db_option()
@click.option("--no-color", is_flag=True, default=False, help="Disable terminal colors.")
def show_tree(db: str, no_color: bool):
    """Print the document hierarchy as a visual tree."""
    db_path = Path(db)

    if not db_path.exists():
        click.echo("Database not found. Run 'govcheck ingest' first.", err=True)
        sys.exit(1)

    conn = get_connection(db_path)
    init_db(conn)

    docs = get_all_documents(conn)
    hierarchy = get_hierarchy(conn)
    conn.close()

    nodes = build_tree(docs, hierarchy)
    use_color = not no_color and sys.stdout.isatty()
    print_tree(nodes, use_color=use_color)


@cli.command("sections")
@click.argument("doc_id")
@_db_option()
def list_sections(doc_id: str, db: str):
    """List all detected sections for a document."""
    db_path = Path(db)

    if not db_path.exists():
        click.echo("Database not found. Run 'govcheck ingest' first.", err=True)
        sys.exit(1)

    conn = get_connection(db_path)
    init_db(conn)

    doc = get_document(conn, doc_id)
    if not doc:
        click.echo(f"Document '{doc_id}' not found.", err=True)
        conn.close()
        sys.exit(1)

    sections = get_sections(conn, doc_id)
    conn.close()

    if not sections:
        click.echo(f"No sections found for '{doc_id}'. The document may have no detectable headings.")
        return

    click.echo(f"Sections for [{doc_id}] {doc['title']}:\n")
    click.echo(f"{'#':<5} {'Lvl':<5} {'Heading':<50} Preview")
    click.echo("-" * 100)
    for s in sections:
        preview = (s["content"] or "").replace("\n", " ").strip()
        preview = preview[:40] + "..." if len(preview) > 40 else preview
        indent = "  " * (s["level"] - 1)
        heading_display = indent + s["heading"]
        heading_display = heading_display[:49]
        click.echo(f"{s['position'] + 1:<5} {s['level']:<5} {heading_display:<50} {preview}")


@cli.group("check")
def check_group():
    """Run consistency checks against the ingested document hierarchy."""


@check_group.command("compliance")
@click.option("--parent", "parent_id", default=None, help="Parent document ID.")
@click.option("--child", "child_id", default=None, help="Child document ID.")
@click.option("--all", "check_all", is_flag=True, default=False,
              help="Check every parent-child pair in the hierarchy.")
@click.option("--output", "-o", default="reports", show_default=True,
              help="Base directory for output; a timestamped subfolder is created inside.")
@_db_option()
def check_compliance(parent_id: str | None, child_id: str | None, check_all: bool,
                     output: str, db: str):
    """Check whether child documents adequately address parent document requirements."""
    from .compliance_checker import run_check_all, run_check_pair

    db_path = Path(db)
    if not db_path.exists():
        click.echo("Database not found. Run 'govcheck ingest' first.", err=True)
        sys.exit(1)

    if not check_all and not (parent_id and child_id):
        click.echo(
            "Specify --parent and --child for a single pair, or use --all for all pairs.",
            err=True,
        )
        sys.exit(1)

    if check_all and (parent_id or child_id):
        click.echo("--all cannot be combined with --parent / --child.", err=True)
        sys.exit(1)

    conn = get_connection(db_path)
    init_db(conn)

    def _progress(msg: str) -> None:
        click.echo(msg)

    click.echo("Extracting requirements and assessing compliance via Claude API ...")
    try:
        if check_all:
            report = run_check_all(conn, progress=_progress)
        else:
            report = run_check_pair(conn, parent_id, child_id, progress=_progress)
    except Exception as e:
        click.echo(f"Error during compliance check: {e}", err=True)
        conn.close()
        sys.exit(1)
    finally:
        conn.close()

    folder = _write_report(report, output, "compliance-report")

    summary = report.get("summary", {})
    total = summary.get("total_requirements_checked", 0)
    click.echo(f"\nDone. {total} requirement(s) checked. Report written to {folder}.")

    by_status = summary.get("by_status", {})
    for status in ("covered", "partially_covered", "not_covered", "contradicted"):
        count = by_status.get(status, 0)
        if count:
            click.echo(f"  {status}: {count}")


@check_group.command("style")
@click.option("--doc", "doc_id", default=None, help="Document ID to check.")
@click.option("--all", "check_all", is_flag=True, default=False,
              help="Check every document in the hierarchy.")
@click.option("--output", "-o", default="reports", show_default=True,
              help="Base directory for output; a timestamped subfolder is created inside.")
@click.option("--config", "config_path", default=None,
              type=click.Path(dir_okay=False),
              help="Path to .govcheck-style.yaml (auto-detected from CWD if omitted).")
@_db_option()
def check_style(doc_id: str | None, check_all: bool, output: str,
                config_path: str | None, db: str):
    """Check writing style consistency and quality.

    Detects mixed modal verbs, readability issues (complex sentences, passive
    voice, ambiguous pronouns), inconsistent terminology, and style drift
    relative to a parent document.

    A .govcheck-style.yaml in the current directory is used automatically
    when present; use --config to specify an alternative location.
    """
    from .style_checker import run_check, run_check_all
    from .style_config import StyleConfig
    from pathlib import Path as _Path

    db_path = Path(db)
    if not db_path.exists():
        click.echo("Database not found. Run 'govcheck ingest' first.", err=True)
        sys.exit(1)

    if not check_all and not doc_id:
        click.echo(
            "Specify --doc <id> for a single document, or use --all for all documents.",
            err=True,
        )
        sys.exit(1)

    if check_all and doc_id:
        click.echo("--all cannot be combined with --doc.", err=True)
        sys.exit(1)

    cfg_path = _Path(config_path) if config_path else None
    config = StyleConfig.load(cfg_path)

    conn = get_connection(db_path)
    init_db(conn)

    def _progress(msg: str) -> None:
        click.echo(msg)

    click.echo("Running style checks ...")
    try:
        if check_all:
            report = run_check_all(conn, config, progress=_progress)
        else:
            report = run_check(conn, doc_id, config, progress=_progress)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        conn.close()
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error during style check: {e}", err=True)
        conn.close()
        sys.exit(1)
    finally:
        conn.close()

    folder = _write_report(report, output, "style-report")

    summary = report.get("summary", {})
    total = summary.get("total_findings", 0)
    click.echo(f"\nDone. {total} finding(s) written to {folder}.")

    by_type = summary.get("by_type", {})
    by_severity = summary.get("by_severity", {})
    if by_type:
        for ftype, count in sorted(by_type.items()):
            click.echo(f"  {ftype}: {count}")
    if by_severity:
        severity_str = "  severity: " + ", ".join(
            f"{k}={v}" for k, v in sorted(by_severity.items())
        )
        click.echo(severity_str)


@check_group.command("definitions")
@click.option("--output", "-o", default="reports", show_default=True,
              help="Base directory for output; a timestamped subfolder is created inside.")
@_db_option()
def check_definitions(output: str, db: str):
    """Check definition consistency across the document hierarchy using the Claude API."""
    from .definitions_checker import run_check

    db_path = Path(db)
    if not db_path.exists():
        click.echo("Database not found. Run 'govcheck ingest' first.", err=True)
        sys.exit(1)

    conn = get_connection(db_path)
    init_db(conn)

    click.echo("Extracting definitions and running semantic comparison via Claude API ...")
    try:
        report = run_check(conn)
    except Exception as e:
        click.echo(f"Error during check: {e}", err=True)
        conn.close()
        sys.exit(1)
    finally:
        conn.close()

    folder = _write_report(report, output, "report")

    summary = report.get("summary", {})
    total = summary.get("total_findings", 0)
    click.echo(f"Done. {total} finding(s) written to {folder}.")

    by_type = summary.get("by_type", {})
    if by_type:
        for ftype, count in sorted(by_type.items()):
            click.echo(f"  {ftype}: {count}")


@cli.command("audit")
@click.option(
    "--all", "audit_all", is_flag=True, default=False,
    help="Run all three checks (compliance, definitions, style) across the full hierarchy.",
)
@click.option(
    "--format", "output_format",
    default="html", show_default=True,
    type=click.Choice(["html", "markdown"], case_sensitive=False),
    help="Output format for the consolidated report.",
)
@click.option(
    "--severity",
    default=None,
    type=click.Choice(["critical", "high", "medium", "low"], case_sensitive=False),
    help="Only include findings at or above this severity level.",
)
@click.option(
    "--output", "-o", default="reports", show_default=True,
    help="Base directory for output; a timestamped subfolder is created inside.",
)
@click.option(
    "--config", "config_path", default=None,
    type=click.Path(dir_okay=False),
    help="Path to .govcheck-style.yaml (auto-detected from CWD if omitted).",
)
@click.option(
    "--clear-cache", "clear_cache", is_flag=True, default=False,
    help="Clear the audit result cache before running.",
)
@_db_option()
def audit(
    audit_all: bool,
    output_format: str,
    severity: str | None,
    output: str,
    config_path: str | None,
    clear_cache: bool,
    db: str,
):
    """Run all checks and produce a consolidated audit report.

    Caches Claude API results per document; unchanged documents are not
    re-analysed on subsequent runs. Use --clear-cache to force a fresh run.
    """
    from .audit_cache import clear_cache as _clear_cache
    from .compliance_checker import run_check_all as compliance_all
    from .definitions_checker import run_check as definitions_check
    from .report_generator import generate_report
    from .style_checker import run_check_all as style_all
    from .style_config import StyleConfig
    from pathlib import Path as _Path

    if not audit_all:
        click.echo(
            "Specify --all to run all checks across the full hierarchy.", err=True
        )
        sys.exit(1)

    db_path = Path(db)
    if not db_path.exists():
        click.echo("Database not found. Run 'govcheck ingest' first.", err=True)
        sys.exit(1)

    cfg_path = _Path(config_path) if config_path else None
    config = StyleConfig.load(cfg_path)

    conn = get_connection(db_path)
    init_db(conn)

    if clear_cache:
        removed = _clear_cache(conn)
        click.echo(f"Cache cleared ({removed} entr{'ies' if removed != 1 else 'y'} removed).")

    def _progress(msg: str) -> None:
        click.echo(msg)

    try:
        click.echo("Running compliance checks ...")
        compliance_report = compliance_all(conn, progress=_progress)

        click.echo("\nRunning definitions checks ...")
        definitions_report = definitions_check(conn)

        click.echo("\nRunning style checks ...")
        style_report = style_all(conn, config, progress=_progress)

        click.echo("\nGenerating report ...")
        all_docs = get_all_documents(conn)
        hierarchy = get_hierarchy(conn)

        report_str = generate_report(
            compliance_report,
            definitions_report,
            style_report,
            docs=list(all_docs),
            hierarchy_rows=list(hierarchy),
            severity_filter=severity,
            fmt=output_format.lower(),
        )
    except Exception as e:
        click.echo(f"Error during audit: {e}", err=True)
        conn.close()
        sys.exit(1)
    finally:
        conn.close()

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    folder = Path(output) / ts
    folder.mkdir(parents=True, exist_ok=True)

    ext = "html" if output_format.lower() == "html" else "md"
    report_path = folder / f"audit-report.{ext}"
    report_path.write_text(report_str, encoding="utf-8")

    # Summary counts for the terminal
    from .report_generator import _normalise_findings, SEVERITY_ORDER
    all_findings = _normalise_findings(
        compliance_report, definitions_report, style_report, severity
    )
    sev_counts: dict[str, int] = {}
    for f in all_findings:
        sev_counts[f["severity"]] = sev_counts.get(f["severity"], 0) + 1

    cached_any = any(
        r.get("cached") for r in (compliance_report, definitions_report, style_report)
    )
    click.echo(f"\nDone. {len(all_findings)} finding(s) written to {report_path}")
    if cached_any:
        click.echo("  (some results served from cache — use --clear-cache to force re-analysis)")
    for sev in ("critical", "high", "medium", "low"):
        cnt = sev_counts.get(sev, 0)
        if cnt:
            click.echo(f"  {sev}: {cnt}")


@cli.command("show")
@click.argument("doc_id")
@click.option("--section", "section_query", default=None, help="Section heading or number to display (e.g. '4.1').")
@_db_option()
def show_document(doc_id: str, section_query: str | None, db: str):
    """Print a document or a specific section."""
    db_path = Path(db)

    if not db_path.exists():
        click.echo("Database not found. Run 'govcheck ingest' first.", err=True)
        sys.exit(1)

    conn = get_connection(db_path)
    init_db(conn)

    doc = get_document(conn, doc_id)
    if not doc:
        click.echo(f"Document '{doc_id}' not found.", err=True)
        conn.close()
        sys.exit(1)

    if section_query is None:
        click.echo(f"[{doc_id}] {doc['title']}\n")
        click.echo(doc["content"] or "(no content)")
        conn.close()
        return

    section = find_section(conn, doc_id, section_query)
    conn.close()

    if not section:
        click.echo(f"Section '{section_query}' not found in '{doc_id}'.", err=True)
        sys.exit(1)

    click.echo(f"[{doc_id}] {doc['title']} — {section['heading']}\n")
    click.echo(section["content"] or "(empty section)")
