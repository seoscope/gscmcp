"""JSON-LD validation in three layers: Schema.org, Google Search, Merchant Center."""

from __future__ import annotations

import html as _html
import json
import re
from typing import Any

from rules import Rules
from vocab import SCHEMA_URL, Vocabulary

MODES = ("schema", "google", "merchant-center")
MODE_ALIASES = {"schema.org": "schema", "schemaorg": "schema", "merchant": "merchant-center",
                "merchant_center": "merchant-center", "merchantcenter": "merchant-center"}

_SCRIPT_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
_ISO_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SCHEMA_PREFIXES = ("https://schema.org/", "http://schema.org/", "schema:")
MAX_NODES = 5000


class InputError(ValueError):
    pass


def _strip(name: str) -> str:
    for p in _SCHEMA_PREFIXES:
        if name.startswith(p):
            return name[len(p):]
    return name


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


def _fmt_path(parts: list) -> str:
    out = "$"
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else f".{p}"
    return out


def gtin_valid(value: str) -> bool:
    digits = re.sub(r"[\s-]", "", value)
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
        return False
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def parse_input(data: Any) -> list[tuple[str, Any]]:
    """Return a list of (label, parsed JSON) blocks from a JSON string, HTML, or object."""
    if isinstance(data, (dict, list)):
        return [("input", data)]
    if not isinstance(data, str) or not data.strip():
        raise InputError("Provide JSON-LD as a JSON object, a JSON string, or HTML containing <script type=\"application/ld+json\"> blocks.")
    text = data.strip()
    if text.startswith("<"):
        blocks = _SCRIPT_RE.findall(text)
        if not blocks:
            raise InputError("No <script type=\"application/ld+json\"> blocks were found in the HTML.")
        out = []
        for i, b in enumerate(blocks):
            try:
                out.append((f"script[{i}]", json.loads(_html.unescape(b.strip()) if "&quot;" in b else b.strip())))
            except json.JSONDecodeError as e:
                raise InputError(f"script[{i}] is not valid JSON: {e.msg} at line {e.lineno} column {e.colno}.") from e
        return out
    try:
        return [("input", json.loads(text))]
    except json.JSONDecodeError as e:
        raise InputError(f"Invalid JSON: {e.msg} at line {e.lineno} column {e.colno}.") from e


