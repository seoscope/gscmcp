"""Schema.org vocabulary index built from the official JSON-LD release file."""

from __future__ import annotations

import datetime as _dt
import difflib
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_VOCAB_FILE = DATA_DIR / "schemaorg-current-https.jsonld"
DEFAULT_VERSIONS_FILE = DATA_DIR / "versions.json"

SCHEMA_PREFIX = "schema:"
SCHEMA_URL = "https://schema.org/"

# Schema.org data types and the primitive JSON shapes that satisfy them.
DATATYPES = {
    "Text", "URL", "CssSelectorType", "XPathType", "PronounceableText",
    "Number", "Integer", "Float", "Boolean", "Date", "DateTime", "Time", "DataType",
}


def _text(value) -> str:
    """Normalise rdfs:label / rdfs:comment values (string or language map)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("@value", ""))
    if isinstance(value, list):
        for v in value:
            if isinstance(v, dict) and v.get("@language", "en") == "en":
                return str(v.get("@value", ""))
        return _text(value[0]) if value else ""
    return str(value)


def _ids(value) -> list[str]:
    """Return a list of short names from {"@id": "schema:X"} or a list of them."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        ident = item.get("@id") if isinstance(item, dict) else item
        if isinstance(ident, str):
            out.append(_short(ident))
    return out


def _short(ident: str) -> str:
    if ident.startswith(SCHEMA_PREFIX):
        return ident[len(SCHEMA_PREFIX):]
    for base in ("https://schema.org/", "http://schema.org/"):
        if ident.startswith(base):
            return ident[len(base):]
    return ident


def _clean_comment(text: str) -> str:
    # The release file uses [[Term]] wiki links and literal "\n" sequences.
    text = text.replace("\\n", "\n")
    return text.replace("[[", "").replace("]]", "").strip()


@dataclass
class Term:
    name: str
    kind: str  # "type" | "property" | "enumeration_member" | "datatype"
    comment: str
    rdf_types: list[str]
    parents: list[str] = field(default_factory=list)          # types: subClassOf
    domain: list[str] = field(default_factory=list)           # properties: domainIncludes
    range: list[str] = field(default_factory=list)            # properties: rangeIncludes
    superseded_by: list[str] = field(default_factory=list)
    inverse_of: list[str] = field(default_factory=list)
    sub_property_of: list[str] = field(default_factory=list)
    part_of: str | None = None                                # pending / attic / extension
    sources: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return SCHEMA_URL + self.name

    @property
    def status(self) -> str:
        if self.superseded_by:
            return "superseded"
        if self.part_of and "attic" in self.part_of:
            return "attic"
        if self.part_of and "pending" in self.part_of:
            return "pending"
        return "stable"


