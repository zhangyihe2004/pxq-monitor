import asyncio
import json
import unittest
from unittest.mock import AsyncMock

import httpx

from pxq_monitor.feishu import FeishuGateway


def config() -> dict:
    return {
        "feishu_app_id": "cli_test",
        "feishu_app_secret": "secret",
        "feishu_admin_open_ids": ["ou_admin"],
        "feishu_default_chat_id": "oc_notify",
    }


def message_event(
    message_id: str = "om_1",
    open_id: str = "ou_admin",
    chat_type: str = "p2p",
    text: str = "列表",
    mentions: list | None = None,
) -> dict:
    return {
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": open_id},
            },
            "message": {
                "message_id": message_id,
                "chat_id": "oc_command",
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "mentions": mentions or [],
            },
        }
    }


class FeishuEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.queue = asyncio.Queue(maxsize=10)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(500)
        ))
        self.gateway = FeishuGateway(config(), self.queue, self.http)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()

    async def test_admin_p2p_message_is_queued(self) -> None:
        await self.gateway.handle_event_payload(message_event())

        command = self.queue.get_nowait()
        self.assertEqual(command.text, "列表")
        self.assertTrue(command.is_admin)

    async def test_duplicate_message_id_is_ignored(self) -> None:
        event = message_event()
        await self.gateway.handle_event_payload(event)
        await self.gateway.handle_event_payload(event)

        self.assertEqual(self.queue.qsize(), 1)

    async def test_non_admin_p2p_message_is_rejected(self) -> None:
        self.gateway.reply_text = AsyncMock(return_value=True)

        await self.gateway.handle_event_payload(message_event(open_id="ou_other"))

        self.assertTrue(self.queue.empty())
        self.gateway.reply_text.assert_awaited_once_with("om_1", "无操作权限")

    async def test_group_message_without_mention_is_ignored(self) -> None:
        await self.gateway.handle_event_payload(
            message_event(chat_type="group", mentions=[])
        )

        self.assertTrue(self.queue.empty())

    async def test_group_mention_placeholder_is_removed(self) -> None:
        await self.gateway.handle_event_payload(message_event(
            chat_type="group",
            text="@_user_1 列表",
            mentions=[{"key": "@_user_1"}],
        ))

        self.assertEqual(self.queue.get_nowait().text, "列表")


class FeishuSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_card_fetches_token_and_uses_app_message_api(self) -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/tenant_access_token/internal"):
                return httpx.Response(200, json={
                    "code": 0,
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                })
            self.assertEqual(request.headers["Authorization"], "Bearer tenant-token")
            body = json.loads(request.content)
            self.assertEqual(body["receive_id"], "oc_notify")
            self.assertEqual(body["msg_type"], "interactive")
            card = json.loads(body["content"])
            self.assertEqual(card["header"]["title"]["content"], "标题")
            return httpx.Response(200, json={"code": 0, "msg": "success"})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = FeishuGateway(config(), http_client=http)
        try:
            result = await gateway.send_card("标题", "内容", "green")
        finally:
            await http.aclose()

        self.assertTrue(result)
        self.assertEqual(len(requests), 2)


if __name__ == "__main__":
    unittest.main()
