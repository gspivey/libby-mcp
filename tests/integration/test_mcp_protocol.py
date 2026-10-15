"""MCP protocol integration tests against a running libby-mcp endpoint.

Black-box HTTP suite: drives a live endpoint (a local container, a bare uvicorn
process, or a remote server) over the MCP Streamable HTTP transport. It does NOT
import ``src`` — the server is treated as an opaque wire endpoint, which keeps
the suite portable across local-docker, local-uvicorn, and remote targets.

Gating (AC-3.3):
- Marked ``@pytest.mark.local_integration`` so the unit job
  (``pytest tests/ --ignore=tests/integration``) never collects it and
  ``-m local_integration`` selects it.
- A module-level ``skipif`` on ``RUN_LOCAL_INTEGRATION != "1"`` means the suite
  is skipped unless that env var is set, so it never runs under the existing
  live-Thunder (``RUN_INTEGRATION``) job or ``make integration``.

Configuration (AC-3.2):
- ``MCP_ENDPOINT`` (default ``http://localhost:8000/mcp``)
- ``MCP_API_KEY`` (default ``ci-test-key``)
- ``MCP_TIMEOUT`` (default ``20`` seconds)

Thunder is hit live (DD-2): assertions use stable identifiers (Going Postal =
149795; slugs ``lcpl``, ``fairfax``) and response structure, never volatile
availability counts.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

ENDPOINT = os.environ.get("MCP_ENDPOINT", "http://localhost:8000/mcp")
API_KEY = os.environ.get("MCP_API_KEY", "ci-test-key")
TIMEOUT = float(os.environ.get("MCP_TIMEOUT", "20"))

# Known-good fixtures (DD-4). `fairfax` verified to return totalItems > 0
# against the live Thunder catalog before implementing the fan-out test.
GOING_POSTAL_ID = "149795"
BOGUS_TITLE_ID = "999999999"
SLUG = "lcpl"
SLUG_2 = "fairfax"
BOGUS_SLUG = "not-a-real-library-xyz"

# v2 enrichment keys that MUST be present on every search_titles title (AC-3.7).
V2_ENRICHMENT_KEYS = (
    "holds_per_copy",
    "estimated_wait_days",
    "series_name",
    "series_position",
    "narrator",
    "publisher",
    "edition",
    "is_abridged",
    "hours",
)

pytestmark = [
    pytest.mark.local_integration,
    pytest.mark.skipif(
        os.environ.get("RUN_LOCAL_INTEGRATION") != "1",
        reason="set RUN_LOCAL_INTEGRATION=1 to run local integration tests",
    ),
]


def _headers(token: str | None) -> dict[str, str]:
    # FastMCP Streamable HTTP requires both JSON and SSE media types in Accept,
    # even when the server replies with JSON (json_response=True); omitting the
    # SSE type yields a 406 from the transport (AC-3.4).
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def mcp_call(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    token: str | None = API_KEY,
    _id: int = 1,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """POST a JSON-RPC 2.0 request to the MCP endpoint and return the response."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    caller = client.post if client is not None else httpx.post
    return caller(ENDPOINT, json=payload, headers=_headers(token), timeout=TIMEOUT)


