"""Lambda handler -- FastMCP Streamable HTTP transport over Mangum ASGI adapter.

Uses stateless mode (no in-memory session state per invocation) for Lambda
compatibility. Builds a fresh FastMCP app per invocation: mcp.server.fastmcp's
StreamableHTTPSessionManager can only call .run() once per instance, so a new
FastMCP instance is created on every _build_app() call (both cold and warm starts).
Bearer token auth is enforced via AuthMiddleware on every request.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mangum import Mangum
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from src.auth import AuthMiddleware
from src.thunder_client import ThunderClient
from src.tools import availability as availability_impl
from src.tools import deep_link as deep_link_impl
from src.tools import search_titles as search_titles_impl

# Module-level -- reused across warm Lambda invocations
_thunder_client = ThunderClient()


def _make_mcp() -> FastMCP:
    """Create and return a fresh FastMCP instance with all tools registered.

    A new instance is required on every invocation because
    StreamableHTTPSessionManager.run() can only be called once per FastMCP
    instance. Reusing a module-level instance across invocations (or across
    test cases) raises RuntimeError on the second call.
    """
    mcp: FastMCP[None] = FastMCP(
        "libby-mcp",
        stateless_http=True,
        json_response=True,
        # DNS rebinding protection is not needed here: the ASGI app is invoked
        # directly by Mangum (Lambda) or the test client, never bound to a real
        # socket where DNS rebinding is a threat. Disabling it avoids the
        # 421 Misdirected Request the middleware returns when the Host header
        # does not match the allowed_hosts list.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool(
        description=(
            "Search a library's OverDrive catalog via the Thunder API. Returns titles "
            "with availability info, deep links, and copy/hold counts. Also returns facets "
            "(including valid subject IDs for the library)."
        )
    )
    def search_titles(
        library_slug: Annotated[
            str, Field(description="Library identifier (e.g., lcpl, fairfaxcounty, nypl)")
        ],
        subjects: Annotated[
            list[str | int] | None, Field(description="Subject IDs to filter by")
        ] = None,
        format: Annotated[  # noqa: A002  (shadows builtin -- intentional, matches v1 schema)
            Literal["audiobook", "ebook"] | None, Field(description="Media format filter")
        ] = None,
        available: Annotated[
            bool | None, Field(description="Only show currently borrowable titles")
        ] = None,
        creator: Annotated[str | None, Field(description="Author or narrator name search")] = None,
        bisac: Annotated[str | None, Field(description="BISAC category code to filter by")] = None,
        series_id: Annotated[
            int | None, Field(description="Filter to a specific series by ID")
        ] = None,
        under_hours: Annotated[
            float | None,
            Field(description="Maximum audiobook length in hours (client-side filter)"),
        ] = None,
        per_page: Annotated[int | None, Field(description="Results per page (max 100)")] = None,
        page: Annotated[int | None, Field(description="Page number for pagination")] = None,
        maturity_level: Annotated[
            Literal["general", "juvenile", "youngadult", "adultonly"] | None,
            Field(description="Content maturity filter"),
        ] = None,
        sort_by: Annotated[str | None, Field(description="Sort order")] = None,
        query: Annotated[
            str | None,
            Field(description="Full-text search on title+subtitle+description"),
        ] = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"library_slug": library_slug}
        for k, v in {
            "subjects": subjects,
            "format": format,
            "available": available,
            "creator": creator,
            "bisac": bisac,
            "series_id": series_id,
            "under_hours": under_hours,
            "per_page": per_page,
            "page": page,
            "maturity_level": maturity_level,
            "sort_by": sort_by,
            "query": query,
        }.items():
            if v is not None:
                args[k] = v
        result = search_titles_impl.handle(args, _thunder_client)
        if "error" in result:
            raise ToolError(result["error"])
        return result

    @mcp.tool(
        description=(
            "Get raw availability data for specific titles at a library. "
            "Returns copies owned, copies available, holds count, and estimated wait days."
        )
    )
    def get_availability(
        library_slug: Annotated[str, Field(description="Library identifier")],
        title_ids: Annotated[
            list[str | int],
            Field(description="Array of OverDrive title IDs to check"),
        ],
    ) -> dict[str, Any]:
        result = availability_impl.handle(
            {"library_slug": library_slug, "title_ids": title_ids}, _thunder_client
        )
        if "error" in result:
            raise ToolError(result["error"])
        return result

    @mcp.tool(
        description=(
            "Generate a direct OverDrive link for a specific title. "
            "Format: https://{slug}.overdrive.com/media/{id}"
        )
    )
    def get_deep_link(
        title_id: Annotated[str | int, Field(description="OverDrive numeric title ID")],
        library_slug: Annotated[str, Field(description="Library slug for the link context")],
    ) -> dict[str, Any]:
        result = deep_link_impl.handle({"title_id": title_id, "library_slug": library_slug})
        if "error" in result:
            raise ToolError(result["error"])
        return result

    return mcp


def _build_app() -> AuthMiddleware:
    """Build a fresh ASGI app instance for each Lambda invocation."""
    fastmcp_app = _make_mcp().streamable_http_app()
    return AuthMiddleware(fastmcp_app)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point for API Gateway HTTP API v2 events."""
    handler = Mangum(_build_app(), lifespan="on")
    return handler(event, context)
