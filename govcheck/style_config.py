"""
Load and validate .govcheck-style.yaml configuration for style checks.

Schema example:

  preferred_modal: "shall"      # flag use of any other mandatory modal

  banned_words:
    - "utilize"
    - "leverage"

  preferred_terms:              # non-preferred: preferred
    "data subject": "individual"
    "end user": "user"

  rules:
    modal_consistency: true
    readability: true
    terminology: true
    parent_consistency: true

  readability:
    max_sentence_words: 40      # sentences over this length are flagged

  modal_groups:                 # override default groupings
    mandatory:  [shall, must]
    recommended: [should]
    permitted:  [may, can]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_CONFIG_NAME = ".govcheck-style.yaml"

_DEFAULTS: dict = {
    "preferred_modal": None,
    "banned_words": [],
    "preferred_terms": {},
    "rules": {
        "modal_consistency": True,
        "readability": True,
        "terminology": True,
        "parent_consistency": True,
    },
    "readability": {
        "max_sentence_words": 40,
    },
    "modal_groups": {
        "mandatory": ["shall", "must"],
        "recommended": ["should"],
        "permitted": ["may", "can"],
    },
}


@dataclass
class StyleConfig:
    preferred_modal: Optional[str]
    banned_words: list[str]
    preferred_terms: dict[str, str]     # non-preferred (lower) -> preferred
    rules: dict[str, bool]
    readability: dict[str, int | float]
    modal_groups: dict[str, list[str]]

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "StyleConfig":
        """Load config from *path*, auto-detecting from CWD if omitted."""
        raw: dict = {}
        if path is None:
            candidate = Path.cwd() / DEFAULT_CONFIG_NAME
            if candidate.exists():
                path = candidate

        if path is not None and path.exists():
            with open(path) as fh:
                raw = yaml.safe_load(fh) or {}

        rules = {**_DEFAULTS["rules"], **raw.get("rules", {})}
        readability = {**_DEFAULTS["readability"], **raw.get("readability", {})}
        modal_groups = {k: list(v) for k, v in _DEFAULTS["modal_groups"].items()}
        if "modal_groups" in raw:
            for group, modals in raw["modal_groups"].items():
                modal_groups[group] = [m.lower() for m in modals]

        preferred_modal = raw.get("preferred_modal", _DEFAULTS["preferred_modal"])
        if preferred_modal:
            preferred_modal = preferred_modal.lower()

        return cls(
            preferred_modal=preferred_modal,
            banned_words=[w.lower() for w in raw.get("banned_words", [])],
            preferred_terms={
                k.lower(): v for k, v in raw.get("preferred_terms", {}).items()
            },
            rules=rules,
            readability=readability,
            modal_groups=modal_groups,
        )

    def mandatory_modals(self) -> list[str]:
        return self.modal_groups.get("mandatory", ["shall", "must"])