def notify(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    token: str | None = API_KEY,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """Send a JSON-RPC notification (no ``id`` field; no response body expected)."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    caller = client.post if client is not None else httpx.post
    return caller(ENDPOINT, json=payload, headers=_headers(token), timeout=TIMEOUT)


def tool_payload(resp: httpx.Response) -> dict[str, Any]:
    """Extract a tool result from the MCP envelope.

    Returns one of:
    - ``{"_jsonrpc_error": {...}}`` when the response is a JSON-RPC error.
    - ``{"_is_error": True, "text": "..."}`` when the tool returned an error
      result (``isError: true``); the text is human-readable prose, NOT JSON, so
      it is returned verbatim rather than parsed.
    - the parsed tool JSON (``json.loads`` of ``result.content[0].text``) on
      success.
    """
    data = resp.json()
    if "error" in data:
        return {"_jsonrpc_error": data["error"]}
    result = data["result"]
    content = result["content"]
    if result.get("isError"):
        return {"_is_error": True, "text": content[0]["text"]}
    return json.loads(content[0]["text"])


def _is_transient_error(payload: dict[str, Any]) -> bool:
    """True if a tool payload carries a transient upstream error (NFR-3.2).

    A ``ThunderClient`` timeout/5xx surfaces as HTTP 200 + an ``isError`` tool
    result whose text contains a transient phrase. For grouped fan-out payloads,
    any ``libraries[*].error`` with a transient phrase also qualifies. A
    non-transient per-library error (e.g. an unknown-slug message) does NOT.
    """
    transient = ("temporarily unavailable", "service error")

    def _has_phrase(text: str) -> bool:
        low = text.lower()
        return any(p in low for p in transient)

    if payload.get("_is_error") and isinstance(payload.get("text"), str):
        if _has_phrase(payload["text"]):
            return True
    libraries = payload.get("libraries")
    if isinstance(libraries, list):
        for block in libraries:
            err = block.get("error") if isinstance(block, dict) else None
            if isinstance(err, str) and _has_phrase(err):
                return True
    return False


def with_retry(
    fn: Callable[[], dict[str, Any]],
    *,
    attempts: int = 3,
    delay: float = 3.0,
) -> dict[str, Any]:
    """Call ``fn`` with bounded retries for Thunder-dependent assertions (NFR-3.2).

    Retries on an ``httpx.TimeoutException`` / transport error (which ``fn``
    raises on an HTTP 5xx), or a transient tool/fan-out error (see
    :func:`_is_transient_error`). Does NOT retry deterministic errors (bad slug,
    auth, method guard).
    """
    last: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        try:
            result = fn()
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == attempts:
                raise
            time.sleep(delay)
            continue
        if _is_transient_error(result) and attempt < attempts:
            last = result
            time.sleep(delay)
            continue
        return result
    return last


@pytest.fixture(scope="module")
def http_client() -> Iterator[httpx.Client]:
    """A connection-pooling HTTP client reused across the module's tests."""
    with httpx.Client(timeout=TIMEOUT) as client:
        yield client


@pytest.fixture(scope="module")
def initialized(http_client: httpx.Client) -> dict[str, str]:
    """Perform the MCP initialize handshake once per module.

    Returns any ``Mcp-Session-Id`` header the server emits (empty dict in
    stateless mode). Subsequent calls echo it if present, which is harmless when
    the server runs ``stateless_http=True``.
    """
    resp = mcp_call(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "libby-mcp-it", "version": "0"},
        },
        client=http_client,
    )
    assert resp.status_code == 200, (
        f"initialize returned HTTP {resp.status_code}; "
        f"is the server up at {ENDPOINT}? body={resp.text[:300]}"
    )
    session_headers: dict[str, str] = {}
    session_id = resp.headers.get("Mcp-Session-Id")
    if session_id:
        session_headers["Mcp-Session-Id"] = session_id
    notify("notifications/initialized", client=http_client)
    return session_headers


def _call_tool(http_client: httpx.Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    resp = mcp_call(
        "tools/call",
        {"name": name, "arguments": arguments},
        client=http_client,
    )
    if resp.status_code >= 500:
        raise httpx.TransportError(f"HTTP {resp.status_code} from {name}")
    assert (
        resp.status_code == 200
    ), f"{name} returned HTTP {resp.status_code}; expected 200. body={resp.text[:300]}"
    return tool_payload(resp)


def test_initialize_returns_server_info(http_client: httpx.Client) -> None:
    """AC-3.5: initialize advertises serverInfo.name == libby-mcp and tools capability."""
    resp = mcp_call(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "libby-mcp-it", "version": "0"},
        },
        client=http_client,
    )
    assert resp.status_code == 200, f"initialize failed: HTTP {resp.status_code}"
    result = resp.json()["result"]
    assert (
        result["serverInfo"]["name"] == "libby-mcp"
    ), f"serverInfo.name should be 'libby-mcp', got {result.get('serverInfo')}"
    assert (
        "tools" in result["capabilities"]
    ), f"capabilities should advertise 'tools', got {result.get('capabilities')}"


def test_tools_list_has_two_tools(http_client: httpx.Client, initialized: dict[str, str]) -> None:
    """AC-3.6 / DD-6: exactly search_titles and get_availability (no get_deep_link)."""
    resp = mcp_call("tools/list", {}, client=http_client, _id=2)
    assert resp.status_code == 200, f"tools/list failed: HTTP {resp.status_code}"
    tools = resp.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"search_titles", "get_availability"}, (
        "post-v2 the server must expose exactly {search_titles, get_availability} "
        f"(get_deep_link was removed); got {sorted(names)}"
    )


def test_search_titles_basic(http_client: httpx.Client, initialized: dict[str, str]) -> None:
    """AC-3.7: single-library search returns titles with base + v2 enrichment fields."""
    payload = with_retry(lambda: _call_tool(http_client, "search_titles", {"library_slug": SLUG}))
    assert (
        "_is_error" not in payload and "_jsonrpc_error" not in payload
    ), f"search_titles({SLUG}) should succeed, got error: {payload}"
    titles = payload.get("titles")
    assert (
        isinstance(titles, list) and len(titles) >= 1
    ), f"expected >=1 title for slug {SLUG}, got {payload}"
    for t in titles:
        assert t.get("title"), f"each title must have a non-empty 'title', got {t}"
        link = t.get("link", "")
        assert (
            link == f"https://{SLUG}.overdrive.com/media/{t['id']}"
        ), f"link must be the v2 format https://{SLUG}.overdrive.com/media/<id>, got {link!r}"
        for key in V2_ENRICHMENT_KEYS:
            assert key in t, (
                f"v2 enrichment key {key!r} missing from title {t.get('id')}; "
                f"keys present: {sorted(t.keys())}"
            )
        assert isinstance(
            t["narrator"], list
        ), f"'narrator' must be a list (may be empty), got {type(t['narrator'])}"


