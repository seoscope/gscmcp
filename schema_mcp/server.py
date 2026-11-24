"""Schema.org MCP server: public, read-only, Streamable HTTP.

Run:  python schema_mcp/server.py            (http://127.0.0.1:3002/mcp)
      MCP_TRANSPORT=stdio python schema_mcp/server.py
"""

from __future__ import annotations

import os
import sys
import time
from collections import defaultdict, deque
from typing import Annotated, Any, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from rules import Rules
from validator import InputError, Validator
from vocab import Vocabulary

SERVER_NAME = os.environ.get("SCHEMA_MCP_NAME", "schema-org")
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "3002"))
PUBLIC_URL = os.environ.get("SCHEMA_MCP_PUBLIC_URL", f"http://{HOST}:{PORT}").rstrip("/")
RATE_LIMIT = int(os.environ.get("SCHEMA_MCP_RATE_LIMIT", "120"))          # requests per minute per IP
MAX_BODY = int(os.environ.get("SCHEMA_MCP_MAX_BODY", str(512 * 1024)))    # bytes
TOOL_TIMEOUT = float(os.environ.get("SCHEMA_MCP_TOOL_TIMEOUT", "10"))     # seconds
TRUST_PROXY = os.environ.get("SCHEMA_MCP_TRUST_PROXY", "false").lower() == "true"
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]

VOCAB = Vocabulary()
RULES = Rules(VOCAB)

INSTRUCTIONS = (
    "Read-only reference for the Schema.org vocabulary plus curated Google Search and Merchant Center rules. "
    "Use get_schema_term to look up any type, property or enumeration value; get_type_properties to see which "
    "properties Google requires or recommends for a type and which are Merchant Center only; validate_jsonld to "
    "check markup (modes: schema, google, merchant-center); compare_schema_types to choose between two types. "
    "Every Google/Merchant Center answer carries its source URL and review date."
)


def _security() -> TransportSecuritySettings:
    if ALLOWED_HOSTS:
        return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=ALLOWED_HOSTS,
                                         allowed_origins=["*"])
    if HOST in ("127.0.0.1", "localhost", "::1"):
        return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
                                         allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"])
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


