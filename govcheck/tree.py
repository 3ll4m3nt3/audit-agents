import sqlite3
from dataclasses import dataclass, field


LEVEL_COLORS = {
    "standard":  "\033[1;34m",   # bold blue
    "policy":    "\033[1;32m",   # bold green
    "procedure": "\033[1;33m",   # bold yellow
    "guideline": "\033[1;35m",   # bold magenta
}
RESET = "\033[0m"


@dataclass
class TreeNode:
    id: str
    title: str
    filename: str
    level: str | None
    children: list["TreeNode"] = field(default_factory=list)


def build_tree(
    docs: list[sqlite3.Row],
    hierarchy: list[sqlite3.Row],
) -> list[TreeNode]:
    doc_map = {row["id"]: row for row in docs}
    children_map: dict[str | None, list[tuple[int, str]]] = {}

    for row in hierarchy:
        parent = row["parent_id"] or None   # '' → None for root
        children_map.setdefault(parent, []).append((row["position"], row["id"]))

    for key in children_map:
        children_map[key].sort()

    def build_node(node_id: str) -> TreeNode:
        doc = doc_map.get(node_id)
        node = TreeNode(
            id=node_id,
            title=doc["title"] if doc else node_id,
            filename=doc["filename"] if doc else "?",
            level=doc["level"] if doc else None,
        )
        for _, child_id in children_map.get(node_id, []):
            node.children.append(build_node(child_id))
        return node

    roots = children_map.get(None, [])
    return [build_node(node_id) for _, node_id in roots]


def print_tree(nodes: list[TreeNode], use_color: bool = True) -> None:
    if not nodes:
        print("No hierarchy data found. Run 'govcheck ingest' first.")
        return

    _print_nodes(nodes, prefix="", use_color=use_color)


def _print_nodes(nodes: list[TreeNode], prefix: str, use_color: bool) -> None:
    for i, node in enumerate(nodes):
        is_last = i == len(nodes) - 1
        connector = "└── " if is_last else "├── "
        child_prefix = prefix + ("    " if is_last else "│   ")

        level_badge = f"[{node.level}]" if node.level else ""
        color = LEVEL_COLORS.get(node.level or "", "") if use_color else ""
        reset = RESET if use_color else ""

        print(f"{prefix}{connector}{color}{node.title}{reset}  {level_badge}  ({node.filename})")
        _print_nodes(node.children, child_prefix, use_color)
