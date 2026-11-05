"""A real stdio subprocess handshake, independent of external APIs."""

import asyncio
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
import pytest


@pytest.mark.local_subprocess
def test_stdio_handshake_and_status(tmp_path):
    config = tmp_path / "paper-fetch.toml"
    config.write_text('mailto = "test@example.org"\nallow_scihub = false\n', encoding="utf-8")
    # The subprocess starts with a clean interpreter, so an editable install is not
    # assumed: point it at this checkout's src/ directory explicitly.
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")]))}

    async def check():
        transport = StdioTransport(command=sys.executable,
                                   args=["-m", "paper_fetch_mcp_safe.server", "--config", str(config)],
                                   env=env)
        async with Client(transport) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == {"execute_paper_request", "get_server_status"}
            result = await client.call_tool("get_server_status", {})
            assert not result.is_error
            assert "providers" in result.data
            assert result.data["scihub_enabled"] is False
            assert result.data["compliance"]["license_policy"] == "open"
            assert result.data["compliance"]["license_required_by_default"] is True
    asyncio.run(check())
