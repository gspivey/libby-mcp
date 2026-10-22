"""Embedded, stateless OAuth 2.1 authorization + resource-server support (Group F).

The MCP server is the OAuth 2.0 *resource server*; the default authorization
server is embedded, minimal, and **stateless** — every artifact (client IDs,
authorization codes, access and refresh tokens) is a self-contained HS256-signed
token, so any Lambda instance validates artifacts minted by any other instance
that shares ``LIBBY_MCP_OAUTH_SIGNING_KEY`` (no server-side store). JWT signing and
verification use only the Python standard library (``hmac``/``hashlib``/``base64``/
``json``) — no third-party JWT dependency (HIGH-4 / AC-F6.4).

PR-F1 (this package's first slice) ships ``jwt.py`` (sign/verify), ``metadata.py``
(RFC 9728 PRM + RFC 8414 AS metadata, canonical resource), ``registration.py``
(RFC 7591 public-client DCR), and ``router.py`` (path dispatch + trusted base-URL
resolution). The ``/authorize`` + ``/token`` flow lands in PR-F2.
"""
