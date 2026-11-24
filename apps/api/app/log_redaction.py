"""Redact credentials from every log record before it is emitted.

Helius RPC URLs carry the API key as a query parameter and Telegram Bot API
URLs embed the bot token in the path. Either can surface in httpx request
logs, exception messages, or tracebacks, so redaction is applied at the
handler level rather than relying on each call site.
"""

from __future__ import annotations

import logging
import re

REDACTED = "<redacted>"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ?api-key=..., &token=..., etc. (Helius uses "api-key").
    (
        re.compile(
            r"([?&](?:api[-_]?key|apikey|token|access[-_]?token|secret|key)=)[^&#\s'\"<>]+",
            re.IGNORECASE,
        ),
        rf"\1{REDACTED}",
    ),
    # https://api.telegram.org/bot<id>:<token>/sendMessage
    (re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+"), rf"\1{REDACTED}"),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Rewrites the rendered message, traceback and stack text of each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format args: leave for the formatter to report
            return True
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


_FILTER = RedactingFilter()


def install_log_redaction(logger_names: tuple[str, ...] = ("", "uvicorn", "uvicorn.error", "uvicorn.access")) -> None:
    """Attach the redacting filter to every handler of the given loggers (idempotent)."""
    for name in logger_names:
        for handler in logging.getLogger(name).handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