class Validator:
    def __init__(self, vocab: Vocabulary, rules: Rules):
        self.vocab = vocab
        self.rules = rules

    # ================================================================ entry

    def validate(self, data: Any, mode: str = "schema", features: list[str] | None = None) -> dict:
        mode = MODE_ALIASES.get(str(mode).lower(), str(mode).lower())
        if mode not in MODES:
            raise InputError(f"Unknown mode '{mode}'. Use one of: {', '.join(MODES)}.")

        self.findings: list[dict] = []
        self._nodes: list[tuple[dict, list, bool, list[str]]] = []  # (node, path, top_level, types)
        self._ids: dict[str, dict] = {}
        self.applied: list[dict] = []

        blocks = parse_input(data)
        multi = len(blocks) > 1
        for label, doc in blocks:
            base = [label] if multi else []
            self._check_context(doc, base)
            for node, path in self._top_nodes(doc, base):
                self._index_ids(node)
                self._walk(node, path, top_level=True, expected=None)

        if mode != "schema" or features:
            self._apply_features(mode, features)

        return self._report(mode)

    # ================================================================ schema layer

    def _add(self, layer, severity, path, message, fix=None, **extra):
        f = {"layer": layer, "severity": severity, "path": _fmt_path(path) if isinstance(path, list) else path,
             "message": message}
        if fix:
            f["fix"] = fix
        f.update({k: v for k, v in extra.items() if v is not None})
        self.findings.append(f)

    def _check_context(self, doc, base):
        docs = doc if isinstance(doc, list) else [doc]
        for i, d in enumerate(docs):
            path = base + ([i] if isinstance(doc, list) else [])
            if not isinstance(d, dict):
                self._add("schema.org", "error", path, "Top-level JSON-LD value must be an object.",
                          "Wrap the data in an object with @context and @type.")
                continue
            ctx = d.get("@context")
            if ctx is None:
                self._add("schema.org", "error", path, "Missing @context, so the properties do not map to Schema.org.",
                          'Add "@context": "https://schema.org".')
                continue
            ctx_text = json.dumps(ctx)
            if "schema.org" not in ctx_text:
                self._add("schema.org", "warning", path + ["@context"],
                          "@context does not reference schema.org.", 'Use "@context": "https://schema.org".')
            elif "http://schema.org" in ctx_text and "https://schema.org" not in ctx_text:
                self._add("schema.org", "info", path + ["@context"],
                          "http://schema.org works, but https://schema.org is the canonical context.",
                          'Use "@context": "https://schema.org".')

    def _top_nodes(self, doc, base):
        items = doc if isinstance(doc, list) else [doc]
        for i, item in enumerate(items):
            path = base + ([i] if isinstance(doc, list) else [])
            if not isinstance(item, dict):
                continue
            if "@graph" in item:
                for j, n in enumerate(_as_list(item["@graph"])):
                    if isinstance(n, dict):
                        yield n, path + ["@graph", j]
            else:
                yield item, path

    def _index_ids(self, node):
        stack = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if isinstance(cur.get("@id"), str) and ("@type" in cur or len(cur) > 1):
                    self._ids.setdefault(cur["@id"], cur)
                stack.extend(v for k, v in cur.items() if k != "@context")
            elif isinstance(cur, list):
                stack.extend(cur)

    def _resolve_types(self, node: dict, path: list, expected: list[str] | None, top_level: bool) -> list[str]:
        raw = node.get("@type")
        if raw is None:
            if top_level:
                self._add("schema.org", "error", path, "Missing @type.", "Add an @type such as \"Product\" or \"Organization\".")
            elif expected and not (set(node) <= {"@id"}):
                self._add("schema.org", "warning", path, f"Missing @type (property expects {' or '.join(expected[:4])}).",
                          f'Add "@type": "{expected[0]}".')
            return []
        valid = []
        for t in _as_list(raw):
            if not isinstance(t, str):
                self._add("schema.org", "error", path + ["@type"], "@type must be a string.")
                continue
            if ":" in t and not t.startswith(_SCHEMA_PREFIXES):
                continue  # another vocabulary; not ours to judge
            name = _strip(t)
            term = self.vocab.resolve(name)
            if term is None:
                sugg = self.vocab.suggest(name, ("type",))
                self._add("schema.org", "error", path + ["@type"], f"'{name}' is not a Schema.org type.",
                          f"Did you mean {', '.join(sugg)}?" if sugg else "Check the type name at https://schema.org/docs/full.html.",
                          type=name)
            elif term.kind == "property":
                self._add("schema.org", "error", path + ["@type"], f"'{name}' is a property, not a type.",
                          f"Use a type from its range: {', '.join(term.range[:5])}." if term.range else None, type=name)
            elif term.kind == "enumeration_member":
                self._add("schema.org", "error", path + ["@type"], f"'{name}' is an enumeration value, not a type.",
                          f"Use it as a property value: \"{term.url}\".", type=name)
            else:
                if term.name != name:
                    self._add("schema.org", "error", path + ["@type"], f"Type names are case-sensitive: '{name}'.",
                              f'Use "{term.name}".', type=name)
                if term.status != "stable":
                    self._add("schema.org", "info", path + ["@type"],
                              f"'{term.name}' is {term.status} in Schema.org and may not be supported by consumers.",
                              type=term.name)
                valid.append(term.name)
        return valid

    def _walk(self, node: dict, path: list, top_level: bool, expected: list[str] | None):
        if len(self._nodes) >= MAX_NODES:
            return
        types = self._resolve_types(node, path, expected, top_level)
        self._nodes.append((node, path, top_level, types))
        allowed = self.vocab.allowed_properties(types) if types else None

        for key, value in node.items():
            if key.startswith("@"):
                if key == "@graph" and not top_level:
                    for j, n in enumerate(_as_list(value)):
                        if isinstance(n, dict):
                            self._walk(n, path + ["@graph", j], True, None)
                continue
            ppath = path + [key]
            if ":" in key and not key.startswith(_SCHEMA_PREFIXES):
                continue
            name = _strip(key)
            prop = self.vocab.resolve(name)
            if prop is None or prop.kind != "property":
                if prop is not None and prop.kind in ("type", "datatype"):
                    msg, fix = f"'{name}' is a type, not a property.", f"Use a property whose range is {prop.name}, e.g. one of: {', '.join(self._props_with_range(prop.name, types)[:5]) or 'see schema.org'}."
                else:
                    sugg = self.vocab.suggest(name, ("property",))
                    msg = f"'{name}' is not a Schema.org property."
                    fix = f"Did you mean {', '.join(sugg)}?" if sugg else "Remove it or move it to an additionalProperty / PropertyValue."
                self._add("schema.org", "error", ppath, msg, fix, property=name)
                continue
            if prop.name != name:
                self._add("schema.org", "error", ppath, f"Property names are case-sensitive: '{name}'.",
                          f'Use "{prop.name}".', property=name)
            if allowed is not None and prop.name not in allowed:
                self._add("schema.org", "error", ppath,
                          f"'{prop.name}' is not a property of {' / '.join(types)}.",
                          f"'{prop.name}' is defined on {', '.join(prop.domain[:6])}. Move it to a nested object of that type or change @type.",
                          property=prop.name, type=types[0])
            if prop.superseded_by:
                self._add("schema.org", "warning", ppath, f"'{prop.name}' is superseded.",
                          f"Use {', '.join(prop.superseded_by)} instead.", property=prop.name)
            elif prop.status in ("pending", "attic"):
                self._add("schema.org", "info", ppath,
                          f"'{prop.name}' is {prop.status} in Schema.org and may not be supported by consumers.",
                          property=prop.name)

            items = _as_list(value)
            for i, item in enumerate(items):
                ipath = ppath + ([i] if isinstance(value, list) else [])
                self._check_value(prop, item, ipath)
                if isinstance(item, dict) and "@value" not in item:
                    self._walk(item, ipath, False, prop.range)

    def _props_with_range(self, type_name: str, types: list[str]) -> list[str]:
        allowed = self.vocab.allowed_properties(types) if types else set()
        return sorted(p for p in allowed if type_name in self.vocab.terms[p].range)

    def _check_value(self, prop, item, path):
        rng = prop.range
        if not rng:
            return
        if isinstance(item, dict):
            if "@value" in item or "@type" not in item:
                return
            item_types = [self.vocab.resolve(_strip(t)) for t in _as_list(item["@type"]) if isinstance(t, str)]
            item_types = [t.name for t in item_types if t is not None and t.kind in ("type", "datatype")]
            if item_types and not any(self.vocab.is_subtype(t, r) for t in item_types for r in rng):
                self._add("schema.org", "warning", path,
                          f"'{prop.name}' expects {' or '.join(rng)}, got {' / '.join(item_types)}.",
                          f"Use one of: {', '.join(rng)}.", property=prop.name)
            return
        if item is None:
            self._add("schema.org", "warning", path, f"'{prop.name}' is null.", "Remove the property or give it a value.",
                      property=prop.name)
            return

        enums = [r for r in rng if r in self.vocab.terms and self.vocab.is_enumeration(r)]
        datatypes = [r for r in rng if r in self.vocab.terms and self.vocab.terms[r].kind == "datatype"]
        textual = any(d in ("Text", "URL") for d in datatypes)

        if enums and isinstance(item, str):
            member = self.vocab.resolve(_strip(item))
            in_enum = member is not None and member.kind == "enumeration_member" and any(
                self.vocab.is_subtype(e, r) for e in member.rdf_types for r in enums)
            if in_enum:
                if not item.startswith(("https://schema.org/", "http://schema.org/")):
                    self._add("schema.org", "info", path, f"Enumeration value '{item}' is not a full URL.",
                              f'Use "{member.url}".', property=prop.name)
                return
            if not textual:
                members = [m for e in enums for m in self.vocab.members_by_enum.get(e, [])]
                self._add("schema.org", "error", path,
                          f"'{item}' is not a valid {' / '.join(enums)} value for '{prop.name}'.",
                          f"Use one of: {', '.join(SCHEMA_URL + m for m in members[:12])}.", property=prop.name)
                return

        if not isinstance(item, str) or textual:
            if isinstance(item, bool) and "Boolean" not in datatypes and datatypes and not enums:
                self._add("schema.org", "warning", path, f"'{prop.name}' expects {' or '.join(rng)}, got a boolean.",
                          property=prop.name)
            return
        only = set(datatypes)
        if not only or len(datatypes) != len(rng):
            return  # range includes object types; plain text is tolerated by Schema.org
        v = item.strip()
        if only <= {"Date"} and not _ISO_DATE.match(v) and not _ISO_DATETIME.match(v):
            self._add("schema.org", "warning", path, f"'{v}' is not an ISO 8601 date.", 'Use "YYYY-MM-DD".', property=prop.name)
        elif only <= {"Date", "DateTime"} and not _ISO_DATETIME.match(v):
            self._add("schema.org", "warning", path, f"'{v}' is not an ISO 8601 date/time.",
                      'Use "YYYY-MM-DD" or "YYYY-MM-DDThh:mm:ss+hh:mm".', property=prop.name)
        elif only <= {"Time"} and not _ISO_TIME.match(v):
            self._add("schema.org", "warning", path, f"'{v}' is not an ISO 8601 time.", 'Use "hh:mm:ss".', property=prop.name)
        elif only <= {"Number", "Integer", "Float"} and not _NUMBER.match(v):
            self._add("schema.org", "warning", path, f"'{v}' is not a number.", "Use digits only, with '.' as the decimal separator.",
                      property=prop.name)
        elif only <= {"Boolean"} and v.lower() not in ("true", "false", "https://schema.org/true", "https://schema.org/false"):
            self._add("schema.org", "warning", path, f"'{v}' is not a boolean.", "Use true or false.", property=prop.name)

    # ================================================================ google / merchant layers

    def _deref(self, value):
        if isinstance(value, dict) and set(value) == {"@id"} and value["@id"] in self._ids:
            return self._ids[value["@id"]]
        return value

    def _values(self, node, key):
        if not isinstance(node, dict):
            return []
        out = []
        for k in (key, "schema:" + key, SCHEMA_URL + key):
            if k in node:
                out.extend(self._deref(v) for v in _as_list(node[k]))
        return [v for v in out if v not in (None, "", [], {})]

    def _has_path(self, node, path: str) -> bool:
        for alt in path.split("|"):
            current = [node]
            segs = alt.split(".")
            for i, seg in enumerate(segs):
                nxt = []
                for c in current:
                    if isinstance(c, dict):
                        nxt.extend(self._values(c, seg))
                    elif isinstance(c, str) and seg == "name" and i == len(segs) - 1:
                        nxt.append(c)  # e.g. "brand": "Acme" is accepted as brand.name
                current = nxt
                if not current:
                    break
            if current:
                return True
        return False

    def _subtree(self, root, root_path):
        """Yield (node, path, types) for root and every nested object, following @id references once."""
        seen = set()
        stack = [(root, root_path)]
        while stack:
            node, path = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            types = [t.name for t in (self.vocab.resolve(_strip(x)) for x in _as_list(node.get("@type", [])) if isinstance(x, str))
                     if t is not None and t.kind == "type"]
            yield node, path, types
            for k, v in node.items():
                if k.startswith("@"):
                    continue
                for i, item in enumerate(_as_list(v)):
                    item = self._deref(item)
                    if isinstance(item, dict) and "@value" not in item:
                        stack.append((item, path + [k] + ([i] if isinstance(v, list) else [])))

    def _apply_features(self, mode: str, requested: list[str] | None):
        if requested:
            unknown = [f for f in requested if f not in self.rules.by_id]
            if unknown:
                raise InputError(f"Unknown feature(s): {', '.join(unknown)}. Call list_google_features for valid ids.")
            features = [self.rules.by_id[f] for f in requested]
        else:
            features = self.rules.features_for_mode(mode)

        done: set[tuple[str, int]] = set()
        for feature in features:
            roots = [(n, p) for n, p, top, types in self._nodes
                     if types and (top or not feature.get("top_level_only"))
                     and any(self.rules.feature_matches_type(feature, t) for t in types)]
            if not roots:
                continue
            errors_before = sum(1 for f in self.findings if f.get("feature") == feature["id"] and f["severity"] == "error")
            for root, rpath in roots:
                for node, path, types in self._subtree(root, rpath):
                    if (feature["id"], id(node)) in done:
                        continue
                    done.add((feature["id"], id(node)))
                    banned = [t for t in types if t in feature.get("disallowed_types", {})]
                    if banned:
                        self._add(feature["layer"], "error", path, feature["disallowed_types"][banned[0]],
                                  "Use Offer instead.", feature=feature["id"], source=feature["source"], type=banned[0])
                        continue
                    key = self.rules.rule_key_for(feature, types)
                    if key is not None:
                        self._check_rule(feature, key, node, path, nested=node is not root)
            errors = sum(1 for f in self.findings if f.get("feature") == feature["id"] and f["severity"] == "error") - errors_before
            self.applied.append({**self.rules.summary(feature), "items_checked": len(roots),
                                 "eligible": errors == 0})

    def _check_rule(self, feature, key, node, path, nested):
        rule = feature["rules"][key]
        layer, fid, src = feature["layer"], feature["id"], feature["source"]
        label = "Merchant Center" if layer == "merchant-center" else f"Google {feature['name'].split(' (')[0]}"
        for p in rule.get("required", []):
            if not self._has_path(node, p):
                self._add(layer, "error", path, f"Missing required property {self._pretty(p)} on {key} for {label}.",
                          f"Add {self._pretty(p)} to the {key}.", feature=fid, source=src, property=p, type=key)
        for c in rule.get("conditional", []):
            if c["path"] == "itemReviewed" and nested:
                continue
            if not self._has_path(node, c["path"]):
                self._add(layer, "warning", path, f"{self._pretty(c['path'])} is missing on {key}. {c['when']}",
                          f"Add {self._pretty(c['path'])} if it applies.", feature=fid, source=src, property=c["path"], type=key)
        missing = [p for p in rule.get("recommended", []) if not self._has_path(node, p)]
        if missing:
            names = ", ".join(self._pretty(p) for p in missing)
            self._add(layer, "warning", path, f"Missing {len(missing)} recommended propert{'y' if len(missing) == 1 else 'ies'} on {key} for {label}: {names}.",
                      "Add the ones that apply to this item; each one improves how the result can be shown.",
                      feature=fid, source=src, property=missing, type=key)
        if layer == "google":  # value checks once, not again in the Merchant Center layer
            self._check_values(feature, key, node, path)

    @staticmethod
    def _pretty(p: str) -> str:
        return " or ".join(f"'{x}'" for x in p.split("|"))

    def _check_values(self, feature, key, node, path):
        layer, fid, src = feature["layer"], feature["id"], feature["source"]
        if key in ("Offer", "UnitPriceSpecification", "AggregateOffer"):
            for prop in ("price", "lowPrice", "highPrice"):
                for v in self._values(node, prop):
                    if isinstance(v, str) and not _NUMBER.match(v.strip()):
                        self._add(layer, "error", path + [prop], f"{prop} '{v}' is not a plain number.",
                                  "Use digits with '.' as the decimal separator and no currency symbols or thousands separators; put the currency in priceCurrency.",
                                  feature=fid, source=src, property=prop, type=key)
                    elif isinstance(v, (int, float)) and not isinstance(v, bool) and v <= 0 and feature["id"] == "merchant-listing":
                        self._add(layer, "warning", path + [prop], f"{prop} is {v}; merchant listings need a price above zero.",
                                  feature=fid, source=src, property=prop, type=key)
            for v in self._values(node, "priceCurrency"):
                if isinstance(v, str) and not _CURRENCY.match(v):
                    self._add(layer, "error", path + ["priceCurrency"], f"priceCurrency '{v}' is not an ISO 4217 code.",
                              'Use a three-letter code such as "USD".', feature=fid, source=src, property="priceCurrency", type=key)
        if key == "Product" and fid == "merchant-listing":
            for prop in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14"):
                for v in self._values(node, prop):
                    if isinstance(v, (str, int)) and not gtin_valid(str(v)):
                        self._add(layer, "warning", path + [prop],
                                  f"{prop} '{v}' is not a valid GTIN (8, 12, 13 or 14 digits with a correct check digit).",
                                  "Use the numeric GTIN from the barcode; do not use the URL form.", feature=fid, source=src,
                                  property=prop, type=key)
        if key == "Product" and fid == "merchant-listing":
            for prop in ("image",):
                for v in self._values(node, prop):
                    if isinstance(v, str) and not v.startswith(("http://", "https://")):
                        self._add(layer, "warning", path + [prop], f"image '{v}' is not an absolute URL.",
                                  "Use a crawlable absolute https:// URL.", feature=fid, source=src, property=prop, type=key)

    # ================================================================ report

    def _report(self, mode):
        order = {"error": 0, "warning": 1, "info": 2}
        layers = {"schema.org": 0, "google": 1, "merchant-center": 2}
        self.findings.sort(key=lambda f: (layers.get(f["layer"], 9), order[f["severity"]], f["path"]))
        counts: dict[str, dict[str, int]] = {}
        for f in self.findings:
            counts.setdefault(f["layer"], {"error": 0, "warning": 0, "info": 0})[f["severity"]] += 1
        detected = sorted({t for _, _, _, types in self._nodes for t in types})
        schema_errors = counts.get("schema.org", {}).get("error", 0)
        return {
            "mode": mode,
            "valid_schema_org": schema_errors == 0,
            "detected_types": detected,
            "summary": counts,
            "features_checked": self.applied,
            "findings": self.findings,
            "schema_org_version": self.vocab.version,
            "rules_reviewed": self.rules.reviewed if mode != "schema" else None,
        }
