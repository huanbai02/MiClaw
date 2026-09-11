from typing import Any, Type
from functools import wraps

from langchain_core.tools import BaseTool, StructuredTool

from .result import (
    ToolResult,
    apply_tool_outcome_status,
    encode_tool_outcome,
    format_tool_result_for_model,
)


class _MiClawStructuredTool(StructuredTool):
    """仅在 ToolCall 路径为 ToolMessage 对齐 framework status。"""

    def run(self, *args, **kwargs):
        """保留 direct invoke 的 string 行为，并投影 ToolMessage status。"""
        return apply_tool_outcome_status(super().run(*args, **kwargs))

    async def arun(self, *args, **kwargs):
        """异步 ToolNode 路径与同步路径使用同一 outcome status。"""
        return apply_tool_outcome_status(await super().arun(*args, **kwargs))
from abc import ABC, abstractmethod
import asyncio
from pydantic import BaseModel, Field

def miclaw_tool(func):
    """把 MiClaw ToolResult 映射到官方 content/artifact 通道，保留 legacy string。"""
    @wraps(func)
    def wrapped(*args, **kwargs):
        result = func(*args, **kwargs)
        if isinstance(result, ToolResult):
            return format_tool_result_for_model(result), encode_tool_outcome(result)
        return result, None

    return _MiClawStructuredTool.from_function(wrapped, response_format="content_and_artifact")

# 类模式工具（适合复杂场景）
class MiClawBaseTool(BaseTool, ABC):
    """
    MiClaw 的标准工具基类。
    如果你的工具需要复杂的初始化逻辑（比如维持一个数据库长连接），
    或者需要保存内部状态，请继承此类并实现 `_run` 方法。
    """
    
    # 未来在扩展层（Extended）做权限控制时，可以用到这个字段
    # required_permission_level: int = 0 
    
    # 也可以加上工具运行超时限制等统一配置
    # timeout_seconds: int = 30 
    name: str
    description: str
    args_schema: Type[BaseModel]
    
    @abstractmethod
    def _run(self, **kwargs: Any) -> Any:
        """
        工具的同步执行逻辑，子类必须实现。
        """
        raise NotImplementedError("子类必须实现 _run 方法")

    async def _arun(self, **kwargs: Any) -> Any:
        """
        工具的异步执行逻辑（可选）。如果你的工具涉及网络请求，强烈建议实现。
        """
        # 默认回退到同步执行
        return await asyncio.to_thread(self._run, **kwargs)

# =========================用法============================    
# class AddArgs(BaseModel):
#     a: int = Field(description="第一个加数")
#     b: int = Field(description="第二个加数")


# class AddTool(MiClawBaseTool):
#     name: str = "add"
#     description: str = "计算两个数的和"
#     args_schema: Type[BaseModel] = AddArgs

#     def _run(self, a: int, b: int) -> int:
#         return a + b


# if __name__ == "__main__":
#     tool_instance = AddTool()

#     # 直接调用工具
#     result = tool_instance.invoke({"a": 2, "b": 3})
#     print("invoke result:", result)

#     # 异步调用工具
#     async def main():
#         result_async = await tool_instance.ainvoke({"a": 10, "b": 20})
#         print("ainvoke result:", result_async)

#     asyncio.run(main())
