"""
Real, end-to-end tests against the actual installed mcp 1.x SDK -- no
mocking of the SDK itself. Each test builds a real FastMCP server and
drives it over ASGI with httpx, the same way a real client would.
"""

import json

import httpx
import pytest
from asgi_lifespan import LifespanManager
from mcp.server.fastmcp import FastMCP

from mcp_legacy_compat_shim import LegacyCompatShim, extract_raw_request_id

BASE_URL = "http://localhost:8000"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


async def post(app, **kwargs):
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            return await client.post("/mcp", headers=HEADERS, **kwargs)


@pytest.mark.asyncio
async def test_raw_sdk_reproduces_the_real_bug(make_probe):
    """
    Sanity check that documents WHY this package exists: the installed
    mcp 1.x SDK, unmodified, really does hardcode the error id.
    """
    app = FastMCP("test-server").streamable_http_app()
    resp = await post(app, json=make_probe(4242))

    assert resp.status_code == 400
    body = json.loads(resp.text)
    assert body["id"] == "server-error"


@pytest.mark.asyncio
async def test_shim_correlates_the_real_request_id(make_probe):
    app = LegacyCompatShim(FastMCP("test-server").streamable_http_app())
    resp = await post(app, json=make_probe(4242))

    assert resp.status_code == 400
    body = json.loads(resp.text)
    assert body["id"] == 4242


@pytest.mark.asyncio
async def test_shim_correlates_a_string_id(make_probe):
    app = LegacyCompatShim(FastMCP("test-server").streamable_http_app())
    resp = await post(app, json=make_probe("req-abc-123"))

    body = json.loads(resp.text)
    assert body["id"] == "req-abc-123"


@pytest.mark.asyncio
async def test_shim_correlates_id_zero(make_probe):
    """id: 0 is falsy in Python but a perfectly valid JSON-RPC id -- must
    not be mistaken for "no id present"."""
    app = LegacyCompatShim(FastMCP("test-server").streamable_http_app())
    resp = await post(app, json=make_probe(0))

    body = json.loads(resp.text)
    assert body["id"] == 0


@pytest.mark.asyncio
async def test_shim_falls_back_to_null_on_unparseable_body():
    app = LegacyCompatShim(FastMCP("test-server").streamable_http_app())
    resp = await post(app, content=b"not even json")

    body = json.loads(resp.text)
    assert body["id"] is None


@pytest.mark.asyncio
async def test_shim_falls_back_to_null_when_id_is_wrong_type(make_probe):
    """A bool or a float is not a spec-valid JSON-RPC id (bool is a subtype
    of int in Python, so this specifically guards against that trap)."""
    app = LegacyCompatShim(FastMCP("test-server").streamable_http_app())
    resp = await post(app, json=make_probe(True))

    body = json.loads(resp.text)
    assert body["id"] is None


@pytest.mark.asyncio
async def test_shim_never_touches_a_successful_response(make_probe):
    """
    Regression guard: the shim must be a no-op for ordinary traffic, not
    just for the one bug it's designed to fix.
    """
    app = LegacyCompatShim(FastMCP("test-server").streamable_http_app())
    unshimmed = FastMCP("test-server").streamable_http_app()

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.0.1"},
        },
    }

    shimmed_resp = await post(app, json=initialize)
    plain_resp = await post(unshimmed, json=initialize)

    # A real initialize response is SSE (text/event-stream), not JSON --
    # compare status, content-type, and raw body directly rather than
    # assuming JSON.
    assert shimmed_resp.status_code == plain_resp.status_code
    assert shimmed_resp.headers["content-type"] == plain_resp.headers["content-type"]
    assert shimmed_resp.headers["content-type"] == "text/event-stream"
    assert shimmed_resp.content == plain_resp.content


def test_extract_raw_request_id_accepts_only_spec_valid_ids():
    assert extract_raw_request_id(b'{"id": "abc"}') == "abc"
    assert extract_raw_request_id(b'{"id": 42}') == 42
    assert extract_raw_request_id(b'{"id": 0}') == 0
    assert extract_raw_request_id(b'{"id": true}') is None
    assert extract_raw_request_id(b'{"id": 1.5}') is None
    assert extract_raw_request_id(b'{"id": null}') is None
    assert extract_raw_request_id(b"not json") is None
    assert extract_raw_request_id(b"[]") is None
