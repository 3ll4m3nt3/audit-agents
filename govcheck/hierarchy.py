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

        # Validate immutable field if present
        if "immutable" in node:
            if not isinstance(node["immutable"], bool):
                raise ValueError(f"{loc}: 'immutable' must be a boolean, got {type(node['immutable']).__name__}")

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


def build_node_map(nodes: list[dict]) -> dict[str, dict]:
    """
    Build a flat map of {node_id: node_dict} for quick lookup.
    Each node includes metadata: id, title, immutable (default False), parent_id, siblings.
    """
    node_map: dict[str, dict] = {}
    
    def _recurse(node_list: list[dict], parent_id: str | None = None) -> None:
        for node in node_list:
            node_id = node["id"]
            # Immutable defaults to False if not specified
            immutable = node.get("immutable", False)
            
            node_map[node_id] = {
                "id": node_id,
                "title": node.get("title", ""),
                "file": node.get("file", ""),
                "level": node.get("level", ""),
                "immutable": immutable,
                "parent_id": parent_id,
                "sibling_ids": [n["id"] for n in node_list if n["id"] != node_id],
            }
            
            children = node.get("children", [])
            if children:
                _recurse(children, parent_id=node_id)
    
    _recurse(nodes)
    return node_map


def get_immutable_docs(node_map: dict[str, dict]) -> list[str]:
    """Return list of immutable document IDs."""
    return [doc_id for doc_id, info in node_map.items() if info["immutable"]]


def get_mutable_docs(node_map: dict[str, dict]) -> list[str]:
    """Return list of mutable document IDs."""
    return [doc_id for doc_id, info in node_map.items() if not info["immutable"]]


def get_siblings(doc_id: str, node_map: dict[str, dict]) -> list[str]:
    """Return list of sibling document IDs (same parent, different doc)."""
    if doc_id not in node_map:
        return []
    return node_map[doc_id].get("sibling_ids", [])
