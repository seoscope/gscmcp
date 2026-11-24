"""Tests for the Schema.org MCP server. No network needed: uses the bundled vocabulary."""

import asyncio
import json

import pytest
from starlette.testclient import TestClient

import server
from validator import gtin_valid

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Mcp-Protocol-Version": "2025-06-18",
}


def run(coro):
    return asyncio.run(coro)


def findings(result, layer=None, severity=None):
    return [f for f in result["findings"]
            if (layer is None or f["layer"] == layer) and (severity is None or f["severity"] == severity)]


GOOD_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Trail running shoe",
    "description": "Lightweight trail shoe.",
    "image": ["https://example.com/shoe.jpg"],
    "sku": "TR-1",
    "gtin13": "4006381333931",
    "brand": {"@type": "Brand", "name": "Acme"},
    "offers": {
        "@type": "Offer",
        "price": "89.99",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
        "itemCondition": "https://schema.org/NewCondition",
    },
}


# ------------------------------------------------------------------ vocabulary

def test_vocabulary_loaded():
    info = server.VOCAB.version_info()
    assert info["schema_org_version"]
    assert info["dataset_checksum"].startswith("sha256:")
    assert info["term_counts"]["type"] > 500


def test_get_schema_term_type_and_case_insensitive():
    r = run(server.get_schema_term("product"))
    assert r["name"] == "Product" and r["kind"] == "type"
    assert "Thing" in r["ancestors"]
    assert any(f["id"] == "merchant-listing" for f in r["google_features"])


def test_get_schema_term_property_and_enum():
    r = run(server.get_schema_term("https://schema.org/gtin13"))
    assert r["kind"] == "property" and "Product" in r["domain_includes"]
    e = run(server.get_schema_term("InStock"))
    assert e["kind"] == "enumeration_member" and e["jsonld_value"] == "https://schema.org/InStock"


def test_get_schema_term_unknown_suggests():
    with pytest.raises(Exception, match="Did you mean"):
        run(server.get_schema_term("Prodcut"))


def test_type_properties_groups():
    r = run(server.get_type_properties("Product"))
    ids = {g["id"] for g in r["google"]}
    assert {"product-snippet", "merchant-listing"} <= ids
    ml = next(g for g in r["google"] if g["id"] == "merchant-listing")
    assert ml["required"] == ["name", "image", "offers"]
    mc_paths = {p["path"] for p in r["merchant_center_only"]["properties"]}
    assert "description" in mc_paths
    assert r["merchant_center_only"]["reviewed"]
    assert "Thing" in r["schema_org"]["by_declaring_type"]


def test_type_properties_rejects_property():
    with pytest.raises(Exception, match="not a type"):
        run(server.get_type_properties("price"))


def test_compare_product_productgroup():
    r = run(server.compare_schema_types("Product", "ProductGroup"))
    assert r["hierarchy"]["relation"] == "ProductGroup is a subtype of Product"
    assert "hasVariant" in r["properties"]["only_ProductGroup"]
    assert r["verdict"]["curated"] and "variants" in r["verdict"]["text"]


def test_compare_without_curated_verdict():
    r = run(server.compare_schema_types("Recipe", "Event"))
    assert r["verdict"]["curated"] is False


def test_search():
    r = run(server.search_schema_terms("return policy", kind="type"))
    assert r["results"][0]["name"] in ("MerchantReturnPolicy", "ProductReturnPolicy")


# ------------------------------------------------------------------ validator

def test_valid_product_has_no_errors_in_merchant_mode():
    r = run(server.validate_jsonld(GOOD_PRODUCT, "merchant-center"))
    assert findings(r, severity="error") == []
    assert all(f["eligible"] for f in r["features_checked"])


def test_layers_are_separate():
    doc = {**GOOD_PRODUCT, "colour": "red", "offers": {"@type": "Offer", "price": "$89", "priceCurrency": "USD"}}
    del doc["description"]
    r = run(server.validate_jsonld(doc, "merchant-center"))
    assert any(f["property"] == "colour" for f in findings(r, "schema.org", "error"))
    assert any("price" in f["path"] for f in findings(r, "google", "error"))
    mc = findings(r, "merchant-center", "error")
    assert {f["property"] for f in mc} == {"description", "availability"}
    for f in r["findings"]:
        assert f["path"].startswith("$") and f["message"]


