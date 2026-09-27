from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from os import getenv
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, WithJsonSchema
from starlette.requests import Request
from starlette.responses import JSONResponse

from report_mcp import __version__
from report_mcp.reporting import SavedReport, TripAnalysis, write_trip_report


@dataclass(frozen=True)
class AppContext:
    reports_directory: Path


@asynccontextmanager
async def lifespan(_: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
    yield AppContext(
        reports_directory=Path(getenv("REPORTS_DIRECTORY", "/data/reports")),
    )


mcp = MCPServer[AppContext](
    "report-mcp",
    description="Persists already prepared multi-domain trip analyses as reports.",
    version=__version__,
    lifespan=lifespan,
)


@mcp.tool(
    description=(
        "Saves an already prepared multi-domain trip analysis as a Markdown report. "
        "Pass the complete structured output of the travel analyze_trip tool in trip_analysis. "
        "This tool only formats and persists that supplied analysis; it does not fetch data, "
        "plan a trip, or perform any analysis itself."
    )
)
async def save_trip_report(
    trip_analysis: Annotated[
        TripAnalysis,
        Field(
            description=(
                "Complete structured travel analyze_trip result. It must contain a non-empty "
                "destination and may contain all prepared analysis domains."
            )
        ),
    ],
    ctx: Context[AppContext, Any],
    format: Annotated[
        str,
        Field(description="Report format. Only markdown is supported."),
        WithJsonSchema(
            {
                "type": "string",
                "const": "markdown",
                "description": "Report format. Only markdown is supported.",
            }
        ),
    ] = "markdown",
) -> SavedReport:
    return write_trip_report(
        trip_analysis,
        ctx.request_context.lifespan_context.reports_directory,
        format,
    )


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        allowed_hosts=["report-mcp:8004", "localhost:8004", "127.0.0.1:8004"],
        allowed_origins=[],
    ),
)
