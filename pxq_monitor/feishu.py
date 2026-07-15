"""飞书企业自建应用：长连接收消息、回复指令并主动推送卡片。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass

import httpx

from .notify import build_card

log = logging.getLogger("pxq.feishu")

OPEN_API_BASE = "https://open.feishu.cn/open-apis"
TOKEN_REFRESH_MARGIN = 60
MESSAGE_DEDUPE_TTL = 3600.0


class FeishuError(RuntimeError):
    pass


@dataclass(frozen=True)
class IncomingCommand:
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    text: str
    is_admin: bool


class FeishuGateway:
    def __init__(
        self,
        config: dict,
        command_queue: asyncio.Queue[IncomingCommand] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.app_id = str(config.get("feishu_app_id") or "").strip()
        self.app_secret = str(config.get("feishu_app_secret") or "").strip()
        self.default_chat_id = str(config.get("feishu_default_chat_id") or "").strip()
        raw_admins = config.get("feishu_admin_open_ids") or []
        if not isinstance(raw_admins, list):
            raise FeishuError("feishu_admin_open_ids 必须是字符串数组")
        self.admin_open_ids = {str(item).strip() for item in raw_admins if str(item).strip()}
        if not self.app_id or not self.app_secret:
            raise FeishuError("缺少 feishu_app_id 或 feishu_app_secret")

        self.command_queue = command_queue
        self._http = http_client or httpx.AsyncClient(timeout=15)
        self._owns_http = http_client is None
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._processed_messages: dict[str, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_client = None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ---- 应用身份与消息 API ----

    async def _tenant_access_token(self, force_refresh: bool = False) -> str:
        now = time.time()
        if not force_refresh and self._token and now < self._token_expires_at:
            return self._token
        async with self._token_lock:
            now = time.time()
            if not force_refresh and self._token and now < self._token_expires_at:
                return self._token
            response = await self._http.post(
                f"{OPEN_API_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0 or not payload.get("tenant_access_token"):
                raise FeishuError(
                    f"获取 tenant_access_token 失败：{payload.get('code')} {payload.get('msg')}"
                )
            expires_in = int(payload.get("expire") or 7200)
            self._token = payload["tenant_access_token"]
            self._token_expires_at = now + max(1, expires_in - TOKEN_REFRESH_MARGIN)
            return self._token

    async def _post_api(self, path: str, payload: dict) -> dict:
        for attempt in range(2):
            token = await self._tenant_access_token(force_refresh=attempt > 0)
            response = await self._http.post(
                f"{OPEN_API_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") == 0:
                return data
            if attempt == 0 and data.get("code") in (99991663, 99991668):
                self._token = ""
                continue
            raise FeishuError(f"飞书 API 失败：{data.get('code')} {data.get('msg')}")
        raise FeishuError("飞书访问凭证刷新失败")

    async def send_card(
        self, title: str, body_md: str, template: str = "red"
    ) -> bool:
        if not self.default_chat_id:
            log.error("未配置 feishu_default_chat_id，无法主动推送")
            return False
        try:
            await self._post_api(
                "/im/v1/messages?receive_id_type=chat_id",
                {
                    "receive_id": self.default_chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(
                        build_card(title, body_md, template), ensure_ascii=False
                    ),
                },
            )
            return True
        except Exception as exc:
            log.error("应用机器人推送失败: %s", exc)
            return False

    async def reply_text(self, message_id: str, text: str) -> bool:
        try:
            await self._post_api(
                f"/im/v1/messages/{message_id}/reply",
                {
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            )
            return True
        except Exception as exc:
            log.error("回复飞书消息失败 [%s]: %s", message_id, exc)
            return False

    # ---- 长连接事件 ----

    async def run_forever(self) -> None:
        if self.command_queue is None:
            raise FeishuError("未配置指令队列，无法启动飞书长连接")
        self._loop = asyncio.get_running_loop()
        stopped = self._loop.create_future()

        def start_client() -> None:
            try:
                # lark-oapi 1.x 的 ws.Client 使用模块级事件循环，必须在实际
                # 运行 start() 的线程内创建并绑定，否则会与主 asyncio 循环冲突。
                sdk_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(sdk_loop)
                try:
                    import lark_oapi as lark
                    import lark_oapi.ws.client as ws_client_module
                except ImportError as exc:
                    raise FeishuError(
                        "未安装 lark-oapi，请先执行 pip install -r requirements.txt"
                    ) from exc
                ws_client_module.loop = sdk_loop

                def on_message(data) -> None:
                    try:
                        payload = json.loads(lark.JSON.marshal(data))
                        if self._loop is not None:
                            self._loop.call_soon_threadsafe(
                                self._dispatch_payload, payload
                            )
                    except Exception:
                        log.exception("解析飞书消息事件失败")

                handler = (
                    lark.EventDispatcherHandler.builder("", "")
                    .register_p2_im_message_receive_v1(on_message)
                    .build()
                )
                self._ws_client = lark.ws.Client(
                    self.app_id,
                    self.app_secret,
                    event_handler=handler,
                    log_level=lark.LogLevel.INFO,
                )
                log.info("正在建立飞书长连接")
                self._ws_client.start()
            except BaseException as exc:
                self._loop.call_soon_threadsafe(
                    self._finish_ws, stopped, exc
                )
            else:
                self._loop.call_soon_threadsafe(
                    self._finish_ws, stopped, FeishuError("飞书长连接意外停止")
                )

        # SDK 的 start() 是阻塞调用。使用 daemon 线程可以让 systemd 停止服务时
        # 不被默认线程池的 shutdown 等待卡住；事件仍通过 call_soon_threadsafe 回主循环。
        threading.Thread(
            target=start_client,
            name="feishu-ws",
            daemon=True,
        ).start()
        await stopped

    @staticmethod
    def _finish_ws(future: asyncio.Future, exc: BaseException) -> None:
        if not future.done():
            future.set_exception(exc)

    def _dispatch_payload(self, payload: dict) -> None:
        asyncio.create_task(self.handle_event_payload(payload))

    async def handle_event_payload(self, payload: dict) -> None:
        """处理已反序列化的消息事件；公开此方法便于离线测试。"""
        event = payload.get("event") or payload
        sender = event.get("sender") or {}
        if sender.get("sender_type") != "user":
            return
        message = event.get("message") or {}
        if message.get("message_type") != "text":
            return

        message_id = str(message.get("message_id") or "")
        chat_id = str(message.get("chat_id") or "")
        chat_type = str(message.get("chat_type") or "")
        sender_id = sender.get("sender_id") or {}
        open_id = str(sender_id.get("open_id") or "")
        if not message_id or not chat_id or not open_id:
            log.warning("忽略字段不完整的飞书消息事件")
            return
        log.info("收到飞书消息：open_id=%s chat_id=%s chat_type=%s",
                 open_id, chat_id, chat_type)

        now = time.time()
        self._cleanup_dedupe(now)
        if message_id in self._processed_messages:
            return

        is_admin = open_id in self.admin_open_ids
        if chat_type == "p2p" and not is_admin:
            log.warning("单聊用户未授权：open_id=%s chat_id=%s", open_id, chat_id)
            self._processed_messages[message_id] = now
            await self.reply_text(message_id, "无操作权限")
            return
        if chat_type != "p2p" and not (message.get("mentions") or []):
            return

        try:
            content = json.loads(message.get("content") or "{}")
            text = str(content.get("text") or "")
        except (TypeError, json.JSONDecodeError):
            return
        for mention in message.get("mentions") or []:
            key = str(mention.get("key") or "")
            if key:
                text = text.replace(key, "")
        text = text.strip()
        if not text:
            return

        command = IncomingCommand(
            message_id=message_id,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_open_id=open_id,
            text=text,
            is_admin=is_admin,
        )
        try:
            self.command_queue.put_nowait(command)
        except asyncio.QueueFull:
            log.error("飞书指令队列已满，丢弃消息 %s", message_id)
            await self.reply_text(message_id, "当前指令过多，请稍后重试")
            return
        self._processed_messages[message_id] = now

    def _cleanup_dedupe(self, now: float) -> None:
        expired = [
            message_id
            for message_id, received_at in self._processed_messages.items()
            if now - received_at >= MESSAGE_DEDUPE_TTL
        ]
        for message_id in expired:
            self._processed_messages.pop(message_id, None)
