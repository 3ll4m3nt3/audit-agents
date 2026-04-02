from pathlib import Path
from typing import Any

import yaml


def load_hierarchy(yaml_path: Path) -> list[dict]:
    """Load and validate the hierarchy YAML file. Returns the top-level node list."""
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "hierarchy" not in data:
        raise ValueError("hierarchy.yaml must have a top-level 'hierarchy' key")

    nodes = data["hierarchy"]
    if not isinstance(nodes, list):
        raise ValueError("'hierarchy' must be a list of nodes")

    _validate_nodes(nodes, path="hierarchy")
    return nodes


def _validate_nodes(nodes: list[dict], path: str) -> None:
    required = {"id", "title", "file"}
    seen_ids: set[str] = set()

    for i, node in enumerate(nodes):
        loc = f"{path}[{i}]"
        if not isinstance(node, dict):
            raise ValueError(f"{loc}: each node must be a mapping")

        missing = required - node.keys()
        if missing:
            raise ValueError(f"{loc}: missing required fields: {', '.join(sorted(missing))}")

        node_id = node["id"]
        if node_id in seen_ids:
            raise ValueError(f"{loc}: duplicate id {node_id!r}")
        seen_ids.add(node_id)

        children = node.get("children", [])
        if children:
            _validate_nodes(children, path=f"{loc}.children")


def walk_nodes(nodes: list[dict], parent_id: str | None = None):
    """Yield (node, parent_id, position) tuples in depth-first order."""
    for position, node in enumerate(nodes):
        yield node, parent_id, position
        children = node.get("children", [])
        if children:
            yield from walk_nodes(children, parent_id=node["id"])
