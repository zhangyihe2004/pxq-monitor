import asyncio
import unittest

from pxq_monitor.commands import CommandWorker
from pxq_monitor.feishu import IncomingCommand


class FakeService:
    def __init__(self):
        self.add_calls = []

    async def search(self, keyword: str):
        return [{"showId": "0123456789abcdef01234567", "showName": keyword}]

    async def get_show_sessions(self, show_id: str):
        return "测试演出", [{
            "bizShowSessionId": "session-1",
            "sessionName": "测试场次",
        }]

    async def fetch_plans(self, show_id: str, session_id: str):
        return [{
            "pid": "plan-1",
            "plan_name": "80元",
            "price": 80.0,
            "can_buy_count": 0,
            "sale_started": True,
        }]

    def add_watch(self, show_id, show_name, session_id, interval, picked_ids):
        self.add_calls.append((show_id, session_id, interval, picked_ids))
        return 1, True


class FakeFeishu:
    async def reply_text(self, message_id: str, text: str):
        return True


def incoming(text: str, is_admin: bool = True) -> IncomingCommand:
    return IncomingCommand(
        message_id="om_1",
        chat_id="oc_1",
        chat_type="p2p",
        sender_open_id="ou_1",
        text=text,
        is_admin=is_admin,
    )


class CommandWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = FakeService()
        self.worker = CommandWorker(asyncio.Queue(), self.service, FakeFeishu())

    async def test_non_admin_cannot_mutate(self) -> None:
        response = await self.worker.execute(incoming("暂停 1", is_admin=False))
        self.assertEqual(response, "无操作权限")

    async def test_search_result_can_be_used_by_add(self) -> None:
        await self.worker.execute(incoming("搜索 测试"))
        response = await self.worker.execute(incoming("监控 1"))

        self.assertIn("已添加监控 #1", response)
        self.assertEqual(len(self.service.add_calls), 1)

    async def test_search_index_expires_with_empty_memory(self) -> None:
        response = await self.worker.execute(incoming("监控 1"))
        self.assertIn("请先发送", response)


if __name__ == "__main__":
    unittest.main()
