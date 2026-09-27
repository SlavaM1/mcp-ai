from __future__ import annotations

import unittest

from report_mcp.server import app, health, mcp


class MCPDefinitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_tool_name_description_and_schema(self) -> None:
        tools = await mcp.list_tools()

        self.assertEqual([tool.name for tool in tools], ["save_trip_report"])
        tool = tools[0]
        self.assertIn("already prepared multi-domain trip analysis", tool.description)
        self.assertIn("analyze_trip", tool.description)
        self.assertIn("does not fetch", tool.description)
        self.assertEqual(tool.input_schema["required"], ["trip_analysis"])
        self.assertEqual(
            tool.input_schema["properties"]["trip_analysis"]["$ref"],
            "#/$defs/TripAnalysis",
        )
        self.assertEqual(
            set(tool.input_schema["$defs"]["TripAnalysis"]["required"]),
            {"destination", "period", "weather", "budget", "flags", "recommendations"},
        )
        self.assertEqual(tool.input_schema["properties"]["format"]["default"], "markdown")
        self.assertEqual(tool.input_schema["properties"]["format"]["const"], "markdown")
        self.assertIn("Only markdown", tool.input_schema["properties"]["format"]["description"])
        self.assertEqual(
            set(tool.output_schema["properties"]),
            {"saved", "file_name", "path", "format"},
        )

    async def test_health_and_mcp_routes_are_registered(self) -> None:
        routes = {route.path for route in app.routes}
        self.assertIn("/health", routes)
        self.assertIn("/mcp", routes)

        response = await health(None)  # type: ignore[arg-type]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b'{"status":"ok"}')
