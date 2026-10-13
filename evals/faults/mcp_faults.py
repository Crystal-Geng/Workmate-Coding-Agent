import asyncio
import json

from evals.faults.base import FaultState
from workmate.core.mcp.client import McpClient, McpToolDef
from workmate.core.mcp.tool import McpTool


class LocalMcp:
    # 创建仅监听 loopback 的确定性测试 MCP 服务
    def __init__(self, state: FaultState | None = None):
        self.state = state
        self.client = McpClient()
        self.server = None
        self.writers = set()

    # 在首个 tools/call 到达后真实关闭 TCP 连接
    async def handle(self, reader, writer):
        self.writers.add(writer)
        try:
            while line := await reader.readline():
                req = json.loads(line)
                if "id" not in req:
                    continue
                if req["method"] == "tools/call":
                    if self.state and self.state.hit():
                        break
                    value = {"content": [{"type": "text", "text": "local-mcp-value-37"}]}
                else:
                    value = {"protocolVersion": "2024-11-05", "capabilities": {}}
                writer.write(
                    (
                        json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": value}) + "\n"
                    ).encode()
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)

    # 使用生产 McpClient 完成真实 TCP 握手
    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        await self.client.connect_tcp("127.0.0.1", self.server.sockets[0].getsockname()[1])
        if self.state:
            original = self.client.call_tool

            # 记录断连后真实客户端尝试次数，不替客户端重连
            async def counted_call(name, arguments):
                if self.state.injections:
                    self.state.recovery_attempts += 1
                    self.state.persist()
                return await original(name, arguments)

            self.client.call_tool = counted_call
        return McpTool(self.client, "eval", McpToolDef("lookup", "Read local test value", {}))

    # 关闭连接，不替生产客户端增加重连逻辑
    async def close(self):
        await self.client.close()
        for writer in list(self.writers):
            writer.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