mcp = FastMCP(
    SERVER_NAME,
    instructions=INSTRUCTIONS,
    host=HOST,
    port=PORT,
    stateless_http=True,
    json_response=True,
    transport_security=_security(),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


async def _run(fn, *args):
    """Run CPU-bound work off the event loop with the per-call timeout."""
    try:
        with anyio.fail_after(TOOL_TIMEOUT):
            return await anyio.to_thread.run_sync(lambda: fn(*args), abandon_on_cancel=True)
    except TimeoutError as e:
        raise ToolError(f"Tool timed out after {TOOL_TIMEOUT:g} seconds.") from e
    except InputError as e:
        raise ToolError(str(e)) from e


def _require(term: str, kinds: tuple[str, ...] | None = None, what: str = "term"):
    t = VOCAB.resolve(term)
    if t is None or (kinds and t.kind not in kinds):
        sugg = VOCAB.suggest(term, kinds)
        hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
        if t is not None:
            hint = f" '{t.name}' is a {t.kind.replace('_', ' ')}, not a {what}." + hint
        raise InputError(f"'{term}' is not a Schema.org {what}.{hint}")
    return t


# ============================================================================ tools


@mcp.tool(annotations=READ_ONLY)
async def get_schema_term(
    term: Annotated[str, Field(description="Type, property, enumeration value or data type, e.g. 'Product', 'gtin13', 'InStock', 'https://schema.org/Offer'.")],
) -> dict[str, Any]:
    """Look up one Schema.org term: definition, hierarchy, domain/range, status and any Google features that use it."""

    def work():
        t = _require(term)
        out = VOCAB.describe(t)
        if t.kind == "type":
            out["google_features"] = [
                {"id": g["id"], "name": g["name"], "source": g["source"], "reviewed": g["reviewed"]}
                for g in RULES.groups_for_type(t.name)
            ]
        elif t.kind == "property":
            uses = []
            for f in RULES.features:
                for rtype, rule in f["rules"].items():
                    for level in ("required", "conditional", "recommended"):
                        for entry in rule.get(level, []):
                            path = entry["path"] if isinstance(entry, dict) else entry
                            if t.name in {seg for alt in path.split("|") for seg in alt.split(".")}:
                                uses.append({"feature": f["id"], "on_type": rtype, "level": level, "path": path,
                                             "source": f["source"]})
            out["google_usage"] = uses
        out["schema_org_version"] = VOCAB.version
        return out

    return await _run(work)


@mcp.tool(annotations=READ_ONLY)
async def get_type_properties(
    type: Annotated[str, Field(description="Schema.org type, e.g. 'Product'.")],
    include_inherited: Annotated[bool, Field(description="Include properties inherited from parent types.")] = True,
    include_descriptions: Annotated[bool, Field(description="Add a short description to each Schema.org property.")] = False,
) -> dict[str, Any]:
    """Properties of a type, split into Google required / recommended, Merchant Center only, and the full Schema.org list grouped by declaring type."""

    def work():
        t = _require(type, ("type",), "type")
        groups = RULES.groups_for_type(t.name)
        google = [g for g in groups if g["layer"] == "google"]
        mc = [g for g in groups if g["layer"] == "merchant-center"]

        google_paths = {p for g in google for p in g["required"]}
        google_any = google_paths | {p for g in google for p in g["recommended"]}
        mc_only = []
        for g in mc:
            for p in g["required"]:
                mc_only.append({"path": p, "level": "required",
                                "google_level": "recommended" if p in google_any else None, "source": g["source"]})
            for c in g["conditional"]:
                mc_only.append({"path": c["path"], "level": "conditional", "when": c["when"],
                                "google_level": "recommended" if c["path"] in google_any else None, "source": g["source"]})

        def prop_entry(name):
            p = VOCAB.terms[name]
            e = {"name": name, "expects": p.range}
            if p.status != "stable":
                e["status"] = p.status
            if include_descriptions:
                e["description"] = p.comment.split("\n")[0][:200]
            return e

        declared = VOCAB.properties_of(t.name, include_inherited)
        return {
            "type": t.name,
            "url": t.url,
            "ancestors": VOCAB.ancestors(t.name),
            "google": [{k: g[k] for k in ("id", "name", "rule_type", "required", "conditional", "recommended",
                                          "source", "reviewed")} for g in google],
            "merchant_center_only": {
                "note": "Asked for by Merchant Center beyond the Google merchant-listing requirements. google_level shows where Google already lists the same property as recommended.",
                "properties": mc_only,
                "source": mc[0]["source"] if mc else None,
                "reviewed": mc[0]["reviewed"] if mc else None,
            } if mc else None,
            "schema_org": {
                "by_declaring_type": {owner: [prop_entry(n) for n in names] for owner, names in declared.items()},
                "total": sum(len(v) for v in declared.values()),
                "version": VOCAB.version,
            },
            "path_syntax": "'a.b' = nested property, 'x|y' = any one of them",
        }

    return await _run(work)


@mcp.tool(annotations=READ_ONLY)
async def validate_jsonld(
    jsonld: Annotated[Any, Field(description="JSON-LD as an object/array, a JSON string, or an HTML page/fragment containing <script type=\"application/ld+json\"> blocks.")],
    mode: Annotated[Literal["schema", "google", "merchant-center"], Field(description="schema = vocabulary only; google = + Google rich result rules; merchant-center = + merchant listing and Merchant Center rules.")] = "google",
    features: Annotated[list[str] | None, Field(description="Optional feature ids to force (see list_google_features), overriding the mode's defaults.")] = None,
) -> dict[str, Any]:
    """Validate JSON-LD. Returns Schema.org errors, Google findings and Merchant Center findings as separate layers, each with a JSONPath, message and fix."""
    # A fresh Validator per call: it keeps per-run state.
    return await _run(lambda: Validator(VOCAB, RULES).validate(jsonld, mode, features))


@mcp.tool(annotations=READ_ONLY)
async def compare_schema_types(
    type_a: Annotated[str, Field(description="First type, e.g. 'Product'.")],
    type_b: Annotated[str, Field(description="Second type, e.g. 'ProductGroup'.")],
) -> dict[str, Any]:
    """Compare two types: hierarchy relation, shared and unique properties, Google support, and a curated verdict when one exists."""

    def work():
        a = _require(type_a, ("type",), "type")
        b = _require(type_b, ("type",), "type")
        anc_a, anc_b = VOCAB.ancestors(a.name), VOCAB.ancestors(b.name)
        if b.name in anc_a:
            relation = f"{a.name} is a subtype of {b.name}"
        elif a.name in anc_b:
            relation = f"{b.name} is a subtype of {a.name}"
        elif a.name == b.name:
            relation = "same type"
        else:
            relation = "siblings under a common ancestor" if set(anc_a) & set(anc_b) else "unrelated"
        common = next((x for x in [a.name] + anc_a if x == b.name or x in anc_b), None)
        props_a = VOCAB.allowed_properties([a.name])
        props_b = VOCAB.allowed_properties([b.name])
        cmp = RULES.comparison(a.name, b.name)
        return {
            "types": [a.name, b.name],
            "hierarchy": {a.name: [a.name] + anc_a, b.name: [b.name] + anc_b,
                          "relation": relation, "nearest_common_ancestor": common},
            "properties": {
                "shared_count": len(props_a & props_b),
                f"only_{a.name}": sorted(props_a - props_b),
                f"only_{b.name}": sorted(props_b - props_a),
            },
            "google_features": {
                a.name: [g["id"] for g in RULES.groups_for_type(a.name)],
                b.name: [g["id"] for g in RULES.groups_for_type(b.name)],
            },
            "descriptions": {a.name: a.comment.split("\n")[0], b.name: b.comment.split("\n")[0]},
            "verdict": ({"text": cmp["verdict"], "source": cmp["source"], "reviewed": RULES.reviewed, "curated": True}
                        if cmp else {"text": None, "curated": False,
                                     "note": "No curated verdict for this pair; decide from the hierarchy, unique properties and Google features above."}),
        }

    return await _run(work)


@mcp.tool(annotations=READ_ONLY)
async def search_schema_terms(
    query: Annotated[str, Field(description="Words to find in term names and descriptions, e.g. 'return policy'.")],
    kind: Annotated[Literal["any", "type", "property", "enumeration_member", "datatype"], Field(description="Restrict to one kind of term.")] = "any",
    limit: Annotated[int, Field(ge=1, le=50, description="Maximum results.")] = 15,
) -> dict[str, Any]:
    """Search Schema.org terms by name and description, best matches first."""

    def work():
        q = query.strip().lower()
        if not q:
            raise InputError("query must not be empty.")
        words = q.split()
        compact = q.replace(" ", "")
        scored = []
        for t in VOCAB.terms.values():
            if kind != "any" and t.kind != kind:
                continue
            name, desc = t.name.lower(), t.comment.lower()
            score = 0
            if name == compact:
                score = 100
            elif name.startswith(compact):
                score = 60
            elif compact in name:
                score = 40
            elif all(w in name for w in words):
                score = 30
            elif all(w in desc for w in words):
                score = 10
            if score:
                score -= 5 if t.status != "stable" else 0
                scored.append((score, len(t.name), t))
        scored.sort(key=lambda x: (-x[0], x[1], x[2].name))
        return {"query": query, "total_matches": len(scored),
                "results": [VOCAB.describe(t, brief=True) for _, _, t in scored[:limit]]}

    return await _run(work)


@mcp.tool(annotations=READ_ONLY)
async def list_google_features() -> dict[str, Any]:
    """List the curated Google Search / Merchant Center features, their source pages and review dates."""
    return {"reviewed": RULES.reviewed, "notes": RULES.notes,
            "features": [RULES.summary(f) for f in RULES.features]}


# ============================================================================ HTTP routes


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/version", methods=["GET"])
async def version(_: Request) -> JSONResponse:
    return JSONResponse({**VOCAB.version_info(), "rules_reviewed": RULES.reviewed, "server": SERVER_NAME})


@mcp.custom_route("/.well-known/mcp.json", methods=["GET"])
async def discovery(_: Request) -> JSONResponse:
    tools = await mcp.list_tools()
    return JSONResponse({
        "name": SERVER_NAME,
        "description": "Read-only Schema.org vocabulary, Google rich result and Merchant Center rules, and a JSON-LD validator.",
        "version": VOCAB.version,
        "transport": [{"type": "streamable-http", "url": f"{PUBLIC_URL}/mcp"}],
        "authentication": {"type": "none"},
        "readOnly": True,
        "tools": [{"name": t.name, "description": t.description} for t in tools],
        "endpoints": {"mcp": f"{PUBLIC_URL}/mcp", "version": f"{PUBLIC_URL}/version", "health": f"{PUBLIC_URL}/health"},
        "limits": {"requests_per_minute_per_ip": RATE_LIMIT, "max_request_bytes": MAX_BODY,
                   "tool_timeout_seconds": TOOL_TIMEOUT},
    })


# ============================================================================ middleware


class LimitsMiddleware:
    """Per-IP sliding-window rate limit and request body cap (pure ASGI so streaming is untouched)."""

    def __init__(self, app, per_minute: int = RATE_LIMIT, max_body: int = MAX_BODY, trust_proxy: bool = TRUST_PROXY):
        self.app = app
        self.per_minute = per_minute
        self.max_body = max_body
        self.trust_proxy = trust_proxy
        self.hits: dict[str, deque] = defaultdict(deque)

    def _ip(self, scope) -> str:
        if self.trust_proxy:
            for k, v in scope.get("headers", []):
                if k == b"x-forwarded-for":
                    return v.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    async def _reject(self, send, status: int, message: str, headers: list | None = None):
        body = ('{"error": "%s"}' % message).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
                    + (headers or [])})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        ip, now = self._ip(scope), time.monotonic()
        window = self.hits[ip]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            retry = int(60 - (now - window[0])) + 1
            return await self._reject(send, 429, "rate limit exceeded", [(b"retry-after", str(retry).encode())])
        window.append(now)
        if len(self.hits) > 50_000:  # drop idle IPs so memory stays bounded
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 60]:
                del self.hits[k]

        for k, v in scope.get("headers", []):
            if k == b"content-length" and v.isdigit() and int(v) > self.max_body:
                return await self._reject(send, 413, f"request body exceeds {self.max_body} bytes")

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._reject(send, 413, f"request body exceeds {self.max_body} bytes")


class _BodyTooLarge(Exception):
    pass


def build_app():
    from starlette.middleware.cors import CORSMiddleware

    app = mcp.streamable_http_app()
    app = CORSMiddleware(app, allow_origins=["*"], allow_methods=["GET", "POST", "OPTIONS"],
                         allow_headers=["*"], expose_headers=["Mcp-Session-Id", "Mcp-Protocol-Version"])
    return LimitsMiddleware(app)


def main():
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http").lower()
    if transport == "stdio":
        mcp.run("stdio")
        return
    import uvicorn

    print(f"{SERVER_NAME}: Schema.org {VOCAB.version} on http://{HOST}:{PORT}/mcp", file=sys.stderr)
    # The session manager's lifespan lives on the inner Starlette app; uvicorn reaches it through the middleware.
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
