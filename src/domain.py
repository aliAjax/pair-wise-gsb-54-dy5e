"""领域基础类型与输入校验。"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List


class DomainError(Exception):
    status = 400
    code = "domain_error"


class ValidationError(DomainError):
    status = 422
    code = "validation_error"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class ResourceConflict(Conflict):
    """抢修船或备缆被其他抢修记录占用；details描述被哪项抢修占住。"""

    code = "resource_conflict"

    def __init__(self, message: str, details: Dict[str, Any] = None) -> None:
        super().__init__(message)
        self.details = details or {}


class StaleVersion(Conflict):
    code = "version_conflict"


class PermissionDenied(DomainError):
    status = 403
    code = "permission_denied"


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str
    organization: str = ""


def text(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s不能为空" % key)
    return value.strip()


def optional_text(data: Dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError("%s必须是文本" % key)
    return value.strip()


def number(data: Dict[str, Any], key: str, minimum: float = None, maximum: float = None) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("%s必须是数字" % key)
    value = float(value)
    if minimum is not None and value < minimum:
        raise ValidationError("%s不能小于%s" % (key, minimum))
    if maximum is not None and value > maximum:
        raise ValidationError("%s不能大于%s" % (key, maximum))
    return value


def integer(data: Dict[str, Any], key: str, minimum: int = None, maximum: int = None) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("%s必须是整数" % key)
    if minimum is not None and value < minimum:
        raise ValidationError("%s不能小于%s" % (key, minimum))
    if maximum is not None and value > maximum:
        raise ValidationError("%s不能大于%s" % (key, maximum))
    return value


def choice(data: Dict[str, Any], key: str, allowed: List[str]) -> str:
    value = text(data, key)
    if value not in allowed:
        raise ValidationError("%s只能是%s" % (key, "/".join(allowed)))
    return value


def boolean(data: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError("%s必须是布尔值" % key)
    return value


def text_list(data: Dict[str, Any], key: str, minimum: int = 0) -> List[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError("%s必须是文本列表" % key)
    if len(value) < minimum:
        raise ValidationError("%s至少需要%s项" % (key, minimum))
    return [item.strip() for item in value]


def iso_dt(data: Dict[str, Any], key: str, required: bool = False):
    """解析ISO8601日期时间，统一为UTC；日期(YYYY-MM-DD)按当天结束(UTC)处理。

    返回带时区的datetime；字段缺省且required=False时返回None。
    """
    raw = data.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise ValidationError("%s不能为空" % key)
        return None
    if not isinstance(raw, str):
        raise ValidationError("%s必须是ISO8601时间文本" % key)
    text_value = raw.strip()
    try:
        if len(text_value) == 10:
            parsed = datetime.combine(date.fromisoformat(text_value), datetime.max.time()).replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("%s必须是ISO8601时间文本" % key) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def dt_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
