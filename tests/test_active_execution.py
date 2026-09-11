"""验证 run-local active execution controller 的取消与 stale-clear 边界。"""

from __future__ import annotations

import asyncio

from miclaw.core.agent.active_execution import ActiveExecutionController


def test_active_execution_controller_cancels_once_and_ignores_stale_clear():
    """同一 task 只接收一次 cancel；旧 task 的 clear 不能抹掉新登记。"""
    async def scenario():
        controller = ActiveExecutionController()
        first = asyncio.create_task(asyncio.Event().wait())
        controller.register(first, "execution-one", 1)
        assert controller.cancel_active() == "cancellation_requested"
        assert controller.cancel_active() == "cancellation_already_requested"
        assert controller.cancellation_requested_for(first) is True
        try:
            await first
        except asyncio.CancelledError:
            pass

        second = asyncio.create_task(asyncio.Event().wait())
        controller.register(second, "execution-two", 1)
        controller.clear_if_current(first)
        assert controller.execution_id == "execution-two"
        controller.clear_if_current(second)
        assert controller.cancel_active() == "no_active_execution"
        second.cancel()
        try:
            await second
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