def test_unknown_type_and_wrong_domain():
    r = run(server.validate_jsonld({"@context": "https://schema.org", "@type": "Prodcut", "name": "x"}, "schema"))
    assert "Product" in findings(r, "schema.org", "error")[0]["fix"]
    r = run(server.validate_jsonld({"@context": "https://schema.org", "@type": "Product", "name": "x",
                                    "price": "10"}, "schema"))
    assert any("not a property of Product" in f["message"] for f in findings(r, "schema.org", "error"))


def test_invalid_enum_value():
    doc = {**GOOD_PRODUCT, "offers": {**GOOD_PRODUCT["offers"], "itemCondition": "Brand new"}}
    r = run(server.validate_jsonld(doc, "schema"))
    assert any(f["path"] == "$.offers.itemCondition" for f in findings(r, "schema.org", "error"))


def test_aggregate_offer_rejected_for_merchant_listing_only():
    doc = {**GOOD_PRODUCT, "offers": {"@type": "AggregateOffer", "lowPrice": "10", "priceCurrency": "USD"}}
    google = run(server.validate_jsonld(doc, "google"))
    assert findings(google, "google", "error") == []
    merchant = run(server.validate_jsonld(doc, "merchant-center"))
    assert any("AggregateOffer" in f["message"] for f in findings(merchant, "google", "error"))


def test_missing_context_and_type():
    r = run(server.validate_jsonld('{"name": "x"}', "schema"))
    msgs = " ".join(f["message"] for f in findings(r, "schema.org", "error"))
    assert "@context" in msgs and "@type" in msgs


def test_graph_and_id_references():
    doc = {"@context": "https://schema.org", "@graph": [
        {"@type": "Offer", "@id": "#offer", "price": "5", "priceCurrency": "EUR", "availability": "https://schema.org/InStock"},
        {**{k: v for k, v in GOOD_PRODUCT.items() if k not in ("@context", "offers")}, "offers": {"@id": "#offer"}},
    ]}
    r = run(server.validate_jsonld(doc, "merchant-center"))
    assert findings(r, severity="error") == []


def test_html_input_and_bad_json():
    html = f'<html><script type="application/ld+json">{json.dumps(GOOD_PRODUCT)}</script></html>'
    r = run(server.validate_jsonld(html, "google"))
    assert r["detected_types"] == ["Brand", "Offer", "Product"]
    with pytest.raises(Exception, match="Invalid JSON"):
        run(server.validate_jsonld("{bad", "schema"))


def test_product_group_variants():
    doc = {"@context": "https://schema.org", "@type": "ProductGroup", "name": "Coat", "productGroupID": "C1",
           "variesBy": "https://schema.org/size",
           "hasVariant": [{**{k: v for k, v in GOOD_PRODUCT.items() if k != "@context"}}]}
    r = run(server.validate_jsonld(doc, "merchant-center"))
    ids = {f["id"] for f in r["features_checked"]}
    assert {"product-variants", "merchant-listing"} <= ids
    assert findings(r, severity="error") == []


def test_gtin_check_digit():
    assert gtin_valid("4006381333931")
    assert not gtin_valid("4006381333932")
    assert not gtin_valid("12345")


# ------------------------------------------------------------------ HTTP

@pytest.fixture(scope="module")
def client():
    with TestClient(server.build_app(), base_url="http://127.0.0.1:3002") as c:
        yield c


def test_health_version_discovery(client):
    assert client.get("/health").json() == {"status": "ok"}
    v = client.get("/version").json()
    assert v["schema_org_version"] and v["dataset_checksum"]
    d = client.get("/.well-known/mcp.json").json()
    assert {t["name"] for t in d["tools"]} >= {"get_schema_term", "validate_jsonld", "compare_schema_types",
                                                "get_type_properties"}


def test_tools_call_without_initialize_returns_structured_and_text(client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_schema_term", "arguments": {"term": "Product"}}}
    r = client.post("/mcp", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["structuredContent"]["name"] == "Product"
    assert json.loads(result["content"][0]["text"])["name"] == "Product"


def test_body_limit(client):
    big = "x" * (server.MAX_BODY + 10)
    r = client.post("/mcp", headers=HEADERS, content=big)
    assert r.status_code == 413


def test_rate_limit():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    inner = Starlette(routes=[Route("/health", lambda _: PlainTextResponse("ok"))])
    app = server.LimitsMiddleware(inner, per_minute=3)
    with TestClient(app, base_url="http://127.0.0.1:3002") as c:
        codes = [c.get("/health").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
