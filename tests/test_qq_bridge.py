"""QQ bridge: configuration, the retry rules of the relay contract, the Feishu notices, and the controller hook."""
import asyncio
import json
import urllib.error
from pathlib import Path

import pytest

from oopz_capture.controller import ControllerConfig, ControllerService
from oopz_capture.controller_protocol import SenderPolicy
from oopz_capture.qq_bridge import BridgeConfig, BridgeConfigError, deliver_digest, failure_notice, request_id_for, send_image

ENV = {"OOPZ_QQ_GROUP_ID": "910614272", "OOPZ_QQ_RELAY_TOKEN": "secret-token-secret-token-1234"}
CONFIG = BridgeConfig.from_env(ENV)


class Script:
    """A scripted endpoint: each call pops the next reply (an exception is raised)."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, body))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def run(*replies, image=None):
    waits, post = [], Script(*replies)
    result = send_image(CONFIG, image or Path("img.png"), "oopz-x-12345678", post=post, sleep=waits.append)
    return result, post, waits


def test_config_is_off_without_a_group_and_strict_otherwise():
    assert BridgeConfig.from_env({}) is None
    assert CONFIG.target_id == "910614272" and CONFIG.url == "http://127.0.0.1:18764"
    assert "secret-token" not in repr(CONFIG)
    with pytest.raises(BridgeConfigError, match="TOKEN"):
        BridgeConfig.from_env({"OOPZ_QQ_GROUP_ID": "910614272"})
    with pytest.raises(BridgeConfigError, match="group number"):
        BridgeConfig.from_env(ENV | {"OOPZ_QQ_GROUP_ID": "abc"})
    for url in ("http://example.com:18764", "https://127.0.0.1:18764", "http://10.0.0.5:18764", "http://127.0.0.1:1/v1"):
        with pytest.raises(BridgeConfigError, match="this machine"):
            BridgeConfig.from_env(ENV | {"OOPZ_QQ_RELAY_URL": url})
    assert BridgeConfig.from_env(ENV | {"OOPZ_QQ_RELAY_URL": "http://localhost:19000/"}).url == "http://localhost:19000"


def test_request_id_is_stable_for_an_image_and_changes_with_it(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"one"), b.write_bytes(b"two")
    first = request_id_for("2026-10-05_02-02-26_BJT", a)
    assert first == request_id_for("2026-10-05_02-02-26_BJT", a) and first != request_id_for("2026-10-05_02-02-26_BJT", b)
    assert first.startswith("oopz-2026-10-05_02-02-26_BJT-") and 8 <= len(first) <= 80


def test_sent_is_final_and_the_token_goes_only_in_the_header():
    result, post, waits = run((200, {"status": "sent", "image_message_id": "1"}))
    assert result["status"] == "sent" and result["attempts"] == 1 and waits == []
    url, headers, body = post.calls[0]
    assert url == "http://127.0.0.1:18764/v1/send" and headers["Authorization"] == "Bearer " + ENV["OOPZ_QQ_RELAY_TOKEN"]
    assert body["target"] == {"type": "group", "id": "910614272"} and "text" not in body and "token" not in json.dumps(body)
    assert ENV["OOPZ_QQ_RELAY_TOKEN"] not in json.dumps(result)


def test_failed_is_retried_with_the_same_id_then_succeeds():
    result, post, waits = run((200, {"status": "failed", "error": "x"}), (200, {"status": "sent"}))
    assert result["status"] == "sent" and result["attempts"] == 2 and len(waits) == 1
    assert [c[2]["request_id"] for c in post.calls] == ["oopz-x-12345678"] * 2


def test_unknown_and_partial_are_never_retried():
    for status in ("unknown", "partial"):
        result, post, _ = run((200, {"status": status, "error": "e"}))
        assert result["status"] == status and len(post.calls) == 1


def test_rate_limit_waits_for_retry_after_then_retries():
    result, post, waits = run((429, {"error": "rate_limited", "retry_after": 12}), (200, {"status": "sent"}))
    assert result["status"] == "sent" and waits == [12.0] and len(post.calls) == 2


def test_other_refusals_stop_at_once():
    for code, error in ((403, "target_blocked"), (401, "unauthorized"), (503, "disabled"), (502, "session_unavailable"),
                        (409, "request_id_conflict"), (400, "invalid_target")):
        result, post, _ = run((code, {"error": error}))
        assert result["status"] == "rejected" and result["error"] == error and len(post.calls) == 1


def test_refused_connection_is_retried_but_a_timeout_is_unknown():
    result, post, _ = run(ConnectionRefusedError(), ConnectionRefusedError(), (200, {"status": "sent"}))
    assert result["status"] == "sent" and result["attempts"] == 3
    result, post, _ = run(urllib.error.URLError(ConnectionRefusedError()), (200, {"status": "sent"}))
    assert result["status"] == "sent"
    result, post, _ = run(TimeoutError())
    assert result["status"] == "unknown" and result["error"] == "timeout" and len(post.calls) == 1
    result, post, _ = run(urllib.error.URLError(TimeoutError()))
    assert result["status"] == "unknown" and len(post.calls) == 1


def test_gives_up_after_the_attempt_limit():
    result, post, waits = run(*[(200, {"status": "failed", "error": "x"})] * 4)
    assert result["status"] == "failed" and result["attempts"] == 4 and len(post.calls) == 4 and len(waits) == 3


def test_deliver_writes_the_outcome_without_the_token(tmp_path):
    session = tmp_path / "2026-10-05_02-02-26_BJT"
    (session / "analysis").mkdir(parents=True)
    image = session / "analysis" / "digest.png"
    image.write_bytes(b"png")
    seen = {}

    def send(config, path, request_id):
        seen.update(path=path, request_id=request_id)
        return {"status": "sent", "attempts": 1}

    assert deliver_digest(session, image, env={}, send=send) is None and not seen              # bridge off
    result = deliver_digest(session, image, env=ENV, send=send)
    assert result["status"] == "sent" and result["target"] == "group:910614272" and seen["path"] == image
    saved = (session / "analysis" / "qq_relay.json").read_text(encoding="utf-8")
    assert "sent" in saved and ENV["OOPZ_QQ_RELAY_TOKEN"] not in saved
    broken = deliver_digest(session, image, env={"OOPZ_QQ_GROUP_ID": "910614272"}, send=send)
    assert broken["status"] == "misconfigured" and "TOKEN" in broken["error"]


def test_notices_only_for_outcomes_that_need_a_person():
    assert failure_notice(None) is None and failure_notice({"status": "sent"}) is None
    assert "黑名单" in failure_notice({"status": "rejected", "error": "target_blocked"})
    assert "连不上 MaiBot" in failure_notice({"status": "failed", "error": "connection:ConnectionRefusedError"})
    unknown = failure_notice({"status": "unknown", "error": "timeout"})
    assert "不确定" in unknown and "不会自动重试" in unknown
    assert "没有启用" in failure_notice({"status": "misconfigured", "error": "x"})


def controller(tmp_path, qq_runner):
    config = ControllerConfig(output_root=tmp_path / "output", state_root=tmp_path / "state",
                              authorization=SenderPolicy(frozenset({"m"}), frozenset({"g"})))

    def analyse(session_dir):
        png = session_dir / "analysis" / "digest" / "digest.png"
        png.parent.mkdir(parents=True)
        png.write_bytes(b"png")
        md = png.with_name("digest.md")
        md.write_text("t", encoding="utf-8")
        return {"png": str(png), "md": str(md), "usage_text": "用量"}

    service = ControllerService(config, analysis_runner=analyse, qq_runner=qq_runner)
    session = tmp_path / "output" / "s1"
    session.mkdir(parents=True)
    service._state["last_job"] = {"session_id": "s1", "status": "analyzing"}
    service._analysis_sessions.add("s1")
    return service, session


def queued_texts(service):
    from oopz_capture.send_request import list_send_requests
    return [(q["source"], q["text"]) for q in list_send_requests(service.state_root, statuses={"pending"})]


@pytest.mark.parametrize("outcome, notice", [
    ({"status": "sent"}, False), (None, False),
    ({"status": "rejected", "error": "target_blocked"}, True), ({"status": "unknown", "error": "timeout"}, True)])
def test_the_controller_sends_qq_after_feishu_and_reports_only_problems(tmp_path, outcome, notice):
    calls = []
    service, session = controller(tmp_path, lambda s, i: calls.append((s.name, i.name)) or outcome)
    asyncio.run(service._analyze_and_deliver(session))
    assert calls == [("s1", "digest.png")]
    sources = [source for source, _ in queued_texts(service)]
    assert ("qq_bridge" in sources) is notice
    assert service._state["last_job"]["status"] == "analysis_completed"
    assert service._state["last_job"].get("qq_relay") == (outcome or {}).get("status")


def test_a_crashing_qq_runner_never_fails_the_analysis(tmp_path):
    def boom(session, image):
        raise RuntimeError("socket gone")

    service, session = controller(tmp_path, boom)
    asyncio.run(service._analyze_and_deliver(session))
    assert service._state["last_job"]["status"] == "analysis_completed"
    notices = [text for source, text in queued_texts(service) if source == "qq_bridge"]
    assert len(notices) == 1 and "QQ 群发图失败" in notices[0]


def test_real_http_round_trip_against_a_local_stub(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers.get("Authorization")
            seen["length"] = self.headers.get("Content-Length")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            code, payload = (429, {"error": "rate_limited", "retry_after": 1}) if "first" not in seen else (200, {"status": "sent"})
            seen["first"] = True
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = BridgeConfig.from_env(ENV | {"OOPZ_QQ_RELAY_URL": f"http://127.0.0.1:{server.server_address[1]}"})
        image = tmp_path / "d.png"
        image.write_bytes(b"png")
        waits = []
        result = send_image(config, image, "oopz-real-12345678", sleep=waits.append)
    finally:
        server.shutdown()
    assert result["status"] == "sent" and result["attempts"] == 2 and waits == [1.0]
    assert seen["auth"] == "Bearer " + ENV["OOPZ_QQ_RELAY_TOKEN"] and int(seen["length"]) > 0
    assert seen["body"]["image_path"] == str(image.resolve()) and seen["body"]["target"]["id"] == "910614272"


def test_nothing_listening_is_a_failed_send_not_a_crash(tmp_path):
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = BridgeConfig.from_env(ENV | {"OOPZ_QQ_RELAY_URL": f"http://127.0.0.1:{port}"})
    waits = []
    result = send_image(config, tmp_path / "x.png", "oopz-none-12345678", sleep=waits.append)
    assert result["status"] == "failed" and result["error"].startswith("connection:") and result["attempts"] == 4


def test_whole_chain_from_the_controller_to_a_local_endpoint(tmp_path, monkeypatch):
    """Controller -> default runner -> real HTTP -> stub endpoint: the image path arrives as the real path
    of the digest, once, with a request id made from the session and the image."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            data = b'{"status":"sent"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OOPZ_QQ_GROUP_ID", "910614272")
    monkeypatch.setenv("OOPZ_QQ_RELAY_TOKEN", ENV["OOPZ_QQ_RELAY_TOKEN"])
    monkeypatch.setenv("OOPZ_QQ_RELAY_URL", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        from oopz_capture.qq_bridge import deliver_digest
        service, session = controller(tmp_path, deliver_digest)
        asyncio.run(service._analyze_and_deliver(session))
    finally:
        server.shutdown()
    digest = session / "analysis" / "digest" / "digest.png"
    assert len(received) == 1 and received[0]["image_path"] == str(digest.resolve())
    assert received[0]["request_id"].startswith("oopz-s1-") and received[0]["target"] == {"type": "group", "id": "910614272"}
    assert [s for s, _ in queued_texts(service)].count("qq_bridge") == 0
    assert service._state["last_job"]["qq_relay"] == "sent"
    assert json.loads((session / "analysis" / "qq_relay.json").read_text(encoding="utf-8"))["status"] == "sent"
