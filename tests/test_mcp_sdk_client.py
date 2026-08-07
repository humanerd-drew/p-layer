"""Official MCP Python SDK client test (optional).

Uses mcp.client.stdio exactly as a production client (opencode, Claude
Desktop) would. Runs in CI on ubuntu where the SDK's stdio transport works.

Skipped locally on macOS: the SDK's stdio transport crashes in this
environment with an anyio cancel-scope bug (`Attempted to exit cancel scope
in a different task`) — reproduced against the official reference server,
so it is an SDK/environment defect, not a p_layer one. The wire-level
transport is covered unconditionally by test_mcp_wire.py.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    HAS_MCP_SDK = True
except ImportError:
    HAS_MCP_SDK = False


@unittest.skipIf(
    not HAS_MCP_SDK,
    "mcp SDK not installed (optional dev dependency; CI installs it)",
)
@unittest.skipIf(
    sys.platform == "darwin",
    "mcp SDK stdio transport is broken on macOS here (anyio cancel-scope bug "
    "in the SDK, reproduced against official servers) — covered by CI on ubuntu",
)
class SdkClientTests(unittest.TestCase):
    def test_sdk_client_full_session(self):
        tmp = tempfile.mkdtemp()
        env = dict(os.environ, P_LAYER_DB=str(Path(tmp) / "memory.db"), P_LAYER_EMBED="hash")
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "p_layer", "serve"],
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

        async def scenario():
            read, write = await stdio_client(params).__aenter__()
            session = await ClientSession(read, write).__aenter__()
            try:
                init = await session.initialize()
                self.assertEqual(init.serverInfo.name, "p-layers")
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                self.assertIn("remember", names)
                r = await session.call_tool("remember", {"fact": "switched to portone v2", "type": "decision"})
                self.assertIn("saved #1", r.content[0].text)
            finally:
                await session.__aexit__(None, None, None)
                await read.aclose()
                await write.aclose()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
