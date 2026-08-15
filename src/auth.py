"""Authentication module for MCP API key validation.

Validates incoming requests against a static API key stored in the
LIBBY_MCP_API_KEY environment variable. Uses case-insensitive header
lookup (API Gateway HTTP API lowercases all headers) and constant-time
comparison to prevent timing attacks.
"""

from __future__ import annotations

import hmac
import logging
import os

logger = logging.getLogger(__name__)


def validate_api_key(headers: dict[str, str]) -> bool:
    """Validate the MCP API key from request headers.

    Performs case-insensitive header name lookup and constant-time
    string comparison against the expected key.

    Args:
        headers: Request headers dict (keys may be any case).

    Returns:
        True if the Bearer token matches the expected API key, False otherwise.
    """
    expected_key = os.environ.get("LIBBY_MCP_API_KEY", "")
    if not expected_key:
        logger.error("LIBBY_MCP_API_KEY environment variable not set")
        return False

    # Case-insensitive header lookup (API Gateway HTTP API lowercases headers)
    normalized_headers = {k.lower(): v for k, v in headers.items()}
    auth_header = normalized_headers.get("authorization", "")

    if not auth_header:
        logger.info("auth_failed: missing authorization header")
        return False

    # Extract Bearer token
    if not auth_header.startswith("Bearer "):
        logger.info("auth_failed: missing Bearer prefix")
        return False

    token = auth_header[len("Bearer ") :]
    if not token:
        logger.info("auth_failed: empty Bearer value")
        return False

    # Constant-time comparison to prevent timing attacks
    is_valid = hmac.compare_digest(token, expected_key)
    if is_valid:
        logger.info("auth_success")
    else:
        logger.info("auth_failed: invalid token")

    return is_valid
