from __future__ import annotations

import logging
import re
from typing import Any


_SENSITIVE_PATTERNS = (
    re.compile(r"\b1[3-9]\d{9}\b"),
    re.compile(r"\b\d{17}[0-9Xx]\b"),
    re.compile(r"(?i)(?:患者姓名|姓名|patient\s*name)\s*[:：=]\s*[^,，;；\s]{2,32}"),
    re.compile(r"(?i)(?:住院号|门诊号|病历号|病例号|patient\s*id)\s*[:：=]\s*[A-Za-z0-9_-]{3,64}"),
    re.compile(r"(?i)(?:[A-Z]:\\(?:[^\\\r\n]+\\)*[^\\\r\n]*|/(?:home|Users|var|tmp)/[^\s,，；;]+)"),
    re.compile(r"(?i)\\\\[^\\\s]+\\[^\r\n,，；;]+"),
)


def contains_sensitive_text(value: str) -> bool:
    text = str(value or "")
    return any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS)


def redact_text(value: str) -> str:
    text = str(value or "")
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub("[已脱敏]", text)
    return text


def redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: "[已脱敏]" if key.casefold() in {
                "password", "token", "authorization", "cookie", "csrf", "patient_name",
                "patient_id", "id_number", "phone", "file_path",
            } else redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_payload(item) for item in value]
    return value


class PrivacyLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(str(record.msg))
        if record.args:
            record.args = tuple(redact_text(str(item)) for item in record.args)
        return True


def install_privacy_log_filter() -> None:
    root = logging.getLogger()
    if not any(isinstance(item, PrivacyLogFilter) for item in root.filters):
        root.addFilter(PrivacyLogFilter())
    for handler in root.handlers:
        if not any(isinstance(item, PrivacyLogFilter) for item in handler.filters):
            handler.addFilter(PrivacyLogFilter())
