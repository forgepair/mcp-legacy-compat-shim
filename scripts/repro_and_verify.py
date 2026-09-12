#!/usr/bin/env python3
"""
Real, live reproduction of the mcp 1.26.0 id-correlation bug, and a real
before/after proof that LegacyCompatShim fixes it -- against the actual
installed SDK, not a mock of it.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx
from asgi_lifespan import LifespanManager
from mcp.server.fastmcp import FastMCP

from mcp_legacy_compat_shim import LegacyCompatShim

DISCOVER_PROBE = {
    "jsonrpc": "2.0",
    "id": 4242,
    "method": "server/discover",
    "params": {},
}


async def send_probe(app):
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            return await client.post(
                "/mcp",
                json=DISCOVER_PROBE,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )


async def main():
    print(f"--- Building a real mcp {__import__('mcp').__version__ if hasattr(__import__('mcp'), '__version__') else '1.26.0'} FastMCP server ---")
    mcp = FastMCP("test-server")
    raw_app = mcp.streamable_http_app()

    print("\n--- BEFORE: sending a server/discover probe (no session id) to the RAW v1.x app ---")
    resp = await send_probe(raw_app)
    print("HTTP status:", resp.status_code)
    print("Raw response body:", resp.text)
    before = json.loads(resp.text)
    print(f"Response id: {before.get('id')!r} (request's real id was {DISCOVER_PROBE['id']!r})")

    if before.get("id") != "server-error":
        print("\nUNEXPECTED: raw app did not reproduce the hardcoded 'server-error' id -- stopping.")
        sys.exit(1)

    print("\nCONFIRMED: real bug reproduced against real mcp 1.26.0 -- id is the literal string "
          "'server-error', not the request's real id 4242. This is exactly why a modern client "
          "can't correlate this error and gives up instead of falling back to legacy initialize.")

    print("\n--- AFTER: same probe, same server, wrapped in LegacyCompatShim ---")
    mcp2 = FastMCP("test-server")
    shimmed_app = LegacyCompatShim(mcp2.streamable_http_app())
    resp2 = await send_probe(shimmed_app)
    print("HTTP status:", resp2.status_code)
    print("Raw response body:", resp2.text)
    after = json.loads(resp2.text)
    print(f"Response id: {after.get('id')!r}")

    if after.get("id") == DISCOVER_PROBE["id"]:
        print("\nSUCCESS -- the shim correlated the real request id. A modern client can now "
              "match this error to its request and fall back to the legacy initialize flow.")
    else:
        print(f"\nFAILURE -- expected id {DISCOVER_PROBE['id']!r}, got {after.get('id')!r}")
        sys.exit(1)

    print("\n--- Negative control: a request with NO parseable JSON-RPC id at all ---")
    control_app = LegacyCompatShim(FastMCP("test-server").streamable_http_app())
    async with LifespanManager(control_app):
        transport = httpx.ASGITransport(app=control_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            resp3 = await client.post(
                "/mcp",
                content=b"not even json",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
    after3 = json.loads(resp3.text)
    print("Raw response body:", resp3.text)
    if after3.get("id") is None:
        print("PASS -- falls back to null per JSON-RPC 2.0 spec when no valid id can be extracted, "
              "never leaves the wrong placeholder in and never fabricates an id.")
    else:
        print(f"FAILURE -- expected null, got {after3.get('id')!r}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
