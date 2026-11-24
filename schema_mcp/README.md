# Schema.org MCP server

Read-only MCP server over Streamable HTTP for the Schema.org vocabulary, with curated Google Search and Merchant Center rules and a three-layer JSON-LD validator. No API key, no Google account.

## Run

```bash
# from the repo root, using the repo's venv (needs only `mcp`, already a dependency)
python schema_mcp/server.py                   # http://127.0.0.1:3002/mcp
MCP_TRANSPORT=stdio python schema_mcp/server.py
```

Connect a client:

```bash
claude mcp add --transport http schema-org http://127.0.0.1:3002/mcp
```

```json
{ "mcpServers": { "schema-org": { "type": "http", "url": "http://127.0.0.1:3002/mcp" } } }
```

## Tools

| Tool | What it returns |
|---|---|
| `get_schema_term` | Definition, hierarchy, domain/range, status (stable / pending / superseded), enum members, Google features that use it |
| `get_type_properties` | Google required / conditional / recommended per feature, Merchant Center only properties, full Schema.org list grouped by declaring type. Every Google group has `source` and `reviewed` |
| `validate_jsonld` | Findings per layer (`schema.org`, `google`, `merchant-center`), each with `severity`, JSONPath `path`, `message`, `fix`, and `source` for Google/MC. Modes: `schema`, `google`, `merchant-center`. Accepts an object, a JSON string, or HTML with `<script type="application/ld+json">` blocks; follows `@graph` and `@id` references |
| `compare_schema_types` | Hierarchy relation, shared / unique properties, Google features, curated verdict for common pairs (Product vs ProductGroup, Offer vs AggregateOffer, Article subtypes, Organization vs LocalBusiness, …) |
| `search_schema_terms` | Name and description search |
| `list_google_features` | The curated feature list with source URLs and review dates |

Every tool result is sent as `structuredContent` and as a JSON text block, so older clients that only read text still work.

## HTTP endpoints

- `POST /mcp` — MCP (stateless, JSON responses; `tools/call` works without an `initialize` round trip)
- `GET /version` — Schema.org version, release date, dataset checksum, freshness
- `GET /health` — liveness
- `GET /.well-known/mcp.json` — discovery metadata

## Configuration

| Variable | Default | |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http` | or `stdio` |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `3002` | Binding to a non-loopback host turns off DNS-rebinding checks unless `MCP_ALLOWED_HOSTS` is set |
| `MCP_ALLOWED_HOSTS` | — | Comma-separated `Host` values to accept, e.g. `mcp.example.com` |
| `SCHEMA_MCP_PUBLIC_URL` | `http://HOST:PORT` | Base URL advertised in `/.well-known/mcp.json` |
| `SCHEMA_MCP_RATE_LIMIT` | `120` | Requests per minute per IP |
| `SCHEMA_MCP_MAX_BODY` | `524288` | Request body cap in bytes (512 KiB) |
| `SCHEMA_MCP_TOOL_TIMEOUT` | `10` | Seconds per tool call |
| `SCHEMA_MCP_TRUST_PROXY` | `false` | Use the first `X-Forwarded-For` hop as the client IP (only behind a proxy you control) |

## Data

- `data/schemaorg-current-https.jsonld`, `data/versions.json` — the official Schema.org release (bundled: 30.1, released 2026-09-16). Update with `python schema_mcp/refresh_data.py`, then restart.
- `data/google_rules.json` — curated rules. Google rich-result rules were checked against the live developers.google.com pages on 2026-11-24. The Merchant Center layer maps the product data specification onto Schema.org by hand and only lists what Merchant Center asks for beyond the merchant-listing rich result. Re-check both when Google changes its docs and bump `reviewed`.

## Tests

```bash
python -m pytest schema_mcp/test_schema_server.py -v
```

No network needed: tests use the bundled vocabulary and Starlette's test client.
