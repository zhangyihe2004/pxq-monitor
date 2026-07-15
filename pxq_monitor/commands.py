"""飞书文字指令解析与顺序执行。"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time

from .feishu import FeishuGateway, IncomingCommand
from .monitor import format_status, parse_picked
from .service import MIN_INTERVAL, WatchService, extract_show_id, parse_plan_numbers

log = logging.getLogger("pxq.commands")

MUTATING_COMMANDS = {"监控", "暂停", "恢复", "删除", "票档", "间隔"}
SEARCH_RESULT_TTL = 900.0

HELP_TEXT = """票星球余票监控指令

搜索 <关键词>
监控 <序号|showId|链接> [场次 N] [票档 3,5|全部] [间隔 秒]
列表
暂停 <id> / 恢复 <id> / 删除 <id>
票档 <id> <3,5|全部>
间隔 <id> <秒>
帮助"""


def _plans_text(plans: list[dict], picked_ids: set[str]) -> str:
    lines = []
    for index, plan in enumerate(plans, 1):
        mark = "✓" if not picked_ids or plan["pid"] in picked_ids else "✗"
        lines.append(
            f"{mark} {index}. {plan['plan_name']} ¥{plan['price']:g} "
            f"{format_status(plan)}"
        )
    return "\n".join(lines) if lines else "当前没有票档"


class CommandWorker:
    def __init__(
        self,
        queue: asyncio.Queue[IncomingCommand],
        service: WatchService,
        feishu: FeishuGateway,
    ):
        self.queue = queue
        self.service = service
        self.feishu = feishu
        self._search_results: dict[tuple[str, str], tuple[float, list[dict]]] = {}

    async def run_forever(self) -> None:
        log.info("飞书指令 worker 启动")
        while True:
            command = await self.queue.get()
            try:
                response = await self.execute(command)
            except Exception:
                log.exception(
                    "执行飞书指令失败 [%s] %s", command.message_id, command.text
                )
                response = "执行失败，请查看服务日志"
            try:
                await self.feishu.reply_text(command.message_id, response)
            finally:
                self.queue.task_done()

    async def execute(self, message: IncomingCommand) -> str:
        try:
            parts = shlex.split(message.text)
        except ValueError as exc:
            return f"指令格式错误：{exc}"
        if not parts:
            return HELP_TEXT
        name = parts[0].lower()
        if name in ("帮助", "help", "?"):
            return HELP_TEXT
        if name in MUTATING_COMMANDS and not message.is_admin:
            return "无操作权限"
        if name == "搜索":
            return await self._search(message, parts)
        if name == "监控":
            return await self._add(message, parts)
        if name == "列表":
            return self._list()
        if name in ("暂停", "恢复", "删除"):
            return self._set_status(name, parts)
        if name == "票档":
            return await self._pick(parts)
        if name == "间隔":
            return self._interval(parts)
        return f"未知指令：{parts[0]}\n\n{HELP_TEXT}"

    @staticmethod
    def _context_key(message: IncomingCommand) -> tuple[str, str]:
        return message.chat_id, message.sender_open_id

    async def _search(self, message: IncomingCommand, parts: list[str]) -> str:
        if len(parts) < 2:
            return "用法：搜索 <关键词>"
        keyword = " ".join(parts[1:]).strip()
        key = self._context_key(message)
        now = time.monotonic()
        self._search_results = {
            context: cached
            for context, cached in self._search_results.items()
            if now - cached[0] < SEARCH_RESULT_TTL
        }
        self._search_results.pop(key, None)
        shows = await self.service.search(keyword)
        if not shows:
            return "未找到相关演出/赛事。"
        self._search_results[key] = (now, shows)
        lines = []
        for index, show in enumerate(shows, 1):
            sold = "已售罄" if show.get("soldOut") else show.get("showStatus", "")
            lines.append(
                f"{index}. {show.get('showName')}\n"
                f"   {show.get('showDate', '')} | {show.get('cityName', '')} "
                f"{show.get('venueName', '')} | {sold}\n"
                f"   showId={show.get('showId')}"
            )
        return "\n".join(lines) + "\n\n发送：监控 <序号>"

    def _resolve_target(self, message: IncomingCommand, target: str) -> str | None:
        if target.isdigit() and len(target) <= 3:
            key = self._context_key(message)
            cached = self._search_results.get(key)
            if not cached or time.monotonic() - cached[0] >= SEARCH_RESULT_TTL:
                self._search_results.pop(key, None)
                return None
            index = int(target)
            shows = cached[1]
            if 1 <= index <= len(shows):
                return extract_show_id(str(shows[index - 1].get("showId") or ""))
            return None
        return extract_show_id(target)

    async def _add(self, message: IncomingCommand, parts: list[str]) -> str:
        if len(parts) < 2:
            return "用法：监控 <序号|showId|链接> [场次 N] [票档 3,5|全部] [间隔 秒]"
        show_id = self._resolve_target(message, parts[1])
        if not show_id:
            return "无法解析目标；若使用序号，请先发送“搜索 <关键词>”。"

        options = {"场次": "", "票档": "", "间隔": "60"}
        index = 2
        while index < len(parts):
            key = parts[index]
            if key not in options or index + 1 >= len(parts):
                return "可选参数：场次 N、票档 3,5|全部、间隔 秒"
            options[key] = parts[index + 1]
            index += 2
        try:
            session_number = int(options["场次"] or 0)
            interval = max(int(options["间隔"]), MIN_INTERVAL)
        except ValueError:
            return "场次和间隔必须是数字。"

        show_name, sessions = await self.service.get_show_sessions(show_id)
        if not sessions:
            return f"{show_name}：当前没有任何场次。"
        if len(sessions) > 1 and not session_number:
            lines = [f"{show_name} 有 {len(sessions)} 个场次："]
            lines.extend(
                f"{number}. {session.get('sessionName', '')}"
                for number, session in enumerate(sessions, 1)
            )
            return "\n".join(lines) + f"\n\n发送：监控 {parts[1]} 场次 <序号>"
        selected = session_number or 1
        if not 1 <= selected <= len(sessions):
            return f"场次序号超出范围：1~{len(sessions)}"
        session = sessions[selected - 1]
        session_id = session["bizShowSessionId"]
        plans = await self.service.fetch_plans(show_id, session_id)

        plan_option = options["票档"].strip().lower()
        if not plan_option:
            command = f"监控 {parts[1]} 场次 {selected} 票档 {{choice}} 间隔 {interval}"
            instructions = []
            if plans:
                example = "1" if len(plans) == 1 else "1,2"
                instructions.append(f"选择票档：{command.format(choice=example)}")
            instructions.append(f"监控全部：{command.format(choice='全部')}")
            return (
                f"{show_name}\n"
                f"场次：{session.get('sessionName', '')}\n\n"
                f"票档：\n{_plans_text(plans, set())}\n\n" + "\n".join(instructions)
            )

        picked_ids: set[str] = set()
        if plan_option not in ("all", "全部"):
            try:
                numbers = parse_plan_numbers(plan_option, len(plans))
            except ValueError as exc:
                return str(exc)
            picked_ids = {plans[number - 1]["pid"] for number in numbers}

        watch_id, created = self.service.add_watch(
            show_id, show_name, session_id, interval, picked_ids
        )
        if not created:
            return f"该场次已在监控中：#{watch_id}"
        scope = f"已选 {len(picked_ids)} 个票档" if picked_ids else "全部票档"
        return (
            f"已添加监控 #{watch_id}\n"
            f"{show_name}\n"
            f"场次：{session.get('sessionName', '')}\n"
            f"间隔：{interval}s；{scope}\n\n"
            f"{_plans_text(plans, picked_ids)}"
        )

    def _list(self) -> str:
        watches = self.service.list_watches()
        if not watches:
            return "暂无监控项。"
        blocks = []
        for watch in watches:
            plans = self.service.get_plan_states(watch["id"])
            picked = parse_picked(watch["price_filter"])
            watched = [
                plan
                for plan in plans.values()
                if not picked or plan["seat_plan_id"] in picked
            ]
            available = sum(
                1
                for plan in watched
                if plan["sale_started"] and plan["can_buy_count"] > 0
            )
            flag = "▶" if watch["status"] == "active" else "⏸"
            lines = [
                f"{flag} #{watch['id']} {watch['show_name']}",
                f"有票票档 {available}/{len(watched)}；间隔 {watch['interval_sec']}s",
            ]
            pinned = watch["session_id"] or ""
            if pinned:
                session = self.service.get_session_state(watch["id"], pinned)
                name = session["session_name"] if session else "待首次抓取"
                lines.append(f"场次：{name}")
            for number, plan in enumerate(plans.values(), 1):
                mark = "✓" if not picked or plan["seat_plan_id"] in picked else "✗"
                lines.append(
                    f"{mark} {number}. {plan['plan_name']} ¥{plan['price']:g} "
                    f"{format_status(plan)}"
                )
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _set_status(self, name: str, parts: list[str]) -> str:
        if len(parts) != 2 or not parts[1].isdigit():
            return f"用法：{name} <监控ID>"
        watch_id = int(parts[1])
        if name == "删除":
            return (
                f"已删除监控 #{watch_id}。"
                if self.service.remove_watch(watch_id)
                else f"监控 #{watch_id} 不存在。"
            )
        status = "paused" if name == "暂停" else "active"
        if not self.service.set_status(watch_id, status):
            return f"监控 #{watch_id} 不存在。"
        return f"监控 #{watch_id} 已{name}。"

    async def _pick(self, parts: list[str]) -> str:
        if len(parts) != 3 or not parts[1].isdigit():
            return "用法：票档 <监控ID> <3,5|全部>"
        watch_id = int(parts[1])
        watch = self.service.get_watch(watch_id)
        if not watch:
            return f"监控 #{watch_id} 不存在。"
        if parts[2].lower() in ("all", "全部"):
            if not self.service.set_picked_ids(watch_id, set()):
                return f"监控 #{watch_id} 不存在。"
            return f"监控 #{watch_id} 现在监控全部票档。"

        plans = await self.service.fetch_plans(watch["show_id"], watch["session_id"])
        try:
            numbers = parse_plan_numbers(parts[2], len(plans))
        except ValueError as exc:
            return str(exc)
        picked_ids = {plans[number - 1]["pid"] for number in numbers}
        if not self.service.set_picked_ids(watch_id, picked_ids):
            return f"监控 #{watch_id} 不存在。"
        return (
            f"监控 #{watch_id} 现在监控 {len(picked_ids)} 个票档：\n"
            f"{_plans_text(plans, picked_ids)}"
        )

    def _interval(self, parts: list[str]) -> str:
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            return "用法：间隔 <监控ID> <秒>"
        watch_id = int(parts[1])
        interval = self.service.set_interval(watch_id, int(parts[2]))
        if interval is None:
            return f"监控 #{watch_id} 不存在。"
        return f"监控 #{watch_id} 轮询间隔已改为 {interval}s。"