class Vocabulary:
    def __init__(self, vocab_path: Path | str | None = None, versions_path: Path | str | None = None):
        self.vocab_path = Path(vocab_path or os.environ.get("SCHEMA_VOCAB_PATH") or DEFAULT_VOCAB_FILE)
        self.versions_path = Path(versions_path or os.environ.get("SCHEMA_VERSIONS_PATH") or DEFAULT_VERSIONS_FILE)
        raw = self.vocab_path.read_bytes()
        self.checksum = "sha256:" + hashlib.sha256(raw).hexdigest()
        graph = json.loads(raw.decode("utf-8"))["@graph"]

        self.terms: dict[str, Term] = {}
        self._lower: dict[str, str] = {}
        pending_members: list[tuple[str, dict]] = []

        for node in graph:
            ident = node.get("@id", "")
            if not ident.startswith(SCHEMA_PREFIX):
                continue
            name = _short(ident)
            rdf_types = node.get("@type", [])
            rdf_types = rdf_types if isinstance(rdf_types, list) else [rdf_types]
            if "rdfs:Class" in rdf_types:
                kind = "datatype" if name in DATATYPES or "schema:DataType" in rdf_types else "type"
            elif "rdf:Property" in rdf_types:
                kind = "property"
            else:
                pending_members.append((name, node))
                continue
            self._add(name, kind, node, rdf_types)

        for name, node in pending_members:
            rdf_types = node.get("@type", [])
            rdf_types = rdf_types if isinstance(rdf_types, list) else [rdf_types]
            self._add(name, "enumeration_member", node, [_short(t) for t in rdf_types])

        # Reverse indexes.
        self.children: dict[str, list[str]] = {}
        self.props_by_domain: dict[str, list[str]] = {}
        self.members_by_enum: dict[str, list[str]] = {}
        for t in self.terms.values():
            if t.kind in ("type", "datatype"):
                for p in t.parents:
                    self.children.setdefault(p, []).append(t.name)
            elif t.kind == "property":
                for d in t.domain:
                    self.props_by_domain.setdefault(d, []).append(t.name)
            elif t.kind == "enumeration_member":
                for e in t.rdf_types:
                    self.members_by_enum.setdefault(e, []).append(t.name)
        for d in (self.children, self.props_by_domain, self.members_by_enum):
            for v in d.values():
                v.sort()

        self.version, self.release_date = self._read_version()

    def _add(self, name: str, kind: str, node: dict, rdf_types: list[str]) -> None:
        part_of = node.get("schema:isPartOf")
        self.terms[name] = Term(
            name=name,
            kind=kind,
            comment=_clean_comment(_text(node.get("rdfs:comment"))),
            rdf_types=[_short(t) for t in rdf_types],
            parents=_ids(node.get("rdfs:subClassOf")),
            domain=_ids(node.get("schema:domainIncludes")),
            range=_ids(node.get("schema:rangeIncludes")),
            superseded_by=_ids(node.get("schema:supersededBy")),
            inverse_of=_ids(node.get("schema:inverseOf")),
            sub_property_of=_ids(node.get("rdfs:subPropertyOf")),
            part_of=(part_of.get("@id") if isinstance(part_of, dict) else part_of),
            sources=_ids(node.get("schema:source")),
        )
        self._lower.setdefault(name.lower(), name)

    def _read_version(self) -> tuple[str | None, str | None]:
        try:
            data = json.loads(self.versions_path.read_text(encoding="utf-8"))
            version = data.get("schemaversion")
            return version, data.get("releaseLog", {}).get(version)
        except (OSError, ValueError):
            return None, None

    # ------------------------------------------------------------------ lookup

    def resolve(self, term: str) -> Term | None:
        """Accept 'Product', 'schema:Product', 'https://schema.org/Product' and case variants."""
        if not isinstance(term, str):
            return None
        name = _short(term.strip())
        if name in self.terms:
            return self.terms[name]
        canonical = self._lower.get(name.lower())
        return self.terms.get(canonical) if canonical else None

    def suggest(self, term: str, kinds: tuple[str, ...] | None = None, n: int = 5) -> list[str]:
        name = _short(str(term).strip())
        pool = [t.name for t in self.terms.values() if kinds is None or t.kind in kinds]
        lowered = {p.lower(): p for p in pool}
        hits = difflib.get_close_matches(name.lower(), list(lowered), n=n, cutoff=0.6)
        return [lowered[h] for h in hits]

    def ancestors(self, type_name: str) -> list[str]:
        """Breadth-first ancestors, nearest first, without duplicates."""
        seen: list[str] = []
        queue = list(self.terms[type_name].parents) if type_name in self.terms else []
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            if cur not in self.terms:
                continue  # parent from another vocabulary (e.g. cmns-cls:Classifier)
            seen.append(cur)
            if cur in self.terms:
                queue.extend(self.terms[cur].parents)
        return seen

    def is_subtype(self, child: str, parent: str) -> bool:
        return child == parent or parent in self.ancestors(child)

    def descendants(self, type_name: str) -> list[str]:
        out, queue = [], list(self.children.get(type_name, []))
        while queue:
            cur = queue.pop(0)
            if cur not in out:
                out.append(cur)
                queue.extend(self.children.get(cur, []))
        return out

    def is_enumeration(self, type_name: str) -> bool:
        return self.is_subtype(type_name, "Enumeration") and type_name != "Enumeration"

    def properties_of(self, type_name: str, inherited: bool = True) -> dict[str, list[str]]:
        """Map of declaring type -> property names, own type first."""
        chain = [type_name] + (self.ancestors(type_name) if inherited else [])
        return {t: self.props_by_domain.get(t, []) for t in chain if self.props_by_domain.get(t)}

    def allowed_properties(self, type_names: list[str]) -> set[str]:
        out: set[str] = set()
        for t in type_names:
            for props in self.properties_of(t).values():
                out.update(props)
        return out

    # ------------------------------------------------------------------ metadata

    def version_info(self) -> dict:
        days = None
        if self.release_date:
            try:
                released = _dt.date.fromisoformat(self.release_date)
                days = (_dt.date.today() - released).days
            except ValueError:
                pass
        counts: dict[str, int] = {}
        for t in self.terms.values():
            counts[t.kind] = counts.get(t.kind, 0) + 1
        return {
            "schema_org_version": self.version,
            "release_date": self.release_date,
            "dataset": self.vocab_path.name,
            "dataset_checksum": self.checksum,
            "freshness": {
                "days_since_release": days,
                "note": "Release date of the bundled Schema.org version. Run refresh_data.py to update.",
            },
            "term_counts": counts,
        }

    def describe(self, term: Term, brief: bool = False) -> dict:
        out: dict = {"name": term.name, "kind": term.kind, "url": term.url, "status": term.status}
        if term.superseded_by:
            out["superseded_by"] = term.superseded_by
        if brief:
            out["description"] = term.comment.split("\n")[0][:240]
            return out
        out["description"] = term.comment
        if term.part_of:
            out["part_of"] = term.part_of
        if term.kind in ("type", "datatype"):
            out["parents"] = [p for p in term.parents if p in self.terms]
            out["ancestors"] = self.ancestors(term.name)
            out["subtypes"] = self.children.get(term.name, [])
            if term.kind == "type":
                out["own_property_count"] = len(self.props_by_domain.get(term.name, []))
                out["total_property_count"] = len(self.allowed_properties([term.name]))
            if self.is_enumeration(term.name):
                out["members"] = self.members_by_enum.get(term.name, [])
        elif term.kind == "property":
            out["domain_includes"] = term.domain
            out["range_includes"] = term.range
            if term.inverse_of:
                out["inverse_of"] = term.inverse_of
            if term.sub_property_of:
                out["sub_property_of"] = term.sub_property_of
        elif term.kind == "enumeration_member":
            out["member_of"] = term.rdf_types
            out["jsonld_value"] = term.url
        return out
