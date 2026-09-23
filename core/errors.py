"""Gemini API 错误分类。

核心目的：把「瞬时抖动」「分钟级限流」「日配额耗尽」分开处理。
- 503 UNAVAILABLE       -> 换 Key 立刻重试，不惩罚该 Key（503 是服务端抖动）
- 429 + 分钟级限流      -> 该 Key 冷却 retryDelay 秒
- 429 + 返回体带 quotaValue -> 该 Key 配额确实被吃满，**直接禁用**（不再参与调度）
- 429 + 没有 quotaValue  -> 一律不自动禁用（可能只是瞬时抖动 / 分钟级限流）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class ErrorKind(str, Enum):
    TRANSIENT = "TRANSIENT"
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA_DAILY = "QUOTA_DAILY"
    FATAL = "FATAL"
    UNKNOWN = "UNKNOWN"


@dataclass
class ClassifiedError:
    kind: ErrorKind
    label: str
    code: int | None
    message: str
    retry_after: float = 2.0
    cooldown_until: datetime | None = None
    quota_value: str | None = None

    @property
    def disables_key(self) -> bool:
        """是否直接禁用这个 Key。

        判定标准只有一个硬信号：429 的返回体里出现 quotaValue（配额上限值）。
        有 -> 该 Key 的配额确实被吃满，自动禁用；没有 -> 不自动禁用。
        """
        if not self.quota_value:
            return False
        return self.code == 429 or self.kind in (
            ErrorKind.RATE_LIMIT,
            ErrorKind.QUOTA_DAILY,
        )


_CODE_RE = re.compile(r"['\"]?code['\"]?\s*[:=]\s*(\d{3})")
_STATUS_RE = re.compile(r"['\"]?status['\"]?\s*[:=]\s*['\"]([A-Z_]+)['\"]")
_STATUS_INLINE_RE = re.compile(r"\b(503|500|429|400|401|403|404)\s+([A-Z_]+)")
_RETRY_RE = re.compile(
    r"(?:retryDelay['\"]?\s*[:=]\s*['\"]?|retry in\s+)([\d.]+)\s*s", re.I
)
_QUOTA_RE = re.compile(r"['\"]?quotaId['\"]?\s*[:=]\s*['\"]([A-Za-z0-9\-]+)['\"]")
_METRIC_RE = re.compile(
    r"['\"]?quotaMetric['\"]?\s*[:=]\s*['\"]([A-Za-z0-9_./\-]+)['\"]"
)
# quotaValue 是「配额上限值」，只有它出现才说明这个 Key 的额度真的用完了
_QUOTA_VALUE_RE = re.compile(
    r"['\"]?quotaValue['\"]?\s*[:=]\s*['\"]?([0-9]+)"
)

FATAL_CODES = {400, 401, 403, 404}


def next_local_midnight(now: datetime | None = None) -> datetime:
    """下一个本地零点（免费额度按天重置）。"""
    now = now or datetime.now()
    return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))


def _extract(
    exc: Exception,
) -> tuple[int | None, str | None, float | None, str, str, str | None]:
    msg = getattr(exc, "message", None)
    payload = None
    if isinstance(msg, dict):
        payload = msg
    elif isinstance(msg, str):
        try:
            payload = json.loads(msg)
        except Exception:
            payload = None

    blob_parts = [str(exc)]
    if msg is not None:
        blob_parts.append(str(msg))
    if payload is not None:
        blob_parts.append(json.dumps(payload, ensure_ascii=False))
    blob = "\n".join(blob_parts)

    code = getattr(exc, "code", None)
    if not isinstance(code, int):
        m = _CODE_RE.search(blob) or _STATUS_INLINE_RE.search(blob)
        code = int(m.group(1)) if m else None

    status = getattr(exc, "status", None)
    if not isinstance(status, str):
        m = _STATUS_RE.search(blob)
        status = m.group(1) if m else (m2.group(2) if (m2 := _STATUS_INLINE_RE.search(blob)) else None)

    retry_after = None
    m = _RETRY_RE.search(blob)
    if m:
        try:
            retry_after = float(m.group(1))
        except ValueError:
            retry_after = None

    quota_id = ""
    m = _QUOTA_RE.search(blob)
    if m:
        quota_id = m.group(1)

    metric = ""
    m = _METRIC_RE.search(blob)
    if m:
        metric = m.group(1)

    quota_value = None
    m = _QUOTA_VALUE_RE.search(blob)
    if m:
        quota_value = m.group(1)

    return code, status, retry_after, quota_id, metric, quota_value


def classify(exc: Exception, now: datetime | None = None) -> ClassifiedError:
    code, status, retry_after, quota_id, metric, quota_value = _extract(exc)
    now = now or datetime.now()

    label = f"ERROR_{code}" if code else f"ERROR_{type(exc).__name__.upper()}"
    message = str(getattr(exc, "message", exc))
    if isinstance(message, dict):
        message = str(message.get("error", {}).get("message", message))
    message = message[:500]

    is_daily = (
        "PerDay" in quota_id
        or "RequestsPerDay" in quota_id
        or "free_tier" in metric.lower()
    )

    if code == 503 or status == "UNAVAILABLE" or "UNAVAILABLE" in str(status or ""):
        return ClassifiedError(
            kind=ErrorKind.TRANSIENT,
            label="ERROR_503",
            code=code or 503,
            message=message,
            retry_after=max(retry_after or 0, 2.0),
        )

    if code == 429 or status == "RESOURCE_EXHAUSTED":
        # quotaValue 是唯一硬信号：出现即说明额度被吃满 -> 禁用该 Key
        if quota_value:
            until = next_local_midnight(now) if is_daily else None
            return ClassifiedError(
                kind=ErrorKind.QUOTA_DAILY if is_daily else ErrorKind.RATE_LIMIT,
                label=(
                    "ERROR_429_QUOTA_EXHAUSTED"
                    if is_daily
                    else "ERROR_429_QUOTA_LIMIT"
                ),
                code=429,
                message=message,
                retry_after=(
                    max((until - now).total_seconds(), 1.0)
                    if until
                    else max(retry_after or 30.0, 2.0)
                ),
                cooldown_until=until,
                quota_value=quota_value,
            )
        if is_daily:
            until = next_local_midnight(now)
            return ClassifiedError(
                kind=ErrorKind.QUOTA_DAILY,
                label="ERROR_429_QUOTA_DAILY",
                code=429,
                message=message,
                retry_after=max((until - now).total_seconds(), 1.0),
                cooldown_until=until,
            )
        return ClassifiedError(
            kind=ErrorKind.RATE_LIMIT,
            label="ERROR_429",
            code=429,
            message=message,
            retry_after=max(retry_after or 30.0, 2.0),
        )

    if code in FATAL_CODES or status in {"INVALID_ARGUMENT", "PERMISSION_DENIED"}:
        return ClassifiedError(
            kind=ErrorKind.FATAL,
            label=label,
            code=code,
            message=message,
            retry_after=0.0,
        )

    return ClassifiedError(
        kind=ErrorKind.UNKNOWN,
        label=label,
        code=code,
        message=message,
        retry_after=max(retry_after or 0, 2.0),
    )
