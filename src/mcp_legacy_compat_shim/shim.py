"""
ASGI middleware that fixes the MCP Python SDK 1.x id-correlation bug.

The bug (confirmed directly in mcp==1.26.0's
src/mcp/server/streamable_http.py): StreamableHTTPServerTransport's session
and protocol-version checks run BEFORE the request body is ever parsed, so
when they reject a request (missing session id, unsupported protocol
version -- exactly what a 2026-07-28-era client's `server/discover` probe
triggers against a v1.x server), `_create_error_response` has no way to
know the original request's JSON-RPC id and hardcodes the literal string
"server-error" instead. Modern clients (Goose's rmcp >=3.2) cannot
correlate that id with their request and give up instead of falling back
to the legacy `initialize` flow that would otherwise work.

This wraps the ASGI app rather than monkeypatching SDK internals --
reaching into a private method is fragile against the next 1.x point
release, an ASGI wrapper isn't. It peeks at the request body to
extract a real, spec-valid JSON-RPC id -- exactly the mechanism proposed
upstream in modelcontextprotocol/python-sdk#2852 -- and, only if the
wrapped app's response is the hardcoded "server-error" placeholder,
replaces it with the real id (or null, per JSON-RPC 2.0, if none could be
extracted). Every other response passes through untouched.
"""

import json


def extract_raw_request_id(body: bytes):
    """
    Best-effort extraction of a spec-valid JSON-RPC id from a raw request
    body. Only strings and non-bool integers are valid JSON-RPC 2.0 ids
    (matching PR #2852's own extraction rule) -- anything else, or a body
    that isn't parseable JSON at all, returns None.
    """
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    raw_id = parsed.get("id")
    if isinstance(raw_id, str):
        return raw_id
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        return raw_id
    return None


def _fix_hardcoded_id(response_body: bytes, real_id) -> bytes:
    try:
        parsed = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return response_body
    if not isinstance(parsed, dict) or parsed.get("id") != "server-error":
        return response_body
    parsed["id"] = real_id
    return json.dumps(parsed).encode("utf-8")


class LegacyCompatShim:
    """
    ASGI middleware for mcp<2.0 servers. Wrap your app's ASGI callable:

        app = mcp.streamable_http_app()
        app = LegacyCompatShim(app)

    Non-HTTP scopes (lifespan, websocket) pass through untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        body_chunks = []
        more_body = True
        while more_body:
            message = await receive()
            body_chunks.append(message.get("body", b""))
            more_body = message.get("more_body", False)
        body = b"".join(body_chunks)
        real_id = extract_raw_request_id(body)

        replayed = {"sent": False}

        async def replay_receive():
            if not replayed["sent"]:
                replayed["sent"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            # Anything after the one replayed request event is real --
            # e.g. a genuine client disconnect during a long-lived SSE
            # response -- and must come from the real connection, not be
            # fabricated here.
            return await receive()

        response_start = {}
        response_body_parts = []
        # None = undecided (still waiting on http.response.start), then
        # True/False once we know whether to buffer-and-rewrite or just
        # pass through untouched. A streaming (e.g. text/event-stream)
        # response must never be buffered -- doing so holds the whole
        # response hostage until the stream ends, which for a live SSE
        # connection may be never.
        intercept = None

        async def capturing_send(message):
            nonlocal intercept

            if message["type"] == "http.response.start":
                response_start.update(message)
                content_type = next(
                    (v for k, v in message.get("headers", []) if k.lower() == b"content-type"),
                    b"",
                )
                intercept = content_type.split(b";")[0].strip() == b"application/json"
                if not intercept:
                    await send(message)
                return

            if message["type"] == "http.response.body":
                if not intercept:
                    await send(message)
                    return
                response_body_parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    full_body = b"".join(response_body_parts)
                    fixed_body = _fix_hardcoded_id(full_body, real_id)

                    headers = [
                        (name, value)
                        for name, value in response_start.get("headers", [])
                        if name.lower() != b"content-length"
                    ]
                    headers.append((b"content-length", str(len(fixed_body)).encode()))

                    await send({**response_start, "headers": headers})
                    await send({"type": "http.response.body", "body": fixed_body, "more_body": False})
                return

            await send(message)

        await self.app(scope, replay_receive, capturing_send)
