"""PR30 使用的最小官方 MCP SDK stdio 测试 server。"""

import os
from pathlib import Path
import sys

import anyio
from mcp.server import MCPServer
from mcp.types import ImageContent


server = MCPServer("miclaw-pr30-test")


@server.tool()
def echo(text: str) -> str:
    """返回输入文本。"""
    return text


@server.tool()
def side_effect_marker(path: str, value: str = "called") -> str:
    """向测试 marker 追加一行，用于证明 permission ordering。"""
    marker = Path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("a", encoding="utf-8") as file:
        file.write(f"{value}\n")
    return "marker written"


@server.tool()
def failing_tool() -> str:
    """返回 SDK 标准 isError tool result。"""
    raise RuntimeError("expected tool failure")


@server.tool()
def long_text(length: int) -> str:
    """返回指定长度文本，供 result bound 测试。"""
    return "x" * length


@server.tool(structured_output=False)
def image_result() -> ImageContent:
    """返回非文本 block，确保 client 不展开 binary data。"""
    return ImageContent(data="RAW_BASE64_MARKER", mimeType="image/png")


@server.tool()
def read_environment(name: str) -> str:
    """返回测试环境变量是否可见。"""
    return os.environ.get(name, "<missing>")


@server.tool()
async def slow_tool(delay: float) -> str:
    """延迟响应，供 timeout 测试。"""
    await anyio.sleep(delay)
    return "finished"


if __name__ == "__main__":
    pid_file = os.environ.get("MCP_TEST_PID_FILE")
    if pid_file:
        Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")
    print("MCP_STDERR_SECRET_MARKER", file=sys.stderr, flush=True)
    server.run("stdio")
