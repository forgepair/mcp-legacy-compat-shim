# mcp-legacy-compat-shim

A small ASGI middleware that fixes a specific interop bug in the MCP
(Model Context Protocol) Python SDK's 1.x line, without requiring the
full, breaking 1.x-to-2.x SDK migration.

## Why I built this

On 2026-07-28 the MCP spec dropped the old `initialize`/`initialized`
handshake for a stateless `server/discover` negotiation. Every server
built against the 1.x SDK now has to speak two incompatible protocol eras
during the transition -- and there's a specific bug in that SDK line that
makes it worse than it needs to be: when a 1.x server rejects a
`server/discover` probe (missing session ID, unsupported protocol
version -- exactly what happens when a 2026-07-28-era client like Goose
>=1.50 talks to it), the JSON-RPC error response hardcodes its `id` field
to the literal string `"server-error"` instead of echoing back the
request's real id. Modern clients can't correlate an error with an
unmatched id, so instead of falling back to the legacy `initialize` flow
-- which would work fine -- they just give up.

I found this hitting real projects independently within days of each
other: a real production Docker deployment reporting the exact source
line and version boundary, an unaffiliated hobbyist's MCP server breaking
the same way, AWS's own agent SDK hard-pinned below 2.0 because it can't
negotiate the new era at all, and Claude Code itself getting rejected by
a v1.x server, patched by hand as a one-off. The real fix already shipped
in `mcp` 2.2.0 -- but 2.x is a genuine breaking rewrite (`FastMCP` renamed,
a new unified `Client`, a new dependency-injection model), and the SDK's
own release notes say v1.x is in maintenance mode for security fixes
only. Projects not ready for that migration are stuck with this bug
indefinitely unless something outside the SDK patches it.

## What it does

Wraps a v1.x server's ASGI app. On every request, it peeks at the body
for a real, spec-valid JSON-RPC id (a string, or a non-boolean integer --
matching the rule in the SDK's own proposed upstream fix), replays the
request unchanged, and -- only if the response comes back with the
hardcoded `"server-error"` placeholder -- swaps in the real id, or `null`
per the JSON-RPC 2.0 spec if none could be extracted. Every other
response passes through completely untouched.

It's a wrapper around the app, not a monkeypatch of SDK internals --
deliberately, so it doesn't break on the SDK's next point release the way
reaching into a private method would.

## Install

```
pip install mcp-legacy-compat-shim
```

## Use

```python
from mcp.server.fastmcp import FastMCP
from mcp_legacy_compat_shim import LegacyCompatShim

mcp = FastMCP("my-server")
app = LegacyCompatShim(mcp.streamable_http_app())
```

## Verification

CI runs the test suite against `mcp` 1.20.0, 1.26.0, and 1.30.0 (the floor
and ceiling of the declared `mcp>=1.20,<2` range, plus the version this
shim was originally built against) across Python 3.10 and 3.12 -- every
combination against the real, installed SDK, not a mock of it. The test
suite reproduces the actual bug first (a raw v1.x app really
does return `"id": "server-error"`, discarding the real request id), then
proves the shim fixes it (the same request, same server, wrapped, returns
the real id), including edge cases that are easy to get wrong: an id of
`0` (falsy in Python, but a valid JSON-RPC id), a string id, a `true`
value (a bool is technically an `int` subclass in Python and has to be
explicitly excluded), an unparseable body falling back to `null`, and a
regression check that a normal successful request comes back byte-for-byte
identical whether or not the shim is wrapped around it.

## Known open items

- Only adds a minimal path for legacy servers to survive a
  `server/discover` probe long enough to fall back -- doesn't implement
  the new protocol era's other features (`subscriptions/listen`,
  multi-round-trip resolvers).
- Not yet published to PyPI.

## License

MIT
