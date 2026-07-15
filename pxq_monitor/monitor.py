"""监控引擎：轮询票星球接口，检测锁定场次的余票变化并推送提醒。"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time

from .api import PxqClient
from .db import Database
from .notify import Notifier

log = logging.getLogger("pxq.monitor")

ERROR_ALERT_COOLDOWN = 3600.0
MAX_BACKOFF_FACTOR = 16
SESSION_GONE_THRESHOLD = 3  # 锁定场次连续 N 轮不在接口里才判定消失（防抖）
ABUNDANT_THRESHOLD = 9999  # 票星球用 999999 表示"库存充足"


def format_count(count: int) -> str:
    return "充足" if count >= ABUNDANT_THRESHOLD else f"{count} 张"


def parse_picked(raw: str) -> set[str]:
    """票档过滤串 -> seatPlanId 集合；空集合表示监控全部。"""
    return {t for t in (raw or "").split(",") if t}


class MonitorEngine:
    def __init__(self, db: Database, client: PxqClient, notifier: Notifier):
        self.db = db
        self.client = client
        self.notifier = notifier
        self._failures: dict[int, int] = {}
        self._next_run: dict[int, float] = {}
        self._session_gone: dict[int, int] = {}  # watch_id -> 锁定场次连续缺失轮数
        self._last_error_alert: dict[int, float] = {}  # watch_id -> 上次异常告警时间

    # ---- 调度循环 ----

    async def run_forever(self) -> None:
        log.info("监控引擎启动")
        while True:
            await self.tick()
            await asyncio.sleep(2)

    async def tick(self) -> None:
        """对所有到期的活跃监控项各执行一轮抓取。"""
        for watch in self.db.list_watches(status="active"):
            wid = watch["id"]
            if self._next_run.get(wid, 0.0) > time.time():
                continue
            try:
                await self.poll_watch(watch)
                if self._failures.get(wid):
                    log.info("监控 #%s 恢复正常", wid)
                self._failures[wid] = 0
            except Exception as exc:
                failures = self._failures.get(wid, 0) + 1
                self._failures[wid] = failures
                log.warning("监控 #%s 抓取失败（连续 %s 次）: %s", wid, failures, exc)
                # 达到阈值后持续尝试告警；只有发送成功时 _alert_error 才会
                # 记录冷却时间，因此临时的飞书发送失败不会永久吞掉异常通知。
                if failures >= 5:
                    await self._alert_error(wid, watch["show_name"], failures, exc)
            interval = watch["interval_sec"] * random.uniform(0.9, 1.2)
            interval *= min(2 ** self._failures.get(wid, 0), MAX_BACKOFF_FACTOR)
            self._next_run[wid] = time.time() + interval

    # ---- 单个监控项（锁定一个场次）----

    async def poll_watch(self, watch: sqlite3.Row) -> None:
        wid, show_id = watch["id"], watch["show_id"]
        show_name = watch["show_name"] or show_id

        dyn = await self.client.sessions_dynamic(show_id)
        session_vos = dyn.get("sessionVOs") or []

        pinned = watch["session_id"]
        session_states = self.db.get_session_states(wid)
        prev_session = session_states.get(pinned)
        session_name = prev_session["session_name"] if prev_session else ""

        svo = next((s for s in session_vos if s["bizShowSessionId"] == pinned), None)
        if svo is None:
            await self._handle_session_gone(wid, show_name, pinned, session_name)
            return
        self._session_gone.pop(wid, None)

        if not session_name:
            static = await self.client.sessions_static(show_id)
            session_name = next(
                (s.get("sessionName", "") for s in static.get("sessionVOs") or []
                 if s["bizShowSessionId"] == pinned), "")

        pending: list[dict] = []  # 本轮触发的提醒，最后合并成一张卡片发送

        # 场次状态只记日志不发卡：状态翻成在售不代表真有票（有票自会触发回流卡）
        new_status = svo.get("sessionStatus") or ""
        if prev_session and prev_session["session_status"] != new_status:
            log.info("监控 #%s 场次状态变化 %s → %s [%s]",
                     wid, prev_session["session_status"], new_status, session_name)
        self.db.upsert_session_state(wid, pinned, session_name, new_status)

        plan_states = self.db.get_plan_states(wid)
        pdyn = await self.client.seat_plans_dynamic(show_id, pinned)
        plans = pdyn.get("seatPlans") or []

        # 出现未知票档时补一次静态数据拿名称/价格
        plan_meta: dict[str, dict] = {}
        if any(p["seatPlanId"] not in plan_states for p in plans):
            pstatic = await self.client.seat_plans_static(show_id, pinned)
            plan_meta = {p["seatPlanId"]: p for p in pstatic.get("seatPlans") or []}

        picked = parse_picked(watch["price_filter"])
        available = 0
        total = 0

        for plan in plans:
            pid = plan["seatPlanId"]
            count = int(plan.get("canBuyCount") or 0)
            sale_started = bool(plan.get("saleStarted"))
            prev = plan_states.get(pid)

            if prev is not None:
                name, price = prev["plan_name"], prev["price"]
            else:
                meta = plan_meta.get(pid, {})
                name = meta.get("seatPlanName") or pid[-6:]
                price = float(meta.get("originalPrice") or 0)

            watched = not picked or pid in picked
            total += watched
            available += watched and count > 0

            if watched:
                count_str = format_count(count)
                base = {
                    "plan": f"{name}（¥{price:g}）",
                    "count": count_str,
                    "line": f"{name}（¥{price:g}）可购 **{count_str}**",
                }
                if prev is None:
                    # 票档首次入库，天然只会发生一次
                    if count > 0 and sale_started:
                        pending.append({
                            **base,
                            "kind": "initial", "label": "当前在售", "title": "🎫 当前在售",
                        })
                else:
                    if not prev["sale_started"] and sale_started:
                        pending.append({
                            **base,
                            "kind": "onsale", "label": "票档开售", "title": "🎫 票档开售",
                        })
                    # 归零后再有票才触发；有票期间数量波动不重复提醒
                    if prev["can_buy_count"] == 0 and count > 0:
                        pending.append({
                            **base,
                            "kind": "restock", "label": "有回流票", "title": "🎫 有回流票",
                        })

            self.db.upsert_plan_state(wid, pinned, pid, name, price, count, sale_started)

        await self._flush_alerts(wid, show_name, session_name, pending)
        log.info("监控 #%s [%s] 有票票档 %s/%s", wid, show_name, available, total)

    async def _handle_session_gone(self, wid: int, show_name: str,
                                   pinned: str, session_name: str) -> None:
        """锁定的场次从接口消失：连续确认后提醒并自动暂停。"""
        rounds = self._session_gone.get(wid, 0) + 1
        self._session_gone[wid] = rounds
        log.info("监控 #%s [%s] 锁定场次不在接口中（第 %s 轮）", wid, show_name, rounds)
        if rounds < SESSION_GONE_THRESHOLD:
            return
        await self._flush_alerts(wid, show_name, session_name or pinned, [{
            "kind": "gone", "label": "场次已移除", "title": "⚠️ 场次已结束或下架",
            "line": "场次已从票星球移除（可能已结束、取消或延期），监控已自动暂停。",
        }])
        self.db.set_watch_status(wid, "paused")
        self._session_gone.pop(wid, None)
        log.info("监控 #%s [%s] 已自动暂停", wid, show_name)

    # ---- 提醒合并发送 ----

    async def _flush_alerts(self, watch_id: int, show_name: str,
                            session_name: str, due: list[dict]) -> None:
        """把一轮触发的所有提醒合并为一张卡片，避免刷屏和飞书限频。"""
        if not due:
            return

        # 颜色优先级：红（回流/开售）> 橙（场次消失）> 绿（在售快照）
        if any(a["kind"] in ("restock", "onsale") for a in due):
            template = "red"
        elif any(a["kind"] == "gone" for a in due):
            template = "orange"
        else:
            template = "green"

        header = f"**{show_name}**"
        if session_name:
            header += f"\n场次：{session_name}"

        if len(due) == 1:
            alert = due[0]
            title = alert["title"]
            if alert["kind"] == "gone":
                body = f"{header}\n{alert['line']}"
            else:
                body = f"{header}\n票档：{alert['plan']}\n可购：**{alert['count']}**"
        else:
            title = "🎫 余票提醒"
            body = header + "\n" + "\n".join(f"· 【{a['label']}】{a['line']}" for a in due)

        if await self.notifier.send_card(title, body, template=template):
            # 推送历史记入运行日志，排查"发没发过卡"时查 journalctl 即可
            log.info("监控 #%s 已推送 [%s] %s 条：%s",
                     watch_id, title, len(due), "；".join(a["label"] for a in due))

    async def _alert_error(self, watch_id: int, show_name: str,
                           failures: int, exc: Exception) -> None:
        """抓取连续失败告警，1 小时内不重复（重启后计时重置）。"""
        last = self._last_error_alert.get(watch_id)
        if last is not None and time.time() - last < ERROR_ALERT_COOLDOWN:
            return
        body = (f"**{show_name}**\n连续 {failures} 次抓取失败，已自动放慢频率。\n"
                f"最近错误：{exc}")
        if await self.notifier.send_card("⚠️ 监控异常", body, template="orange"):
            self._last_error_alert[watch_id] = time.time()
