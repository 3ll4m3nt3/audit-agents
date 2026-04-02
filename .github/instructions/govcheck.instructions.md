---
name: govcheck-auto-approve
description: "Use when: working in the govcheck folder or on the CLI tool. Auto-approves tool usage, file edits, and terminal commands without confirmation prompts."
applyTo: ["govcheck/**", "*.py", "cli.py", "hierarchy.py", "section_parser.py", "extractor.py", "db.py"]
---

# Govcheck Auto-Approval Instructions

When working on the govcheck CLI tool, operate with full autonomy:

1. **Apply code changes immediately** — Don't ask before modifying Python files or configuration files. Make the changes and explain what was done.

2. **Run terminal commands directly** — Execute `govcheck` commands, pip installs, and Python scripts without requesting confirmation. Provide output and context.

3. **Assume safe operations** — Within the govcheck scope (CLI, testing, docs), assume that file modifications, terminal commands, and tool invocations are safe to proceed with.

4. **Report changes, don't delay them** — Once complete, summarize what you changed and why, but don't block on approval requests during the work.

5. **Respect destructive operations** — For operations that delete or significantly modify data (dropping databases, removing files), still flag those explicitly before proceeding.

This applies to:
- All `/govcheck/` Python modules
- CLI commands in terminal
- Hierarchy and configuration files
- Testing and validation scripts
