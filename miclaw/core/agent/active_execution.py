"""当前 interactive runtime 的单个 active execution 取消控制，不持久化任何用户内容。"""

from __future__ import annotations

import asyncio


class ActiveExecutionController:
    """维护一个 run-local active execution Task，并避免旧任务清理新任务。"""

    def __init__(self) -> None:
        self._task: asyncio.Task[object] | None = None
        self._cancel_requested_for: asyncio.Task[object] | None = None
        self.execution_id: str | None = None
        self.attempt: int | None = None

    def register(self, task: asyncio.Task[object], execution_id: str, attempt: int) -> None:
        """登记已创建且 identity 已确定的执行任务。"""
        if not isinstance(task, asyncio.Task) or type(execution_id) is not str or not execution_id.strip() or type(attempt) is not int or attempt < 1:
            raise ValueError("invalid_active_execution")
        if self._task is not None and not self._task.done():
            raise RuntimeError("active_execution_exists")
        self._task = task
        self._cancel_requested_for = None
        self.execution_id = execution_id
        self.attempt = attempt

    def clear_if_current(self, task: asyncio.Task[object]) -> None:
        """仅清理当前 task，避免旧执行 finally 抹掉新执行登记。"""
        if self._task is task:
            self._task = None
            self._cancel_requested_for = None
            self.execution_id = None
            self.attempt = None

    def cancel_active(self) -> str:
        """请求取消当前执行；返回稳定的 host-control 结果。"""
        task = self._task
        if task is None or task.done():
            return "no_active_execution"
        if self._cancel_requested_for is task:
            return "cancellation_already_requested"
        self._cancel_requested_for = task
        task.cancel()
        return "cancellation_requested"

    def cancellation_requested_for(self, task: asyncio.Task[object]) -> bool:
        """判断当前 child cancellation 是否由本 runtime control boundary 请求。"""
        return self._cancel_requested_for is task
