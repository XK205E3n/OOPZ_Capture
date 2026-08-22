from __future__ import annotations

import asyncio
import json
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from oopz_capture.onebot_gateway import DiagnosticEchoBridge, OneBotGateway, OneBotGatewayConfig


ADMIN = "123456789"
GROUP = "987654321"
TOKEN = "integration-token-with-at-least-24-characters"


def test_local_onebot_websocket_feedback_without_oopz(tmp_path: Path) -> None:
    asyncio.run(_local_onebot_websocket_feedback_without_oopz(tmp_path))


async def _local_onebot_websocket_feedback_without_oopz(tmp_path: Path) -> None:
    observations: dict[str, object] = {}

    async def fake_napcat(websocket) -> None:
        observations["authorization"] = websocket.request.headers.get("Authorization")
        await websocket.send(json.dumps({
            "post_type": "message",
            "message_type": "group",
            "group_id": int(GROUP),
            "message_id": 100,
            "user_id": int(ADMIN),
            "raw_message": "该群消息不得被处理",
        }, ensure_ascii=False))
        await websocket.send(json.dumps({
            "post_type": "message",
            "message_type": "private",
            "message_id": 101,
            "user_id": 555555555,
            "raw_message": "非管理员私聊不得被处理",
        }, ensure_ascii=False))
        await websocket.send(json.dumps({
            "time": 1786630000,
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": 102,
            "user_id": int(ADMIN),
            "raw_message": "/oopz状态",
        }, ensure_ascii=False))
        while True:
            request = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2))
            if request["action"] == "get_status":
                await websocket.send(json.dumps({
                    "status": "ok", "retcode": 0,
                    "data": {"online": True, "good": True},
                    "echo": request["echo"],
                }))
                continue
            observations["action"] = request["action"]
            observations["params"] = request["params"]
            await websocket.send(json.dumps({
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": 103},
                "echo": request["echo"],
            }))
            break

    async with serve(fake_napcat, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = OneBotGatewayConfig(
            websocket_url=f"ws://127.0.0.1:{port}",
            access_token=TOKEN,
            admin_qq=ADMIN,
            report_group_qq=GROUP,
            report_friend_qq="112120116",
            state_root=tmp_path / "state",
        )
        gateway = OneBotGateway(config, DiagnosticEchoBridge(), mode="diagnostic_echo")
        async with connect(
            config.websocket_url,
            additional_headers={"Authorization": f"Bearer {TOKEN}"},
            proxy=None,
        ) as websocket:
            await gateway.run_connection(websocket)

    assert observations["authorization"] == f"Bearer {TOKEN}"
    assert observations["action"] == "send_private_msg"
    params = observations["params"]
    assert isinstance(params, dict)
    assert params["user_id"] == int(ADMIN)
    assert isinstance(params["message"], list)
    assert "不会登录 OOPZ" in params["message"][0]["data"]["text"]
    assert gateway.state.counters.group_events_discarded == 1
    assert gateway.state.counters.unauthorized_private_discarded == 1
    assert gateway.state.counters.replies_sent == 1
    state_text = (tmp_path / "state" / "onebot_gateway.json").read_text(encoding="utf-8")
    assert "该群消息不得被处理" not in state_text
    assert "非管理员私聊不得被处理" not in state_text
