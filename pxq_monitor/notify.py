"""飞书群自定义机器人推送；未配置 webhook 时降级为控制台输出。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

import httpx

log = logging.getLogger("pxq.notify")


class Notifier:
    def __init__(self, webhook: str | None = None, secret: str | None = None):
        self.webhook = (webhook or "").strip()
        self.secret = (secret or "").strip()

    @staticmethod
    def _sign(secret: str, timestamp: int) -> str:
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    async def _post(self, payload: dict, fallback_text: str) -> bool:
        if not self.webhook:
            print(f"[通知] {fallback_text}")
            return True
        if self.secret:
            timestamp = int(time.time())
            payload = {"timestamp": str(timestamp), "sign": self._sign(self.secret, timestamp), **payload}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.webhook, json=payload)
            data = resp.json()
            code = data.get("code", data.get("StatusCode"))
            if code != 0:
                log.error("飞书推送失败: %s", data)
                return False
            return True
        except Exception as exc:
            log.error("飞书推送异常: %s", exc)
            return False

    async def send_card(self, title: str, body_md: str, template: str = "red") -> bool:
        """发送消息卡片。body_md 使用飞书 lark_md 语法。"""
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": template,
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": body_md}},
                    {
                        "tag": "note",
                        "elements": [{
                            "tag": "plain_text",
                            "content": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }],
                    },
                ],
            },
        }
        return await self._post(payload, fallback_text=f"{title}\n{body_md}")
