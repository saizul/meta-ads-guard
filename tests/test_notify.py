from typing import ClassVar

import pytest

from meta_ads_guard.notify import (
    EmailNotifier,
    NotifyError,
    TelegramNotifier,
    WebhookNotifier,
    chunk_text,
)

from .conftest import FakeTransport, respond


def test_telegram_send(transport):
    transport.add("POST", r"/bot123:abc/sendMessage$", respond({"ok": True, "result": {}}))
    tg = TelegramNotifier("tg", "123:abc", "-1001", transport=transport)
    tg.send("Alerts", "body text")
    call = transport.calls[0]
    assert call.url.startswith("https://api.telegram.org/bot123:abc/")
    assert call.json_body["chat_id"] == "-1001"
    assert call.json_body["text"] == "Alerts\n\nbody text"
    assert call.json_body["disable_web_page_preview"] is True


def test_telegram_splits_long_messages(transport):
    transport.add("POST", r"/sendMessage$", respond({"ok": True}))
    TelegramNotifier("tg", "1:a", "2", transport=transport).send("", ("x" * 100 + "\n") * 90)
    assert len(transport.calls) == 3
    assert all(len(c.json_body["text"]) <= 4096 for c in transport.calls)


def test_telegram_error(transport):
    transport.add("POST", r"/sendMessage$",
                  respond({"ok": False, "description": "Bad Request: chat not found"}, 400))
    with pytest.raises(NotifyError, match="chat not found"):
        TelegramNotifier("tg", "1:a", "2", transport=transport).send("s", "t")


def test_telegram_requires_values():
    with pytest.raises(ValueError):
        TelegramNotifier("tg", "", "1")


def test_webhook_send(transport):
    transport.add("POST", r"/services/T/B/X$", respond(None, 200))
    hook = WebhookNotifier("slack", "https://hooks.example.com/services/T/B/X",
                           transport=transport, extra={"username": "guard"})
    hook.send("Subject", "line")
    assert transport.calls[0].json_body == {"username": "guard", "text": "*Subject*\nline"}


def test_webhook_discord_field_and_error(transport):
    transport.add("POST", r"/api/webhooks/1/abc$", respond({"message": "nope"}, 401))
    hook = WebhookNotifier("d", "https://discord.example.com/api/webhooks/1/abc", field="content",
                           transport=transport)
    with pytest.raises(NotifyError, match="HTTP 401"):
        hook.send("", "x")
    assert "content" in transport.calls[0].json_body


def test_webhook_url_validation():
    with pytest.raises(ValueError):
        WebhookNotifier("x", "ftp://example.com")


class FakeSMTP:
    instances: ClassVar[list] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.log = []
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.log.append("starttls")

    def login(self, user, password):
        self.log.append(("login", user, password))

    def send_message(self, msg):
        self.log.append(("send", msg))

    def quit(self):
        self.log.append("quit")


@pytest.fixture(autouse=True)
def _reset_smtp():
    FakeSMTP.instances = []


def test_email_starttls_login_send():
    mail = EmailNotifier("m", host="smtp.example.com", sender="alerts@example.com",
                         recipients=["a@example.com", "b@example.com"], username="u",
                         password="p", smtp_factory=FakeSMTP)
    mail.send("2 alert(s)", "body")
    server = FakeSMTP.instances[0]
    assert server.port == 587
    assert server.log[0] == "starttls"
    assert server.log[1] == ("login", "u", "p")
    msg = server.log[2][1]
    assert msg["Subject"] == "[meta-ads-guard] 2 alert(s)"
    assert msg["To"] == "a@example.com, b@example.com"
    assert msg.get_content().strip() == "body"
    assert server.log[-1] == "quit"


def test_email_ssl_and_plain():
    EmailNotifier("m", host="h", sender="a@example.com", recipients=["b@example.com"], port=465,
                  security="ssl", ssl_factory=FakeSMTP).send("s", "t")
    assert FakeSMTP.instances[0].context is not None
    assert "starttls" not in FakeSMTP.instances[0].log
    EmailNotifier("m", host="h", sender="a@example.com", recipients=["b@example.com"],
                  security="none", smtp_factory=FakeSMTP).send("s", "t")
    assert not any(isinstance(x, tuple) and x[0] == "login" for x in FakeSMTP.instances[1].log)


def test_email_failure_is_wrapped():
    def broken(*a, **kw):
        raise OSError("connection refused")

    mail = EmailNotifier("m", host="h", sender="a@example.com", recipients=["b@example.com"],
                         smtp_factory=broken)
    with pytest.raises(NotifyError, match="connection refused"):
        mail.send("s", "t")


def test_email_validation():
    with pytest.raises(ValueError):
        EmailNotifier("m", host="h", sender="a", recipients=[])
    with pytest.raises(ValueError):
        EmailNotifier("m", host="h", sender="a", recipients=["b"], security="tls")


def test_chunk_text():
    assert chunk_text("short", 10) == ["short"]
    parts = chunk_text("aaaa\nbbbb\ncccc", 10)
    assert parts == ["aaaa\nbbbb", "cccc"]
    long_line = chunk_text("x" * 25, 10)
    assert [len(p) for p in long_line] == [10, 10, 5]
    with pytest.raises(ValueError):
        chunk_text("x", 0)


def test_fake_transport_unexpected_request():
    with pytest.raises(AssertionError):
        FakeTransport().request("GET", "https://example.com/x")
