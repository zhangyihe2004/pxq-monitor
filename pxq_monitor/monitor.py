"""监控引擎：轮询票星球接口，检测锁定场次的余票变化并推送提醒。"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time
from dataclasses import dataclass

from .api import PxqClient
from .db import Database
from .notify import Notifier

log = logging.getLogger("pxq.monitor")

ERROR_ALERT_COOLDOWN = 3600.0
MAX_BACKOFF_FACTOR = 16
MAX_CONCURRENT_POLLS = 3
PLAN_MISSING_THRESHOLD = 3
SESSION_GONE_THRESHOLD = 3  # 锁定场次连续 N 轮不在接口里才判定消失（防抖）
ABUNDANT_THRESHOLD = 9999  # 票星球用 999999 表示"库存充足"


def format_count(count: int) -> str:
    return "充足" if count >= ABUNDANT_THRESHOLD else f"{count} 张"


def parse_picked(raw: str) -> set[str]:
    """票档过滤串 -> seatPlanId 集合；空集合表示监控全部。"""
    return {t for t in (raw or "").split(",") if t}


def format_status(row) -> str:
    """统一格式化 API 票档或数据库票档的可售状态。"""
    if row["can_buy_count"] > 0 and row["sale_started"]:
        return f"有票·{format_count(row['can_buy_count'])}"
    return "可售但无票" if row["sale_started"] else "未开售"


@dataclass
class PendingNotice:
    show_name: str
    session_name: str
    due: list[dict]
    sent: bool = False
    pause_on_success: bool = False


class MonitorEngine:
    def __init__(self, db: Database, client: PxqClient, notifier: Notifier):
        self.db = db
        self.client = client
        self.notifier = notifier
        self._failures: dict[int, int] = {}
        self._next_run: dict[int, float] = {}
        self._session_gone: dict[int, int] = {}  # watch_id -> 锁定场次连续缺失轮数
        self._plan_missing: dict[int, dict[str, int]] = {}
        self._last_error_alert: dict[int, float] = {}  # watch_id -> 上次异常告警时间
        self._pending_notices: dict[int, list[PendingNotice]] = {}

    # ---- 调度循环 ----

    async def run_forever(self) -> None:
        log.info("监控引擎启动")
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("监控调度循环异常，将在下一轮重试")
            await asyncio.sleep(2)

    async def tick(self) -> None:
        """并发抓取到期监控；同一演出在本轮共享场次动态请求。"""
        watches = self.db.list_watches(status="active")
        active_ids = {watch["id"] for watch in watches}
        self._prune_runtime_state(active_ids)

        now = time.monotonic()
        due = [
            watch for watch in watches if self._next_run.get(watch["id"], 0.0) <= now
        ]
        if not due:
            return

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_POLLS)
        session_tasks: dict[str, asyncio.Task[dict]] = {}

        async def poll_due(watch: sqlite3.Row) -> None:
            async with semaphore:
                show_id = watch["show_id"]
                task = session_tasks.get(show_id)
                if task is None:
                    task = asyncio.create_task(self.client.sessions_dynamic(show_id))
                    session_tasks[show_id] = task
                await self._poll_safely(watch, task)

        await asyncio.gather(*(poll_due(watch) for watch in due))

    async def _poll_safely(
        self, watch: sqlite3.Row, sessions_task: asyncio.Task[dict]
    ) -> None:
        wid = watch["id"]
        try:
            await self.poll_watch(watch, await sessions_task)
            if self._failures.get(wid):
                log.info("监控 #%s 恢复正常", wid)
            self._failures[wid] = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures = self._failures.get(wid, 0) + 1
            self._failures[wid] = failures
            log.warning("监控 #%s 抓取失败（连续 %s 次）: %s", wid, failures, exc)
            if failures >= 5:
                try:
                    await self._alert_error(wid, watch["show_name"], failures, exc)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("监控 #%s 异常告警发送失败", wid)

        try:
            current = self.db.get_watch(wid)
            if not current or current["status"] != "active":
                self._clear_runtime_state(wid)
                return
            interval = current["interval_sec"] * random.uniform(0.9, 1.2)
            interval *= min(2 ** self._failures.get(wid, 0), MAX_BACKOFF_FACTOR)
            self._next_run[wid] = time.monotonic() + interval
        except Exception:
            log.exception("监控 #%s 更新下次调度时间失败", wid)

    def _prune_runtime_state(self, active_ids: set[int]) -> None:
        for mapping in (
            self._failures,
            self._next_run,
            self._session_gone,
            self._last_error_alert,
        ):
            for watch_id in set(mapping) - active_ids:
                mapping.pop(watch_id, None)
        for watch_id in set(self._plan_missing) - active_ids:
            self._plan_missing.pop(watch_id, None)
        for watch_id in set(self._pending_notices) - active_ids:
            self._pending_notices.pop(watch_id, None)

    def _clear_runtime_state(self, watch_id: int) -> None:
        for mapping in (
            self._failures,
            self._next_run,
            self._session_gone,
            self._last_error_alert,
        ):
            mapping.pop(watch_id, None)
        self._plan_missing.pop(watch_id, None)
        self._pending_notices.pop(watch_id, None)

    # ---- 单个监控项（锁定一个场次）----

    async def poll_watch(self, watch: sqlite3.Row, dyn: dict) -> None:
        wid, show_id = watch["id"], watch["show_id"]
        show_name = watch["show_name"] or show_id
        session_vos = dyn["sessionVOs"]
        pinned = watch["session_id"]
        svo = next((s for s in session_vos if s["bizShowSessionId"] == pinned), None)
        if svo is not None:
            # 场次恢复后，尚未送达的“场次消失”提醒已经过期，不能再补发或暂停。
            self._drop_pending_pause_notices(wid)
            self._session_gone.pop(wid, None)
        acknowledged = await self._retry_pending_notices(wid)
        if acknowledged is None:
            return

        prev_session = self.db.get_session_state(wid, pinned)
        session_name = prev_session["session_name"] if prev_session else ""

        if svo is None:
            await self._handle_session_gone(wid, show_name, pinned, session_name)
            return

        if not session_name:
            static = await self.client.sessions_static(show_id)
            session_name = next(
                (
                    session.get("sessionName", "")
                    for session in static["sessionVOs"]
                    if session["bizShowSessionId"] == pinned
                ),
                "",
            )
            session_name = session_name or pinned[-6:]

        pending: list[dict] = []  # 本轮触发的提醒，最后合并成一张卡片发送

        # 场次状态只记日志不发卡：状态翻成在售不代表真有票（有票自会触发回流卡）
        new_status = svo.get("sessionStatus") or ""
        if prev_session and prev_session["session_status"] != new_status:
            log.info(
                "监控 #%s 场次状态变化 %s → %s [%s]",
                wid,
                prev_session["session_status"],
                new_status,
                session_name,
            )
        plan_states = self.db.get_plan_states(wid)
        pdyn = await self.client.seat_plans_dynamic(show_id, pinned)
        plans = pdyn["seatPlans"]

        # 出现未知票档时补一次静态数据拿名称/价格
        plan_meta: dict[str, dict] = {}
        if any(p["seatPlanId"] not in plan_states for p in plans):
            pstatic = await self.client.seat_plans_static(show_id, pinned)
            plan_meta = {plan["seatPlanId"]: plan for plan in pstatic["seatPlans"]}

        current = self.db.get_watch(wid)
        if (
            not current
            or current["status"] != "active"
            or current["session_id"] != pinned
        ):
            log.info("监控 #%s 已变更，丢弃本轮网络结果", wid)
            return

        picked = parse_picked(current["price_filter"])
        available = 0
        total = 0
        snapshot: list[tuple[str, str, float, int, bool]] = []
        current_plan_ids = {plan["seatPlanId"] for plan in plans}
        missing_rounds = self._plan_missing.setdefault(wid, {})
        for plan_id in current_plan_ids:
            missing_rounds.pop(plan_id, None)

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
            can_buy = sale_started and count > 0
            total += int(watched)
            available += int(watched and can_buy)

            if watched:
                count_str = format_count(count)
                base = {
                    "plan": f"{name}（¥{price:g}）",
                    "count": count_str,
                    "line": f"{name}（¥{price:g}）可购 **{count_str}**",
                }
                if prev is None:
                    # 票档首次入库，天然只会发生一次
                    if can_buy:
                        pending.append(
                            {
                                **base,
                                "key": f"{pid}:initial",
                                "kind": "initial",
                                "label": "当前在售",
                                "title": "🎫 当前在售",
                            }
                        )
                else:
                    if not prev["sale_started"] and sale_started:
                        pending.append(
                            {
                                **base,
                                "key": f"{pid}:onsale",
                                "kind": "onsale",
                                "label": "票档开售",
                                "title": "🎫 票档开售",
                            }
                        )
                    # 归零后再有票才触发；有票期间数量波动不重复提醒
                    elif (
                        not (prev["sale_started"] and prev["can_buy_count"] > 0)
                        and can_buy
                    ):
                        pending.append(
                            {
                                **base,
                                "key": f"{pid}:restock",
                                "kind": "restock",
                                "label": "有回流票",
                                "title": "🎫 有回流票",
                            }
                        )
            snapshot.append((pid, name, price, count, sale_started))

        # 动态接口连续三轮缺少旧票档时将其库存归零；短暂残缺不会制造假回流。
        for pid, previous in plan_states.items():
            if pid in current_plan_ids:
                continue
            rounds = min(
                missing_rounds.get(pid, 0) + 1,
                PLAN_MISSING_THRESHOLD,
            )
            missing_rounds[pid] = rounds
            if rounds >= PLAN_MISSING_THRESHOLD:
                snapshot.append(
                    (
                        pid,
                        previous["plan_name"],
                        previous["price"],
                        0,
                        bool(previous["sale_started"]),
                    )
                )

        pending = [alert for alert in pending if alert["key"] not in acknowledged]
        if pending and not await self._send_notice(
            wid, show_name, session_name, pending
        ):
            log.warning("监控 #%s 提醒未送达，本轮状态不推进，下一轮继续尝试", wid)
            return
        if not self.db.apply_snapshot(wid, pinned, session_name, new_status, snapshot):
            log.info("监控 #%s 已停用，丢弃本轮状态快照", wid)
            self._clear_runtime_state(wid)
            return
        self._pending_notices.pop(wid, None)
        log.info("监控 #%s [%s] 有票票档 %s/%s", wid, show_name, available, total)

    async def _handle_session_gone(
        self, wid: int, show_name: str, pinned: str, session_name: str
    ) -> None:
        """锁定的场次从接口消失：连续确认后提醒并自动暂停。"""
        rounds = self._session_gone.get(wid, 0) + 1
        self._session_gone[wid] = rounds
        log.info("监控 #%s [%s] 锁定场次不在接口中（第 %s 轮）", wid, show_name, rounds)
        if rounds < SESSION_GONE_THRESHOLD:
            return
        current = self.db.get_watch(wid)
        if (
            not current
            or current["status"] != "active"
            or current["session_id"] != pinned
        ):
            self._clear_runtime_state(wid)
            return
        sent = await self._send_notice(
            wid,
            show_name,
            session_name or pinned,
            [
                {
                    "kind": "gone",
                    "key": "session:gone",
                    "label": "场次已移除",
                    "title": "⚠️ 场次已结束或下架",
                    "line": "场次已从票星球移除（可能已结束、取消或延期），监控已自动暂停。",
                }
            ],
            pause_on_success=True,
        )
        if not sent or not self.db.set_watch_status(wid, "paused"):
            return
        self._pending_notices.pop(wid, None)
        self._session_gone.pop(wid, None)
        log.info("监控 #%s [%s] 已自动暂停", wid, show_name)

    # ---- 提醒合并发送 ----

    def _drop_pending_pause_notices(self, watch_id: int) -> None:
        notices = self._pending_notices.get(watch_id)
        if not notices:
            return
        remaining = [notice for notice in notices if not notice.pause_on_success]
        if remaining:
            self._pending_notices[watch_id] = remaining
        else:
            self._pending_notices.pop(watch_id, None)

    async def _retry_pending_notices(self, watch_id: int) -> set[str] | None:
        notices = self._pending_notices.get(watch_id)
        if not notices:
            return set()
        current = self.db.get_watch(watch_id)
        if not current or current["status"] != "active":
            self._clear_runtime_state(watch_id)
            return None

        for notice in notices:
            if not notice.sent:
                notice.sent = await self._flush_alerts(
                    watch_id,
                    notice.show_name,
                    notice.session_name,
                    notice.due,
                )
                if not notice.sent:
                    return None
            if notice.pause_on_success:
                if self.db.set_watch_status(watch_id, "paused"):
                    self._clear_runtime_state(watch_id)
                    log.info("监控 #%s 已在待发提醒送达后自动暂停", watch_id)
                return None

        return {alert["key"] for notice in notices for alert in notice.due}

    async def _send_notice(
        self,
        watch_id: int,
        show_name: str,
        session_name: str,
        due: list[dict],
        pause_on_success: bool = False,
    ) -> bool:
        notice = PendingNotice(
            show_name,
            session_name,
            due,
            pause_on_success=pause_on_success,
        )
        self._pending_notices.setdefault(watch_id, []).append(notice)
        notice.sent = await self._flush_alerts(watch_id, show_name, session_name, due)
        return notice.sent

    async def _flush_alerts(
        self, watch_id: int, show_name: str, session_name: str, due: list[dict]
    ) -> bool:
        """把一轮触发的所有提醒合并为一张卡片，避免刷屏和飞书限频。"""
        if not due:
            return True

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
            body = (
                header + "\n" + "\n".join(f"· 【{a['label']}】{a['line']}" for a in due)
            )

        sent = await self.notifier.send_card(title, body, template=template)
        if sent:
            # 推送历史记入运行日志，排查"发没发过卡"时查 journalctl 即可
            log.info(
                "监控 #%s 已推送 [%s] %s 条：%s",
                watch_id,
                title,
                len(due),
                "；".join(a["label"] for a in due),
            )
        return sent

    async def _alert_error(
        self, watch_id: int, show_name: str, failures: int, exc: Exception
    ) -> None:
        """抓取连续失败告警，1 小时内不重复（重启后计时重置）。"""
        last = self._last_error_alert.get(watch_id)
        if last is not None and time.monotonic() - last < ERROR_ALERT_COOLDOWN:
            return
        body = (
            f"**{show_name}**\n连续 {failures} 次抓取失败，已自动放慢频率。\n"
            f"最近错误：{exc}"
        )
        if await self.notifier.send_card("⚠️ 监控异常", body, template="orange"):
            self._last_error_alert[watch_id] = time.monotonic()
