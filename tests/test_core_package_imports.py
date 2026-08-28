"""验证 core 领域 package 的 canonical 模块可独立导入。"""

import importlib

import pytest


CANONICAL_MODULES = (
    "miclaw.core.agent.graph",
    "miclaw.core.agent.execution",
    "miclaw.core.agent.recovery",
    "miclaw.core.agent.context",
    "miclaw.core.agent.context_assembly",
    "miclaw.core.execution.models",
    "miclaw.core.execution.state",
    "miclaw.core.execution.failures",
    "miclaw.core.execution.retry",
    "miclaw.core.execution.guards",
    "miclaw.core.execution.recovery",
    "miclaw.core.runtime.config",
    "miclaw.core.runtime.workspace",
    "miclaw.core.runtime.bus",
    "miclaw.core.runtime.execution_store",
    "miclaw.core.security.permissions",
    "miclaw.core.observability.logger",
    "miclaw.core.observability.redaction",
    "miclaw.core.observability.trace",
    "miclaw.core.memory.models",
    "miclaw.core.memory.user_profile",
    "miclaw.core.memory.lifecycle",
    "miclaw.core.memory.permissions",
    "miclaw.core.memory.retrieval",
    "miclaw.core.mcp.adapter",
    "miclaw.core.mcp.client",
    "miclaw.core.mcp.permissions",
    "miclaw.core.mcp.tools",
    "miclaw.core.skills.loader",
    "miclaw.core.scheduler.heartbeat",
    "miclaw.core.llm.provider",
    "miclaw.core.tools.sandbox",
)


@pytest.mark.parametrize("module_name", CANONICAL_MODULES)
def test_canonical_core_module_imports(module_name):
    """每个迁移后的 canonical module 都能独立加载。"""
    assert importlib.import_module(module_name).__name__ == module_name


def test_moved_modules_share_canonical_state_and_type_identities():
    """跨领域引用同一 workspace、permission、Memory 与 trace 实现，不产生迁移副本。"""
    import miclaw.core.memory.lifecycle as lifecycle
    import miclaw.core.memory.models as models
    import miclaw.core.memory.retrieval as retrieval
    import miclaw.core.memory.user_profile as user_profile
    import miclaw.core.observability.logger as logger
    import miclaw.core.observability.trace as trace
    import miclaw.core.runtime.workspace as workspace
    import miclaw.core.security.permissions as permissions
    import miclaw.core.tools.sandbox as sandbox

    assert sandbox.WorkspaceScope is workspace.WorkspaceScope
    assert user_profile.WorkspaceScope is workspace.WorkspaceScope
    assert sandbox.PermissionDecision is permissions.PermissionDecision
    assert lifecycle.MemoryKind is models.MemoryKind is retrieval.MemoryKind
    assert logger.get_current_trace_context is trace.get_current_trace_context
