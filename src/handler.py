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
from src.tools import search_titles as search_titles_impl
from src.tools._slugs import normalize_slug

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
            "Search one or more libraries' OverDrive catalogs via the Thunder API. "
            "Returns titles with availability info (including holds_per_copy and "
            "estimated_wait_days folded in from the search response), deep links (each "
            "title's `link` field), and copy/hold counts. Provide exactly one of "
            "library_slug (single library, flat response) or library_slugs (fan-out "
            "across up to 10 libraries, results grouped per library). Set exact=true "
            "(with a query) to return only titles whose title matches the query exactly. "
            "Facets (valid "
            "subject IDs for the library) are opt-in: pass include_facets=true to "
            "receive them; by default the facets block is omitted to reduce payload size."
        )
    )
    async def search_titles(
        library_slug: Annotated[
            str | None,
            Field(description="Single library identifier (e.g., lcpl, fairfax, nypl)"),
        ] = None,
        library_slugs: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Multiple library identifiers to search in parallel (e.g., "
                    "['lcpl', 'fairfax', 'nypl']). Mutually exclusive with library_slug; "
                    "max 10 libraries. Results are grouped per library."
                )
            ),
        ] = None,
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
        exact: Annotated[
            bool,
            Field(
                description=(
                    "When true, return only titles whose title matches `query` "
                    "exactly (case-insensitive), with no fuzzy fallback. Requires "
                    "`query`. Defaults to false (fuzzy full-text search)."
                )
            ),
        ] = False,
        include_facets: Annotated[
            bool,
            Field(
                description=(
                    "Include the facets block (valid subject IDs, etc.) in the response. "
                    "Defaults to false — facets are opt-in to reduce payload size."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        # Normalize slugs at the input boundary (FR-D2): lowercase + trim before
        # they are built into args, validated, deduped, or echoed into responses.
        if library_slug is not None:
            library_slug = normalize_slug(library_slug)
        if library_slugs is not None:
            library_slugs = [normalize_slug(s) if isinstance(s, str) else s for s in library_slugs]

        # Build args dict from non-None values only, so a library_slugs-only
        # call never carries a stray library_slug=None into validation.
        args: dict[str, Any] = {"include_facets": include_facets, "exact": exact}
        for k, v in {
            "library_slug": library_slug,
            "library_slugs": library_slugs,
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

        # exact requires query (AC-D6.4): there is no title to match against
        # without a query, so reject before dispatch.
        if exact and "query" not in args:
            raise ToolError("exact requires query")

        # One-of validation (AC-B1.2)
        has_slug = "library_slug" in args
        has_slugs = "library_slugs" in args
        if has_slug and has_slugs:
            raise ToolError("provide either library_slug or library_slugs, not both")
        if not has_slug and not has_slugs:
            raise ToolError("library_slug or library_slugs is required")

        # Dispatch
        if has_slugs:
            # library_slugs validation (AC-B1.9–B1.12), then dedupe before fan-out.
            err = search_titles_impl.validate_library_slugs(args["library_slugs"])
            if err is not None:
                raise ToolError(err)
            args["library_slugs"] = search_titles_impl.dedupe_library_slugs(args["library_slugs"])
            result = await search_titles_impl.handle_multi(args, _thunder_client)
        else:
            result = search_titles_impl.handle(args, _thunder_client)

        if "error" in result:
            raise ToolError(result["error"])
        return result

    @mcp.tool(
        description=(
            "Get raw availability data for specific titles at a library. "
            "Returns copies owned, copies available, holds count, and estimated wait days. "
            "A title the library does not own (or a malformed title ID) is returned with "
            "status 'not_owned' rather than failing the whole request."
        )
    )
    async def get_availability(
        library_slug: Annotated[str, Field(description="Library identifier")],
        title_ids: Annotated[
            list[str | int],
            Field(description="Array of OverDrive title IDs to check"),
        ],
    ) -> dict[str, Any]:
        # Normalize the slug at the input boundary (FR-D2). title_ids are
        # normalized (int/padded → stripped string) inside the availability
        # classifier, which is the authoritative coercion point (HIGH-4/HIGH-A).
        slug = normalize_slug(library_slug)
        result = await availability_impl.handle(
            {"library_slug": slug, "title_ids": title_ids}, _thunder_client
        )
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
