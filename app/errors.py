"""可区分的错误分类与统一异常类型。"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    """拒绝原因分类，调用方与测试可据此区分不同失败。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    UNRECOGNIZED_CODING_SYSTEM = "unrecognized_coding_system"
    UNSUPPORTED_VERSION = "unsupported_version"
    VERSION_CONFLICT = "version_conflict"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    MALFORMED_INPUT = "malformed_input"
    IDEMPOTENCY_REPLAY = "idempotency_replay"


class IngestError(Exception):
    """业务错误：携带分类、面向调用方的消息与原始线索。"""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        raw_hint: Any = None,
        field: str | None = None,
    ) -> None:
        self.category = category
        self.message = message
        self.raw_hint = raw_hint
        self.field = field
        super().__init__(message)

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        return f"[{self.category.value}] {self.message}"
