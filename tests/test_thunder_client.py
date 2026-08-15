"""Tests for ThunderClient — Thunder API HTTP client.

Covers: URL construction, parameter mapping, timeout handling, error wrapping,
User-Agent header. No real network calls — all HTTP is mocked.
"""

from unittest.mock import MagicMock, patch

import pytest

thunder_client = pytest.importorskip(
    "src.thunder_client", reason="Implementation not yet written (TDD red phase)"
)


class TestSearchURLConstruction:
    """Test that ThunderClient.search() builds correct Thunder API URLs."""

    def test_base_url_with_slug(self, mock_thunder_client: MagicMock) -> None:
        """AC-4.1: URL constructed as /v2/libraries/{slug}/media."""
        client = thunder_client.ThunderClient()
        with patch("httpx.Client.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value={"items": [], "totalItems": 0}),
                raise_for_status=MagicMock(),
            )
            client.search("lcpl", {})
            call_url = mock_get.call_args[0][0]
            assert "thunder.api.overdrive.com/v2/libraries/lcpl/media" in call_url, (
                "REMEDIATION: ThunderClient.search() must construct URL as "
                "https://thunder.api.overdrive.com/v2/libraries/{slug}/media"
            )

    def test_repeated_subject_params(self) -> None:
        """AC-1.2: subjects=[24, 80] maps to subject=24&subject=80."""
        client = thunder_client.ThunderClient()
        with patch("httpx.Client.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value={"items": [], "totalItems": 0}),
                raise_for_status=MagicMock(),
            )
            client.search("lcpl", {"subject": ["24", "80"]})
            call_url = mock_get.call_args[0][0]
            assert "subject=24" in call_url, (
                "REMEDIATION: ThunderClient must support repeated subject params"
            )
            assert "subject=80" in call_url, (
                "REMEDIATION: ThunderClient must support repeated subject params"
            )

    def test_format_mapping_audiobook(self) -> None:
        """AC-1.3: format param passed directly to URL."""
        client = thunder_client.ThunderClient()
        with patch("httpx.Client.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value={"items": [], "totalItems": 0}),
                raise_for_status=MagicMock(),
            )
            client.search("lcpl", {"format": "audiobook-overdrive"})
            call_url = mock_get.call_args[0][0]
            assert "format=audiobook-overdrive" in call_url, (
                "REMEDIATION: ThunderClient must pass format param to URL"
            )


class TestGetAvailabilityURL:
    """Test that get_availability constructs the library-scoped availability endpoint."""

    def test_availability_url_construction(self) -> None:
        """AC-2.3: Uses /v2/libraries/{slug}/media/availability?titleIds=..."""
        client = thunder_client.ThunderClient()
        with patch("httpx.Client.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value={"items": []}),
                raise_for_status=MagicMock(),
            )
            client.get_availability("lcpl", ["12345", "67890"])
            call_url = mock_get.call_args[0][0]
            assert "/v2/libraries/lcpl/media/availability" in call_url, (
                "REMEDIATION: get_availability must use /v2/libraries/{slug}/media/availability"
            )
            assert "titleIds=12345" in call_url, (
                "REMEDIATION: get_availability must include titleIds in query params"
            )
            assert "titleIds=67890" in call_url, (
                "REMEDIATION: get_availability must include all title IDs"
            )


class TestTimeoutHandling:
    """Test that ThunderClient handles timeouts gracefully."""

    def test_timeout_returns_structured_error(self) -> None:
        """NFR-6.2: 10s timeout returns structured error, not exception."""
        import httpx

        client = thunder_client.ThunderClient()
        with patch("httpx.Client.get") as mock_get:
            mock_get.side_effect = httpx.TimeoutException("Connection timed out")
            result = client.search("lcpl", {})
            assert "error" in result, (
                "REMEDIATION: ThunderClient.search() must return a dict with 'error' key on timeout"
            )
            assert "timeout" in result["error"].lower() or "unavailable" in result["error"].lower()


class TestHTTPErrorWrapping:
    """Test that HTTP errors are wrapped into structured error dicts."""

    def test_server_error_returns_structured_error(self) -> None:
        """NFR-6.1: HTTP 500 returns structured error, not stack trace."""
        import httpx

        client = thunder_client.ThunderClient()
        with patch("httpx.Client.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Server Error", request=MagicMock(), response=mock_response
            )
            mock_get.return_value = mock_response
            result = client.search("lcpl", {})
            assert "error" in result, (
                "REMEDIATION: ThunderClient must return structured error on HTTP 5xx"
            )


class TestUserAgent:
    """Test that requests include the correct User-Agent header."""

    def test_user_agent_set(self) -> None:
        """NFR-3.2: Requests include User-Agent: libby-mcp/1.0."""
        client = thunder_client.ThunderClient()
        # Check that the client has the correct headers configured
        assert client._client.headers.get("user-agent") == "libby-mcp/1.0", (
            "REMEDIATION: ThunderClient must set User-Agent to 'libby-mcp/1.0'"
        )
