"""Curated Google Search and Merchant Center rules layered on Schema.org."""

from __future__ import annotations

import json
import os
from pathlib import Path

from vocab import DATA_DIR, Vocabulary

DEFAULT_RULES_FILE = DATA_DIR / "google_rules.json"


class Rules:
    def __init__(self, vocab: Vocabulary, path: Path | str | None = None):
        self.vocab = vocab
        self.path = Path(path or os.environ.get("SCHEMA_RULES_PATH") or DEFAULT_RULES_FILE)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.reviewed: str = data.get("reviewed", "")
        self.notes: str = data.get("notes", "")
        self.features: list[dict] = data["features"]
        self.by_id = {f["id"]: f for f in self.features}
        self.comparisons: list[dict] = data.get("comparisons", [])

    # ---------------------------------------------------------------- features

    def feature_matches_type(self, feature: dict, type_name: str) -> bool:
        if any(self.vocab.is_subtype(type_name, ex) for ex in feature.get("exclude_subtypes", [])):
            return False
        return any(self.vocab.is_subtype(type_name, t) for t in feature["applies_to"])

    def rule_key_for(self, feature: dict, type_names: list[str]) -> str | None:
        """Most specific rule key in `feature` that one of `type_names` falls under."""
        best, best_depth = None, -1
        for t in type_names:
            if any(self.vocab.is_subtype(t, ex) for ex in feature.get("exclude_subtypes", [])):
                continue
            for key in feature["rules"]:
                if self.vocab.is_subtype(t, key):
                    depth = len(self.vocab.ancestors(key))
                    if depth > best_depth:
                        best, best_depth = key, depth
        return best

    def features_for_mode(self, mode: str) -> list[dict]:
        return [f for f in self.features if mode in f.get("modes", [])]

    def summary(self, feature: dict) -> dict:
        out = {
            "id": feature["id"],
            "name": feature["name"],
            "layer": feature["layer"],
            "applies_to": feature["applies_to"],
            "source": feature["source"],
            "reviewed": feature.get("reviewed", self.reviewed),
            "validate_modes": feature.get("modes", []),
        }
        if feature.get("basis"):
            out["basis"] = feature["basis"]
        return out

    def groups_for_type(self, type_name: str) -> list[dict]:
        """Requirement groups from every feature whose rules cover `type_name` directly."""
        out = []
        for f in self.features:
            key = self.rule_key_for(f, [type_name])
            if key is None:
                continue
            rule = f["rules"][key]
            out.append({
                **self.summary(f),
                "rule_type": key,
                "required": rule.get("required", []),
                "conditional": rule.get("conditional", []),
                "recommended": rule.get("recommended", []),
            })
        return out

    def comparison(self, a: str, b: str) -> dict | None:
        for c in self.comparisons:
            if {a, b} == set(c["types"]):
                return c
        return None
