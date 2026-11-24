"""Download the latest Schema.org release into schema_mcp/data/.

Usage: python schema_mcp/refresh_data.py
The server reads the files at startup, so restart it afterwards.
"""

import hashlib
import json
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
SOURCES = {
    "schemaorg-current-https.jsonld": "https://schema.org/version/latest/schemaorg-current-https.jsonld",
    "versions.json": "https://raw.githubusercontent.com/schemaorg/schemaorg/main/versions.json",
}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "schema-org-mcp/refresh"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main():
    payloads = {name: fetch(url) for name, url in SOURCES.items()}
    graph = json.loads(payloads["schemaorg-current-https.jsonld"])["@graph"]  # sanity check before writing
    version = json.loads(payloads["versions.json"])["schemaversion"]
    for name, body in payloads.items():
        (DATA / name).write_bytes(body)
    digest = hashlib.sha256(payloads["schemaorg-current-https.jsonld"]).hexdigest()
    print(f"Schema.org {version}: {len(graph)} terms, sha256:{digest}")


if __name__ == "__main__":
    main()
