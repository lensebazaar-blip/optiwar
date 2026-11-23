import importlib
import importlib.util
import re
import sys
import types
from pathlib import Path

from jinja2 import Environment


ROOT = Path(__file__).resolve().parent
EU_OPS_ROOT = ROOT.parent
sys.path.insert(0, str(EU_OPS_ROOT))

config_stub = types.ModuleType("config")
services_stub = types.ModuleType("services")
services_stub.__path__ = [str(EU_OPS_ROOT / "services")]
ow_rx_stub = types.ModuleType("services.ow_rx")
ow_rx_stub.format_eye = lambda value: str(value)
services_stub.ow_rx = ow_rx_stub

saved_modules = {
    name: sys.modules.get(name)
    for name in ("config", "services", "services.ow_rx")
}
try:
    sys.modules["config"] = config_stub
    sys.modules["services"] = services_stub
    sys.modules["services.ow_rx"] = ow_rx_stub
    spec = importlib.util.spec_from_file_location(
        "_eu_ops_notifications", EU_OPS_ROOT / "services" / "notifications.py"
    )
    notifications = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = notifications
    spec.loader.exec_module(notifications)
finally:
    for name, module in saved_modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class FakeCursor:
    def __init__(self, rows, events, event_error=None):
        self.rows = rows
        self.events = events
        self.event_error = event_error
        self.executed = []
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))
        if "FROM ops_notification_logs" in query:
            self.result = self.rows
        elif "FROM msg91_delivery_events" in query:
            if self.event_error:
                raise self.event_error
            self.result = self.events
        else:
            raise AssertionError(f"Unexpected query: {query}")

    def fetchall(self):
        return [dict(row) for row in self.result]


def install_cursor(monkeypatch, rows, events=(), event_error=None):
    cursor = FakeCursor(rows, events, event_error)
    database_stub = types.ModuleType("database")
    database_stub.ow_cursor = lambda: cursor
    database_stub.db_cursor = lambda: None
    monkeypatch.setitem(sys.modules, "database", database_stub)
    return cursor


def notification(channel="whatsapp", reference="request-1", success=True):
    return {
        "id": 1,
        "channel": channel,
        "event": "status_update",
        "recipient": "customer",
        "success": success,
        "message": "message",
        "reference": reference,
        "created_by": "ops",
        "created_at": "2026-11-23 10:00:00",
    }


def test_failed_delivery_wins_over_later_delivered(monkeypatch):
    install_cursor(
        monkeypatch,
        [notification()],
        [
            {
                "msg91_request_id": "request-1",
                "status": "delivered",
                "failure_reason": None,
                "received_at": "2026-11-23 10:05:00",
            },
            {
                "msg91_request_id": "request-1",
                "status": "failed",
                "failure_reason": "template rejected",
                "received_at": "2026-11-23 10:04:00",
            },
        ],
    )

    row = notifications.get_notification_logs("OW-123")[0]

    assert row["delivery_status"] == "failed"
    assert row["delivery_reason"] == "template rejected"
    assert row["delivery_at"] == "2026-11-23 10:04:00"


def test_read_delivered_and_sent_priority(monkeypatch):
    rows = [
        notification(reference="read-request"),
        notification(reference="delivered-request"),
        notification(reference="sent-request"),
    ]
    events = [
        {"msg91_request_id": "read-request", "status": "sent"},
        {"msg91_request_id": "read-request", "status": "delivered"},
        {"msg91_request_id": "read-request", "status": "read"},
        {"msg91_request_id": "delivered-request", "status": "sent"},
        {"msg91_request_id": "delivered-request", "status": "delivered"},
        {"msg91_request_id": "sent-request", "status": "sent"},
    ]
    install_cursor(monkeypatch, rows, events)

    result = notifications.get_notification_logs("OW-123")

    assert [row["delivery_status"] for row in result] == ["read", "delivered", "sent"]


def test_email_is_untouched_and_whatsapp_without_report_has_no_delivery_keys(monkeypatch):
    email = notification(channel="email", reference="email-request")
    whatsapp = notification(reference="no-report")
    cursor = install_cursor(monkeypatch, [email, whatsapp])

    result = notifications.get_notification_logs("OW-123")

    assert result[0] == email
    assert "delivery_status" not in result[0]
    assert "delivery_status" not in result[1]
    assert cursor.executed[1][1] == ("no-report",)


def test_delivery_query_failure_returns_rows_without_delivery_info(monkeypatch, caplog):
    cursor = install_cursor(
        monkeypatch, [notification()], event_error=RuntimeError("missing table")
    )

    result = notifications.get_notification_logs("OW-123")

    assert len(cursor.executed) == 2
    assert "delivery_status" not in result[0]
    assert "Failed to fetch Optiwar WhatsApp delivery reports" in caplog.text


def test_notification_log_template_renders_every_delivery_state():
    template_source = (
        EU_OPS_ROOT / "templates/pages/ow_order_detail.html"
    ).read_text()
    match = re.search(
        r"<!-- Notification Log -->(.*?^\s{4}</div>\s*^\s*</div>)",
        template_source,
        re.DOTALL | re.MULTILINE,
    )
    assert match
    template = Environment(autoescape=True).from_string(match.group(0))
    rows = [
        {
            **notification(reference="failed-request"),
            "delivery_status": "failed",
            "delivery_reason": "template rejected",
            "delivery_at": "failed-at",
        },
        {
            **notification(reference="read-request"),
            "delivery_status": "read",
            "delivery_at": "read-at",
        },
        {
            **notification(reference="delivered-request"),
            "delivery_status": "delivered",
            "delivery_at": "delivered-at",
        },
        {
            **notification(reference="sent-request"),
            "delivery_status": "sent",
            "delivery_at": "sent-at",
        },
        notification(reference="pending-request"),
        notification(channel="email", reference="email-failed", success=False),
    ]

    rendered = template.render(
        notif_logs=rows,
        base="/ops",
        order={"order_id": "OW-123"},
    )

    for label in (
        "Failed (msg91)",
        "Read",
        "Delivered",
        "Sent",
        "Accepted - no delivery report yet",
        "Failed",
        "template rejected",
        "failed-at",
        "read-at",
        "delivered-at",
        "sent-at",
    ):
        assert label in rendered
