"""CLI 与飞书指令共用的监控业务服务。"""

from __future__ import annotations

import re
import sqlite3

from .api import PxqClient
from .db import Database

SHOW_ID_RE = re.compile(r"[0-9a-f]{24}")
MIN_INTERVAL = 15


def extract_show_id(target: str) -> str | None:
    """从 showId 或包含 showId 的链接中提取 24 位 ID。"""
    match = SHOW_ID_RE.search(target)
    return match.group(0) if match else None


def parse_plan_numbers(raw: str, upper: int) -> list[int]:
    """解析逗号分隔的票档编号；无效输入抛出 ValueError。"""
    try:
        numbers = sorted({
            int(part.strip())
            for part in raw.replace("，", ",").split(",")
            if part.strip()
        })
    except ValueError as exc:
        raise ValueError(f"票档编号需为逗号分隔的数字，如 3,5（收到：{raw}）") from exc
    bad = [number for number in numbers if not 1 <= number <= upper]
    if bad or not numbers:
        raise ValueError(f"票档编号超出范围（1~{upper}）：{bad or raw}")
    return numbers


class WatchService:
    def __init__(self, db: Database, client: PxqClient | None = None):
        self.db = db
        self.client = client

    def _require_client(self) -> PxqClient:
        if self.client is None:
            raise RuntimeError("该操作需要票星球 API 客户端")
        return self.client

    async def search(self, keyword: str, page: int = 1) -> list[dict]:
        return await self._require_client().search_shows(keyword, page=page)

    async def get_show_sessions(self, show_id: str) -> tuple[str, list[dict]]:
        static = await self._require_client().sessions_static(show_id)
        return static.get("showName") or show_id, static.get("sessionVOs") or []

    async def fetch_plans(self, show_id: str, session_id: str) -> list[dict]:
        """按票星球接口顺序返回带静态信息的票档。"""
        client = self._require_client()
        static = await client.seat_plans_static(show_id, session_id)
        meta = {plan["seatPlanId"]: plan for plan in static.get("seatPlans") or []}
        dynamic = await client.seat_plans_dynamic(show_id, session_id)
        plans = []
        for plan in dynamic.get("seatPlans") or []:
            info = meta.get(plan["seatPlanId"], {})
            plans.append({
                "pid": plan["seatPlanId"],
                "plan_name": info.get("seatPlanName") or plan["seatPlanId"][-6:],
                "price": float(info.get("originalPrice") or 0),
                "can_buy_count": int(plan.get("canBuyCount") or 0),
                "sale_started": bool(plan.get("saleStarted")),
            })
        return plans

    def add_watch(
        self,
        show_id: str,
        show_name: str,
        session_id: str,
        interval: int,
        picked_ids: set[str],
    ) -> tuple[int, bool]:
        """添加监控；已存在时返回现有 ID 和 False。"""
        existing = self.db.find_watch(show_id, session_id)
        if existing:
            return existing["id"], False
        watch_id = self.db.add_watch(
            show_id,
            show_name,
            max(interval, MIN_INTERVAL),
            ",".join(sorted(picked_ids)),
            session_id,
        )
        return watch_id, True

    def list_watches(self) -> list[sqlite3.Row]:
        return self.db.list_watches()

    def get_watch(self, watch_id: int) -> sqlite3.Row | None:
        return self.db.get_watch(watch_id)

    def get_plan_states(self, watch_id: int) -> dict[str, sqlite3.Row]:
        return self.db.get_plan_states(watch_id)

    def get_session_states(self, watch_id: int) -> dict[str, sqlite3.Row]:
        return self.db.get_session_states(watch_id)

    def set_status(self, watch_id: int, status: str) -> bool:
        if not self.db.get_watch(watch_id):
            return False
        self.db.set_watch_status(watch_id, status)
        return True

    def remove_watch(self, watch_id: int) -> bool:
        if not self.db.get_watch(watch_id):
            return False
        self.db.remove_watch(watch_id)
        return True

    def set_interval(self, watch_id: int, seconds: int) -> int | None:
        if not self.db.get_watch(watch_id):
            return None
        interval = max(seconds, MIN_INTERVAL)
        self.db.set_watch_interval(watch_id, interval)
        return interval

    def set_picked_ids(self, watch_id: int, picked_ids: set[str]) -> bool:
        if not self.db.get_watch(watch_id):
            return False
        self.db.set_watch_filter(watch_id, ",".join(sorted(picked_ids)))
        return True
