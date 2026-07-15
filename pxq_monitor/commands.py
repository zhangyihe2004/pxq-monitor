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
SEARCH_COMMAND = "搜索 <关键词>"
ADD_COMMAND = (
    "监控 <搜索序号|showId|链接> [场次 <序号>] [票档 <编号列表|全部>] [间隔 <秒>]"
)
PICK_COMMAND = "票档 <监控ID> <编号列表|全部>"
INTERVAL_COMMAND = "间隔 <监控ID> <秒>"

HELP_TEXT = f"""票星球余票监控

【查询】
{SEARCH_COMMAND}
列表
帮助

【添加】
监控 <搜索序号|showId|链接>
机器人会继续提示选择场次和票档；间隔默认 60 秒。

【管理】
暂停 <监控ID>
恢复 <监控ID>
删除 <监控ID>
{PICK_COMMAND}
{INTERVAL_COMMAND}

最低间隔 15 秒；群聊使用时请先 @机器人。"""


def _send(command: str) -> str:
    return f"发送：{command}"


def _plan_line(
    index: int, name: str, price: float, status: str, selected: bool = True
) -> str:
    mark = "✓" if selected else "✗"
    return f"{mark} {index}. {name}｜¥{price:g}｜{status}"


def _plans_text(plans: list[dict], picked_ids: set[str]) -> str:
    lines = []
    for index, plan in enumerate(plans, 1):
        lines.append(
            _plan_line(
                index,
                plan["plan_name"],
                plan["price"],
                format_status(plan),
                not picked_ids or plan["pid"] in picked_ids,
            )
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
                response = "执行失败，请查看服务日志。"
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
            return "无操作权限。"
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
        return f"无法识别指令：{parts[0]}\n\n{HELP_TEXT}"

    @staticmethod
    def _context_key(message: IncomingCommand) -> tuple[str, str]:
        return message.chat_id, message.sender_open_id

    async def _search(self, message: IncomingCommand, parts: list[str]) -> str:
        if len(parts) < 2:
            return _send(SEARCH_COMMAND)
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
            return "搜索结果（0）\n\n未找到相关演出或赛事。"
        self._search_results[key] = (now, shows)
        lines = [f"搜索结果（{len(shows)}）"]
        for index, show in enumerate(shows, 1):
            sold = "已售罄" if show.get("soldOut") else show.get("showStatus", "")
            location = " ".join(
                value
                for value in (show.get("cityName", ""), show.get("venueName", ""))
                if value
            )
            lines.append(
                f"\n{index}. {show.get('showName')}\n"
                f"   时间：{show.get('showDate') or '未知'}\n"
                f"   地点：{location or '未知'}\n"
                f"   状态：{sold or '未知'}\n"
                f"   showId：{show.get('showId')}"
            )
        return "\n".join(lines) + "\n\n" + _send("监控 <搜索序号>")

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
            return _send(ADD_COMMAND)
        show_id = self._resolve_target(message, parts[1])
        if not show_id:
            return f"无法解析监控目标。\n\n{_send(SEARCH_COMMAND)}"

        options = {"场次": "", "票档": "", "间隔": "60"}
        index = 2
        while index < len(parts):
            key = parts[index]
            if key not in options or index + 1 >= len(parts):
                return _send(ADD_COMMAND)
            options[key] = parts[index + 1]
            index += 2
        try:
            session_number = int(options["场次"] or 0)
            interval = max(int(options["间隔"]), MIN_INTERVAL)
        except ValueError:
            return f"场次序号和间隔必须是数字。\n\n{_send(ADD_COMMAND)}"

        show_name, sessions = await self.service.get_show_sessions(show_id)
        if not sessions:
            return f"无法添加监控\n演出：{show_name}\n原因：当前没有场次。"
        if len(sessions) > 1 and not session_number:
            lines = [f"演出：{show_name}", f"场次（{len(sessions)}）："]
            lines.extend(
                f"{number}. {session.get('sessionName', '')}"
                for number, session in enumerate(sessions, 1)
            )
            return "\n".join(lines) + "\n\n" + _send(f"监控 {parts[1]} 场次 <序号>")
        selected = session_number or 1
        if not 1 <= selected <= len(sessions):
            return f"场次序号超出范围：1~{len(sessions)}。\n\n" + _send(
                f"监控 {parts[1]} 场次 <序号>"
            )
        session = sessions[selected - 1]
        session_id = session["bizShowSessionId"]
        plans = await self.service.fetch_plans(show_id, session_id)

        plan_option = options["票档"].strip().lower()
        if not plan_option:
            plan_choice = "<编号列表|全部>" if plans else "全部"
            return (
                f"演出：{show_name}\n"
                f"场次：{session.get('sessionName', '')}\n\n"
                f"票档（{len(plans)}）：\n{_plans_text(plans, set())}\n\n"
                + _send(
                    f"监控 {parts[1]} 场次 {selected} 票档 {plan_choice} [间隔 <秒>]"
                )
            )

        picked_ids: set[str] = set()
        if plan_option not in ("all", "全部"):
            try:
                numbers = parse_plan_numbers(plan_option, len(plans))
            except ValueError as exc:
                return f"{exc}\n\n" + _send(
                    f"监控 {parts[1]} 场次 {selected} 票档 <编号列表|全部> [间隔 <秒>]"
                )
            picked_ids = {plans[number - 1]["pid"] for number in numbers}

        watch_id, created = self.service.add_watch(
            show_id, show_name, session_id, interval, picked_ids
        )
        if not created:
            return f"无需重复添加\n监控：#{watch_id}\n原因：该场次已在监控。"
        scope = f"已选 {len(picked_ids)} 个票档" if picked_ids else "全部票档"
        return (
            f"已添加监控 #{watch_id}\n"
            f"演出：{show_name}\n"
            f"场次：{session.get('sessionName', '')}\n"
            f"票档：{scope}\n"
            f"间隔：{interval} 秒\n\n"
            f"{_plans_text(plans, picked_ids)}"
        )

    def _list(self) -> str:
        watches = self.service.list_watches()
        if not watches:
            return "监控列表（0）\n\n暂无监控项。"
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
            status = "运行中" if watch["status"] == "active" else "已暂停"
            pinned = watch["session_id"] or ""
            session_name = "待首次抓取"
            if pinned:
                session = self.service.get_session_state(watch["id"], pinned)
                if session:
                    session_name = session["session_name"]
            lines = [
                f"监控 #{watch['id']}",
                f"演出：{watch['show_name']}",
                f"场次：{session_name}",
                f"状态：{status}",
                f"票档：有票 {available}/{len(watched)}",
                f"间隔：{watch['interval_sec']} 秒",
            ]
            if plans:
                lines.append(f"票档明细（{len(plans)}）：")
            else:
                lines.append("票档明细：尚未抓取")
            for number, plan in enumerate(plans.values(), 1):
                lines.append(
                    _plan_line(
                        number,
                        plan["plan_name"],
                        plan["price"],
                        format_status(plan),
                        not picked or plan["seat_plan_id"] in picked,
                    )
                )
            blocks.append("\n".join(lines))
        return f"监控列表（{len(watches)}）\n\n" + "\n\n".join(blocks)

    def _set_status(self, name: str, parts: list[str]) -> str:
        if len(parts) != 2 or not parts[1].isdigit():
            return _send(f"{name} <监控ID>")
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
        return f"已{name}监控 #{watch_id}。"

    async def _pick(self, parts: list[str]) -> str:
        if len(parts) != 3 or not parts[1].isdigit():
            return _send(PICK_COMMAND)
        watch_id = int(parts[1])
        watch = self.service.get_watch(watch_id)
        if not watch:
            return f"监控 #{watch_id} 不存在。"
        if parts[2].lower() in ("all", "全部"):
            if not self.service.set_picked_ids(watch_id, set()):
                return f"监控 #{watch_id} 不存在。"
            return f"已更新监控 #{watch_id}\n票档：全部票档"

        plans = await self.service.fetch_plans(watch["show_id"], watch["session_id"])
        try:
            numbers = parse_plan_numbers(parts[2], len(plans))
        except ValueError as exc:
            return f"{exc}\n\n{_send(PICK_COMMAND)}"
        picked_ids = {plans[number - 1]["pid"] for number in numbers}
        if not self.service.set_picked_ids(watch_id, picked_ids):
            return f"监控 #{watch_id} 不存在。"
        return (
            f"已更新监控 #{watch_id}\n"
            f"票档：已选 {len(picked_ids)} 个\n\n"
            f"{_plans_text(plans, picked_ids)}"
        )

    def _interval(self, parts: list[str]) -> str:
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            return _send(INTERVAL_COMMAND)
        watch_id = int(parts[1])
        interval = self.service.set_interval(watch_id, int(parts[2]))
        if interval is None:
            return f"监控 #{watch_id} 不存在。"
        return f"已更新监控 #{watch_id}\n间隔：{interval} 秒"
