"""Tests for the one-click Feishu app-registration setup flow."""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
from urllib.parse import parse_qs, urlparse

import pytest

from oopz_capture.feishu_setup import (
    FEISHU_ACCOUNT_DOMAIN,
    LARK_ACCOUNT_DOMAIN,
    REQUIRED_CALLBACKS,
    REQUIRED_TENANT_EVENTS,
    REQUIRED_TENANT_SCOPES,
    RegistrationError,
    build_registration_url,
    encode_addons,
    register_app,
    run_setup,
)


class FakeTransport:
    """Replays scripted responses; raises entries that are exception instances."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, domain: str, fields: dict[str, str]):
        self.calls.append((domain, dict(fields)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _begin(**overrides) -> dict:
    value = {
        "verification_uri_complete": "https://accounts.feishu.cn/qr_connect?token=abc123",
        "device_code": "device-1",
        "interval": 0,
        "expires_in": 600,
    }
    value.update(overrides)
    return value


def _success(app_id="cli_a1b2", secret="s3cret-value", **user) -> dict:
    return {
        "client_id": app_id,
        "client_secret": secret,
        "user_info": {"open_id": "ou_operator", "tenant_brand": "feishu", **user},
    }


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, amount: float) -> None:
        self.now += amount


def _decode_addons(encoded: str) -> dict:
    padded = encoded + "=" * (-len(encoded) % 4)
    raw = base64.b64decode(padded, altchars=b"-_")
    return json.loads(gzip.decompress(raw).decode("utf-8"))


def test_encode_addons_carries_exact_scopes_events_and_callbacks():
    payload = _decode_addons(encode_addons())
    assert payload == {
        "scopes": {"tenant": list(REQUIRED_TENANT_SCOPES), "user": []},
        "events": {"items": {"tenant": list(REQUIRED_TENANT_EVENTS), "user": []}},
        "callbacks": {"items": list(REQUIRED_CALLBACKS)},
    }
    assert len(REQUIRED_TENANT_SCOPES) == 11
    assert "im:message" not in REQUIRED_TENANT_SCOPES
    assert REQUIRED_TENANT_EVENTS == ("im.message.receive_v1", "p2.im.chat.member.bot.added_v1")
    assert REQUIRED_CALLBACKS == ("card.action.trigger",)
    assert "=" not in encode_addons()


def test_registration_url_appends_addons_preset_and_options():
    url = build_registration_url(_begin(), app_id="cli_old", create_only=True)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert url.startswith(_begin()["verification_uri_complete"] + "&")
    assert parsed.path == "/qr_connect"
    assert params["token"] == ["abc123"]
    assert params["from"] == ["sdk"]
    assert params["tp"] == ["sdk"]
    assert params["source"] == ["oopz-capture"]
    assert params["clientID"] == ["cli_old"]
    assert params["createOnly"] == ["true"]
    assert params["name"] == ["OOPZ 管理机器人"]
    assert "desc" in params
    assert _decode_addons(params["addons"][0])["scopes"]["tenant"][0] == REQUIRED_TENANT_SCOPES[0]

    plain = build_registration_url(_begin())
    assert "clientID" not in parse_qs(urlparse(plain).query)
    assert "createOnly" not in parse_qs(urlparse(plain).query)


def test_register_app_polls_until_success_without_waiting_when_confirmed():
    transport = FakeTransport([
        _begin(),
        {"error": "authorization_pending"},
        _success(app_id="cli_new", secret="value-secret"),
    ])
    sleeps: list[float] = []
    lines: list[str] = []
    shown: list[str] = []
    result = register_app(
        transport=transport,
        show_qr=shown.append,
        print_line=lines.append,
        sleep=sleeps.append,
        clock=FakeClock(),
    )
    assert result.app_id == "cli_new"
    assert result.app_secret == "value-secret"
    assert result.operator_open_id == "ou_operator"
    assert result.tenant_brand == "feishu"
    assert transport.calls[0][1]["action"] == "begin"
    assert transport.calls[1] == (FEISHU_ACCOUNT_DOMAIN, {"action": "poll", "device_code": "device-1"})
    assert shown and shown[0].startswith("https://accounts.feishu.cn/qr_connect")
    assert any("二维码" in line for line in lines)
    assert sleeps == [0.0]


def test_register_app_slow_down_adds_backoff():
    transport = FakeTransport([_begin(), {"error": "slow_down"}, _success()])
    sleeps: list[float] = []
    register_app(
        transport=transport, show_qr=lambda url: None,
        print_line=lambda line: None, sleep=sleeps.append, clock=FakeClock(),
    )
    assert sleeps == [5.0]


def test_register_app_switches_to_lark_domain_for_international_tenants():
    transport = FakeTransport([
        _begin(),
        {"error": "authorization_pending", "user_info": {"tenant_brand": "lark"}},
        _success(tenant_brand="lark"),
    ])
    result = register_app(
        transport=transport, show_qr=lambda url: None,
        print_line=lambda line: None, sleep=lambda seconds: None, clock=FakeClock(),
    )
    assert result.tenant_brand == "lark"
    poll_domains = [domain for domain, fields in transport.calls if fields.get("action") == "poll"]
    assert poll_domains == [FEISHU_ACCOUNT_DOMAIN, LARK_ACCOUNT_DOMAIN]


def test_register_app_access_denied_is_terminal():
    transport = FakeTransport([
        _begin(),
        {"error": "access_denied", "error_description": "user denied"},
    ])
    with pytest.raises(RegistrationError) as excinfo:
        register_app(
            transport=transport, show_qr=lambda url: None,
            print_line=lambda line: None, sleep=lambda seconds: None, clock=FakeClock(),
        )
    assert excinfo.value.code == "access_denied"
    assert "user denied" in excinfo.value.description


def test_register_app_times_out_when_confirmation_never_happens():
    transport = FakeTransport([_begin(expires_in=2)] + [{"error": "authorization_pending"}] * 10)
    clock = FakeClock()
    sleeps: list[float] = []

    def advancing_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.tick(1.0)

    with pytest.raises(RegistrationError) as excinfo:
        register_app(
            transport=transport, show_qr=lambda url: None,
            print_line=lambda line: None, sleep=advancing_sleep, clock=clock,
        )
    assert excinfo.value.code == "expired_token"


def test_run_setup_writes_credentials_and_reports_next_steps(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text('OOPZ_FEISHU_APP_SECRET=old-secret\n', encoding="utf-8")
    monkeypatch.delenv("OOPZ_FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("OOPZ_FEISHU_APP_SECRET", raising=False)
    transport = FakeTransport([_begin(), _success(app_id="cli_new", secret="brand-new-secret")])
    lines: list[str] = []
    code = run_setup(
        env_path=env_path,
        print_line=lines.append,
        transport=transport,
        sleep=lambda seconds: None,
        clock=FakeClock(),
    )
    assert code == 0
    text = env_path.read_text(encoding="utf-8")
    assert "OOPZ_FEISHU_APP_ID=cli_new" in text
    assert "OOPZ_FEISHU_APP_SECRET=brand-new-secret" in text
    joined = "\n".join(lines)
    assert "brand-new-secret" not in joined
    assert "cli_new" in joined
    assert "版本" in joined and "控制群" in joined
    assert any("长度 16" in line for line in lines)


def test_run_setup_refuses_to_replace_another_apps_credentials_without_force(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("OOPZ_FEISHU_APP_ID=cli_old\nOOPZ_FEISHU_APP_SECRET=keep-me\n", encoding="utf-8")
    monkeypatch.setenv("OOPZ_FEISHU_APP_ID", "cli_old")
    monkeypatch.setenv("OOPZ_FEISHU_APP_SECRET", "keep-me")
    transport = FakeTransport([_begin(), _success(app_id="cli_other", secret="other-secret")])
    lines: list[str] = []
    code = run_setup(
        env_path=env_path,
        force=False,
        print_line=lines.append,
        transport=transport,
        sleep=lambda seconds: None,
        clock=FakeClock(),
    )
    assert code == 1
    assert env_path.read_text(encoding="utf-8") == "OOPZ_FEISHU_APP_ID=cli_old\nOOPZ_FEISHU_APP_SECRET=keep-me\n"
    assert any("--force" in line for line in lines)

    transport = FakeTransport([_begin(), _success(app_id="cli_other", secret="other-secret")])
    code = run_setup(
        env_path=env_path,
        force=True,
        print_line=lines.append,
        transport=transport,
        sleep=lambda seconds: None,
        clock=FakeClock(),
    )
    assert code == 0
    text = env_path.read_text(encoding="utf-8")
    assert "OOPZ_FEISHU_APP_ID=cli_other" in text
    assert "OOPZ_FEISHU_APP_SECRET=other-secret" in text


def test_run_setup_targets_existing_env_app_id_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OOPZ_FEISHU_APP_ID", "cli_existing")
    monkeypatch.setenv("OOPZ_FEISHU_APP_SECRET", "secret")
    transport = FakeTransport([_begin(), _success(app_id="cli_existing", secret="rotated")])
    shown: list[str] = []
    code = run_setup(
        env_path=tmp_path / ".env",
        show_qr=shown.append,
        print_line=lambda line: None,
        transport=transport,
        sleep=lambda seconds: None,
        clock=FakeClock(),
    )
    assert code == 0
    assert len(shown) == 1
    assert "clientID=cli_existing" in shown[0]


def test_run_setup_network_failure_prints_manual_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("OOPZ_FEISHU_APP_ID", raising=False)
    transport = FakeTransport([OSError("network unreachable")])
    lines: list[str] = []
    code = run_setup(
        env_path=tmp_path / ".env",
        print_line=lines.append,
        transport=transport,
        sleep=lambda seconds: None,
        clock=FakeClock(),
    )
    assert code == 1
    joined = "\n".join(lines)
    assert "README_FEISHU_BOT_SETUP.md" in joined
    assert "im:message.group_at_msg:readonly" in joined
    assert "card.action.trigger" in joined
    assert not (tmp_path / ".env").exists() or "APP_ID" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_manual_fallback_mentions_every_required_scope():
    from oopz_capture.feishu_setup import manual_fallback_text

    text = manual_fallback_text()
    for scope in REQUIRED_TENANT_SCOPES:
        assert scope in text
    for event in REQUIRED_TENANT_EVENTS:
        assert event in text
    assert not re.search(r"cli_[A-Za-z0-9]+", text)