def test_search_titles_bad_slug(http_client: httpx.Client, initialized: dict[str, str]) -> None:
    """AC-3.8: a bogus slug yields a human-readable error naming the library."""
    payload = _call_tool(http_client, "search_titles", {"library_slug": BOGUS_SLUG})
    assert payload.get("_is_error") or payload.get(
        "_jsonrpc_error"
    ), f"a bogus slug must surface an error result, got {payload}"
    text = payload.get("text") or json.dumps(payload.get("_jsonrpc_error", {}))
    assert (
        BOGUS_SLUG in text
    ), f"the error should name the library slug {BOGUS_SLUG!r}; got {text!r}"
    assert (
        "HTTP 404" not in text
    ), f"the error must read as human prose, not a raw status dump; got {text!r}"


def test_search_titles_multi_library(
    http_client: httpx.Client, initialized: dict[str, str]
) -> None:
    """AC-3.10: multi-library fan-out returns a grouped shape, both libraries OK."""
    payload = with_retry(
        lambda: _call_tool(http_client, "search_titles", {"library_slugs": [SLUG, SLUG_2]})
    )
    assert (
        "_is_error" not in payload and "_jsonrpc_error" not in payload
    ), f"multi-library search should succeed overall, got {payload}"
    libraries = payload.get("libraries")
    assert (
        isinstance(libraries, list) and len(libraries) == 2
    ), f"expected payload['libraries'] of length 2, got {payload}"
    slugs = [block.get("library_slug") for block in libraries]
    assert slugs == [
        SLUG,
        SLUG_2,
    ], f"fan-out order must be preserved as {[SLUG, SLUG_2]}, got {slugs}"
    for block in libraries:
        assert "titles" in block, f"each library block must carry 'titles', got {block}"
        assert "error" not in block, f"both libraries should succeed (no 'error' key), got {block}"


def test_get_availability_partial_batch(
    http_client: httpx.Client, initialized: dict[str, str]
) -> None:
    """AC-3.11: a mixed batch returns per-title data; bogus id reported not_found."""
    payload = with_retry(
        lambda: _call_tool(
            http_client,
            "get_availability",
            {"library_slug": SLUG, "title_ids": [GOING_POSTAL_ID, BOGUS_TITLE_ID]},
        )
    )
    assert (
        "_is_error" not in payload and "_jsonrpc_error" not in payload
    ), f"get_availability should succeed overall, got {payload}"
    by_id = {t["title_id"]: t for t in payload.get("titles", [])}
    assert GOING_POSTAL_ID in by_id, f"valid id {GOING_POSTAL_ID} missing from {payload}"
    valid = by_id[GOING_POSTAL_ID]
    for key in ("copies_owned", "copies_available", "holds_count"):
        assert key in valid, f"valid title must expose {key!r}, got {valid}"
    assert BOGUS_TITLE_ID in by_id, f"bogus id {BOGUS_TITLE_ID} missing from {payload}"
    assert (
        by_id[BOGUS_TITLE_ID].get("error") == "not_found"
    ), f"bogus id must be reported with error == 'not_found', got {by_id[BOGUS_TITLE_ID]}"


def test_auth_missing_bearer(http_client: httpx.Client) -> None:
    """AC-3.13: a POST with no Authorization header returns HTTP 401."""
    resp = mcp_call("tools/list", {}, token=None, client=http_client)
    assert (
        resp.status_code == 401
    ), f"missing Bearer must yield 401, got {resp.status_code}: {resp.text[:200]}"


def test_auth_wrong_token(http_client: httpx.Client) -> None:
    """AC-3.14: a POST with a wrong Bearer token returns HTTP 401."""
    resp = mcp_call("tools/list", {}, token="wrong-token", client=http_client)
    assert (
        resp.status_code == 401
    ), f"wrong token must yield 401, got {resp.status_code}: {resp.text[:200]}"


def test_get_mcp_returns_405(http_client: httpx.Client) -> None:
    """AC-3.15 / DD-7: GET /mcp returns HTTP 405 with an Allow: POST header."""
    resp = http_client.get(ENDPOINT, headers=_headers(API_KEY), timeout=TIMEOUT)
    assert (
        resp.status_code == 405
    ), f"GET /mcp must yield 405 (method guard), got {resp.status_code}"
    allow = resp.headers.get("Allow", "")
    assert "POST" in allow, f"405 response must include an 'Allow: POST' header, got {allow!r}"
