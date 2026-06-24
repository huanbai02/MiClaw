import os
import json
import threading
import queue
import atexit
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .config import get_log_file_path

# 内存队列 + 守护线程
class JSONLEventLogger:
    # 单例模式
    _instance = None
    _lock = threading.Lock()

    def __new__(
        cls,
        log_dir: str | Path | None = None,
        *,
        log_file: str | Path | None = None,
        workspace: str | Path | None = None,
    ):
        if log_dir is not None or log_file is not None or workspace is not None:
            instance = super().__new__(cls)
            instance._init_logger(log_dir=log_dir, log_file=log_file, workspace=workspace)
            return instance

        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_logger()
            return cls._instance
        
    def _init_logger(
        self,
        log_dir: str | Path | None = None,
        log_file: str | Path | None = None,
        workspace: str | Path | None = None,
    ):
        self.log_dir = Path(log_dir).expanduser() if log_dir is not None else None
        self.log_file = None if self.log_dir is not None else get_log_file_path(workspace=workspace, log_file=log_file)
        if self.log_dir is not None:
            os.makedirs(self.log_dir, exist_ok=True)
        else:
            os.makedirs(self.log_file.parent, exist_ok=True)

        # 无界内存队列，用于缓冲日志事件
        self.log_queue = queue.Queue()
        self._closed = False

        self.worker_thread = threading.Thread(target=self._write_loop, daemon=True)
        self.worker_thread.start()

        # 确保程序被关闭时，队列里的剩下日志能写完
        atexit.register(self.shutdown)

    # 后台线程的死循环：一直盯着队列，有日志就写，没日志就阻塞休眠
    def _write_loop(self):
        while True:
            log_item = self.log_queue.get()

            if log_item is None:
                self.log_queue.task_done()
                break

            try:
                file_path = self._resolve_write_path(log_item)
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_item, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[Logger Error] 异步写日志失败: {e}")
            finally:
                self.log_queue.task_done()

    # 前台调用的埋点方法
    def log_event(self, thread_id: str, event: str, **kwargs):
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        log_item = {
            "ts": now_utc,
            "thread_id": thread_id,
            "event": event,
            **kwargs
        }

        self.log_queue.put(log_item)

    def shutdown(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.log_queue.put(None)
        self.log_queue.join()

    def _resolve_write_path(self, log_item: dict) -> Path:
        """兼容旧 log_dir per-thread 文件和新的单文件 log path。"""
        if self.log_dir is None:
            return self.log_file
        thread_id = str(log_item.get("thread_id", "system"))
        safe_id = "".join(c for c in thread_id if c.isalnum() or c in "-_") or "default"
        return self.log_dir / f"{safe_id}.jsonl"

audit_logger = JSONLEventLogger()

_SAFE_METADATA_KEYS = {
    "tool_name",
    "operation",
    "target",
    "permission_decision",
    "error_type",
    "cwd_scope",
    "exit_code",
    "timeout",
    "shell_command_present",
    "command_length",
    "shell_risk_level",
    "blocked_by_shell_safety",
}


def build_permission_decision_event(
    request,
    result,
    *,
    tool_name: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """构建 JSON-friendly 的 permission_decision audit event。"""
    safe_metadata = _safe_permission_metadata(metadata or {})

    decision = _json_safe_value(getattr(result, "decision", "deny"))
    event = {
        "event_type": "permission_decision",
        "tool_name": tool_name or safe_metadata.get("tool_name") or "unknown",
        "capability": _json_safe_value(getattr(request, "capability", "unknown")),
        "operation": str(getattr(request, "operation", "") or ""),
        "target": str(getattr(request, "target", "") or ""),
        "decision": decision,
        "risk_level": _json_safe_value(getattr(result, "risk_level", getattr(request, "risk_level", "low"))),
        "reason": str(getattr(result, "reason", "") or ""),
        "requires_confirmation": bool(getattr(result, "requires_confirmation", False)),
        "error_type": _permission_error_type(decision),
        "metadata": safe_metadata,
    }
    return event


def log_permission_decision(
    request,
    result,
    *,
    tool_name: str | None = None,
    metadata: dict | None = None,
    thread_id: str = "system",
) -> None:
    """通过现有 JSONL logger 写入 permission_decision audit event。"""
    try:
        event = build_permission_decision_event(request, result, tool_name=tool_name, metadata=metadata)
        audit_logger.log_event(thread_id, event["event_type"], **event)
    except Exception as e:
        print(f"[Logger Error] permission decision audit failed: {e}")


def _permission_error_type(decision: str) -> str:
    if decision == "deny":
        return "permission_denied"
    if decision == "ask":
        return "permission_required"
    return ""


def _safe_permission_metadata(metadata: dict) -> dict:
    safe = {}
    for key, value in dict(metadata).items():
        key_text = str(key)
        if key_text not in _SAFE_METADATA_KEYS:
            continue
        safe[key_text] = _json_safe_value(value)
    return safe


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
