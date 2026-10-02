"""Production log level and redaction.

Every handler the app logs through gets one filter that masks contact details
and credentials before a record is written, so a customer's email, phone, a
bearer token or a link token never reaches /var/log/optiwar. Operational
identifiers (order refs, AWBs, ticket UIDs, event ids, payment ids, status,
model, latency) are left as they are.

The level is LOG_LEVEL (default INFO). The HTTP client libraries the AI layer
uses are held at WARNING whatever LOG_LEVEL says: at DEBUG they write each
request body, which is the customer's chat text and prescription.
"""
import logging
import os
import re

QUIET_LIBRARIES = ("openai", "httpx", "httpcore", "urllib3")

_RULES = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b(token|access_token|api_key|apikey|key|secret|"
                r"signature|sig|password|otp)=([^&\s'\",]+)"), r"\1=<redacted>"),
    (re.compile(r"(/(?:f|face-scan/request)/)[A-Za-z0-9_-]{16,}"),
     r"\1<redacted>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?<![\w-])(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?![\w-])"),
     "<phone>"),
)


def redact(text):
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg, record.args = redact(msg), None
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(
                logging.Formatter().formatException(record.exc_info))
        return True


def configured_level(value=None):
    name = (os.environ.get("LOG_LEVEL", "") if value is None
            else value).strip().upper()
    level = logging.getLevelName(name) if name else logging.INFO
    return level if isinstance(level, int) else logging.INFO


def _guard(handler, level):
    handler.setLevel(level)
    if not any(isinstance(f, RedactingFilter) for f in handler.filters):
        handler.addFilter(RedactingFilter())


def install(app):
    """Apply the level and the filter to the app logger and the root logger.

    Called once the blueprints are imported: a module that ran
    logging.basicConfig(level=DEBUG) at import has already set the root level.
    """
    level = configured_level()
    app.logger.setLevel(level)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(app.logger.handlers) + list(root.handlers):
        _guard(handler, level)
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
    return level
