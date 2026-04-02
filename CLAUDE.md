# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -e .
```

This installs the `govcheck` CLI entry point in editable mode. Requires Python 3.11+.

Set `ANTHROPIC_API_KEY` in your environment (or in a `.env` file) before running any `check` or `audit` commands.

## CLI Commands

```bash
# Ingest documents from a directory using a hierarchy definition
govcheck ingest --docs <dir> --hierarchy <hierarchy.yaml> [--db <path>]

# Display the document hierarchy as a colored tree
govcheck tree [--db <path>] [--no-color]

# List all detected sections for a document
govcheck sections <doc_id> [--db <path>]

# Print a document or a specific section
govcheck show <doc_id> [--section "4.1"] [--db <path>]

# Compliance check: verify mutable documents conform to immutable references (Claude API)
govcheck check compliance --parent <id> --child <id> [--output <dir>] [--db <path>]
govcheck check compliance --all [--output <dir>] [--db <path>]
# Note: --parent is source doc (reference), --child is target doc (to check)

# Definitions check: find inconsistent term definitions across the hierarchy (Claude API)
govcheck check definitions [--output <dir>] [--db <path>]
#   --extraction-mode / -m  glossary (default) | inline | semantic
#   Can be repeated to combine: -m glossary -m semantic

# Style check: writing quality and consistency (Claude API + regex)
govcheck check style --doc <id> [--config <path>] [--output <dir>] [--db <path>]
govcheck check style --all   [--config <path>] [--output <dir>] [--db <path>]

# Full audit: run all three checks and produce a consolidated report
govcheck audit --all [--format html|markdown] [--severity critical|high|medium|low]
               [--config <path>] [--output <dir>] [--clear-cache] [--db <path>]
```

Default database location: `~/.govcheck/govcheck.db`

Reports are written to timestamped subfolders under `--output` (default: `reports/`).

## Architecture

**Data flow:**
```
hierarchy.yaml
    → hierarchy.py       parse & validate
    → cli.py             orchestrate
    → extractor.py       PDF / DOCX / MD / TXT → plain text
    → section_parser.py  split into sections (numbered / markdown / caps / auto)
    → db.py              SQLite storage
    → tree.py            colored hierarchy display
    → *_extractor.py     pull requirements / definitions from stored content
    → *_checker.py       Claude API analysis (compliance / definitions / style)
    → report_generator.py  HTML or Markdown consolidated report
```

**Key modules:**
- `govcheck/cli.py` — Click CLI: `ingest`, `tree`, `sections`, `show`, `check compliance`, `check definitions`, `check style`, `audit`
- `govcheck/hierarchy.py` — Loads and validates the YAML hierarchy; builds node map with immutability and sibling relationships
- `govcheck/extractor.py` — Extracts text from PDF (pymupdf), DOCX (python-docx), Markdown, and plain text
- `govcheck/section_parser.py` — Splits document text into sections by detecting heading patterns (`numbered`, `markdown`, `caps`); defaults to auto-detection
- `govcheck/db.py` — SQLite with four tables: `documents`, `hierarchy`, `sections`, `audit_cache`
- `govcheck/tree.py` — Builds in-memory tree from DB and renders with colored output
- `govcheck/requirements_extractor.py` — Extracts obligation sentences ("shall", "must", "is required to") from source documents
- `govcheck/compliance_checker.py` — Immutability-based checking: each mutable doc is checked against all immutable docs (conformance), and sibling pairs are checked for consistency (sibling_consistency). Batched Claude API calls classify each requirement as covered / partially_covered / not_covered / contradicted. `partially_covered` maps to the `review` severity in report output.
- `govcheck/definitions_extractor.py` — Three-mode term extraction: `glossary` (centralised sections, default), `inline` (loose patterns across all sections: parentheticals, `**Term**: ...`, "hereafter referred to as", AKA aliases), `semantic` (Claude API contextual extraction). Controlled via `ExtractionMode` enum and the `mode=` parameter of `extract_definitions()`. Modes can be combined; results are merged and de-duplicated by (term_lower, section_id).
- `govcheck/definitions_checker.py` — Semantic comparison of term definitions across the hierarchy (Claude API), plus missing/orphan definition checks (regex)
- `govcheck/style_checker.py` — Hybrid style analysis: modal consistency, banned words, preferred terms (regex); readability and terminology consistency (Claude API)
- `govcheck/style_config.py` — Loads `.govcheck-style.yaml`; merges with defaults
- `govcheck/audit_cache.py` — SHA-256-keyed SQLite cache for Claude API results; avoids re-running unchanged documents
- `govcheck/report_generator.py` — Generates HTML or Markdown consolidated audit reports. HTML reports include in-browser filter controls (severity, document, keyword) on each findings table. Severity levels: `critical`, `high`, `medium`, `low`, `review` (mapped from `partially_covered` compliance status — purple, indicates human judgement required).

**Database schema:**
- `documents` — `id`, `title`, `filename`, `doc_type`, `level`, `content`, `ingested_at`
- `hierarchy` — `id`, `parent_id`, `position` (parent-child relationships; root nodes have `parent_id = ''`)
- `sections` — `section_id`, `doc_id`, `heading`, `level`, `content`, `position`
- `audit_cache` — `cache_key`, `check_type`, `result_json`, `created_at`

**hierarchy.yaml schema:** Each node has `id`, `title`, `file`, `level` (standard/policy/procedure/guideline), optional `immutable` (boolean, default false), optional `children`, and optional `parsing_hints` (one of `numbered`, `markdown`, `caps`, `auto`). The root list is under the `hierarchy` key.

**Immutability model:** Documents marked `immutable: true` (regulations, standards, high-level policies) are treated as reference sources that others must conform to. Documents marked `immutable: false` (your own policies and procedures) are checked for conformance to immutable docs, and sibling pairs are checked against each other for consistency.

**Section parsing hints:**
- `numbered` — detects headings like `4.1 Introduction` or `A.2.3 Scope`; nesting level derived from dot-count
- `markdown` — detects ATX headings (`#`, `##`, etc.); nesting level from hash count
- `caps` — detects ALL-CAPS lines as level-1 headings
- `auto` (default) — counts candidates for each pattern and picks the best fit

**Claude API model:** Hardcoded as `MODEL = "claude-sonnet-4-20250514"` in `compliance_checker.py`, `definitions_checker.py`, and `style_checker.py`. Update these constants to change the model.

**Caching:** The `audit_cache` table stores results keyed by SHA-256 hash of document content. Unchanged documents are served from cache on subsequent runs. Use `govcheck audit --clear-cache` to force a fresh run.

## Example hierarchy.yaml

```yaml
hierarchy:
  - id: iso27001
    title: "ISO 27001:2022"
    file: "iso27001.pdf"
    level: "standard"
    immutable: true              # Reference standard; others must conform
    parsing_hints: "numbered"
    children:
      - id: infosec-policy
        title: "Company InfoSec Policy"
        file: "infosec-policy.docx"
        level: "policy"
        immutable: false          # Can change to stay consistent
        parsing_hints: "markdown"
        children:
          - id: access-control-proc
            title: "Access Control Procedure"
            file: "access-control.docx"
            level: "procedure"
            immutable: false
```
