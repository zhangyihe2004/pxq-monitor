"""命令行入口：搜索比赛、管理监控项、运行监控守护进程。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx

from .api import PxqClient, PxqError
from .commands import CommandWorker
from .db import Database
from .feishu import FeishuError, FeishuGateway, IncomingCommand
from .monitor import MonitorEngine, format_count, parse_picked
from .service import MIN_INTERVAL, WatchService, extract_show_id, parse_plan_numbers

BASE_DIR = Path(os.environ.get("PXQ_MONITOR_DIR", Path.home() / ".pxq-monitor"))
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "monitor.db"
SEARCH_CACHE_PATH = BASE_DIR / "last_search.json"

DEFAULT_CONFIG = {
    "feishu_app_id": "",
    "feishu_app_secret": "",
    "feishu_admin_open_ids": [],
    "feishu_default_chat_id": "",
}

def load_config() -> dict:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已生成配置文件模板：{CONFIG_PATH}（填入飞书应用配置后即可推送）")
    return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}


def run_async(coro) -> None:
    """统一异步入口：网络/接口错误与 Ctrl+C 打印一行提示而不是 traceback。"""
    try:
        asyncio.run(coro)
    except (httpx.HTTPError, PxqError) as exc:
        print(f"请求票星球失败（检查网络或 showId）：{exc}")
    except FeishuError as exc:
        print(f"飞书应用配置或连接失败：{exc}")
    except KeyboardInterrupt:
        print("\n已停止。")


def resolve_show_id(target: str) -> str | None:
    """支持三种输入：搜索结果序号、showId、含 showId 的链接。"""
    if target.isdigit() and len(target) <= 3 and SEARCH_CACHE_PATH.exists():
        cached = json.loads(SEARCH_CACHE_PATH.read_text(encoding="utf-8"))
        index = int(target)
        if 1 <= index <= len(cached):
            return cached[index - 1]["showId"]
        return None
    return extract_show_id(target)


def format_status(row) -> str:
    if row["can_buy_count"] > 0:
        return f"有票·{format_count(row['can_buy_count'])}"
    return "可售但无票" if row["sale_started"] else "未开售"


def parse_numbers(raw: str, upper: int) -> list[int] | None:
    """'3,5' -> [3, 5]，越界或格式错误时打印提示并返回 None。"""
    try:
        return parse_plan_numbers(raw, upper)
    except ValueError as exc:
        print(exc)
        return None


def print_plans(plans: list[dict], picked_ids: set[str]) -> None:
    for i, p in enumerate(plans, 1):
        mark = "✓" if not picked_ids or p["pid"] in picked_ids else "✗"
        print(f"  {mark} {i:>2}. {p['plan_name']:<12} ¥{p['price']:<8g} {format_status(p)}")


# ---- 子命令 ----

async def cmd_search(args) -> None:
    db = Database(DB_PATH)
    client = PxqClient()
    service = WatchService(db, client)
    try:
        shows = await service.search(args.keyword, page=args.page)
    finally:
        await client.aclose()
        db.close()
    if not shows:
        print("未找到相关演出/赛事。")
        return
    SEARCH_CACHE_PATH.write_text(
        json.dumps(shows, ensure_ascii=False), encoding="utf-8"
    )
    for i, s in enumerate(shows, 1):
        sold = "已售罄" if s.get("soldOut") else s.get("showStatus", "")
        price = s.get("minOriginalPriceVO") or {}
        print(
            f"{i:>2}. {s.get('showName')}\n"
            f"    {s.get('showDate', '')} | {s.get('cityName', '')} {s.get('venueName', '')}"
            f" | {sold} | {price.get('prefix', '¥')}{price.get('yuanNum', '?')}起\n"
            f"    showId={s.get('showId')}"
        )
    print("\n用 `add <序号>` 或 `add <showId>` 加入监控。")


async def cmd_add(args) -> None:
    show_id = resolve_show_id(args.target)
    if not show_id:
        print(f"无法从 '{args.target}' 解析出 showId（先执行 search，或直接给出 24 位 id / 链接）。")
        return
    interval = max(args.interval, MIN_INTERVAL)
    db = Database(DB_PATH)
    client = PxqClient()
    service = WatchService(db, client)
    try:
        show_name, sessions = await service.get_show_sessions(show_id)
        if not sessions:
            print(f"{show_name}：当前没有任何场次（未开票或已失效），未添加。")
            return
        if len(sessions) > 1 and not args.session:
            print(f"{show_name} 有 {len(sessions)} 个场次，请加 --session <序号> 指定监控哪一场：")
            for i, svo in enumerate(sessions, 1):
                print(f"  {i}. {svo.get('sessionName', '')}")
            return
        index = (args.session or 1) - 1
        if not 0 <= index < len(sessions):
            print(f"--session 超出范围（1~{len(sessions)}）。")
            return
        chosen = sessions[index]
        session_id = chosen["bizShowSessionId"]

        plans = await service.fetch_plans(show_id, session_id)
        picked_ids: set[str] = set()
        if args.pick:
            numbers = parse_numbers(args.pick, len(plans))
            if numbers is None:
                return
            picked_ids = {plans[n - 1]["pid"] for n in numbers}

        watch_id, created = service.add_watch(
            show_id, show_name, session_id, interval, picked_ids
        )
        if not created:
            print(f"该场次已在监控中：#{watch_id}，未重复添加。")
            return
        scope = f"已选 {len(picked_ids)} 个票档" if picked_ids else "全部票档"
        print(f"已添加监控 #{watch_id}：{show_name}")
        print(f"场次：{chosen.get('sessionName', '')}（每 {interval}s，{scope}）")

        # 只读预览，不落库、不发卡片——首次提醒由守护进程统一发送，避免重复
        print(f"\n票档编号（改选用 pick 命令，如 pick {watch_id} 3,5）：")
        print_plans(plans, picked_ids)
    finally:
        await client.aclose()
        db.close()


async def cmd_list(_args) -> None:
    db = Database(DB_PATH)
    service = WatchService(db)
    try:
        watches = service.list_watches()
        if not watches:
            print("暂无监控项。")
            return
        for w in watches:
            plans = service.get_plan_states(w["id"])
            picked = parse_picked(w["price_filter"])
            watched = [p for p in plans.values()
                       if not picked or p["seat_plan_id"] in picked]
            available = sum(1 for p in watched if p["can_buy_count"] > 0)
            updated = max((p["updated_at"] for p in plans.values()), default=None)
            age = f"{int(time.time() - updated)}s 前" if updated else "尚未抓取"
            flag = "▶" if w["status"] == "active" else "⏸"
            print(
                f"{flag} #{w['id']} {w['show_name']}\n"
                f"    有票票档 {available}/{len(watched)} | 间隔 {w['interval_sec']}s"
                f" | 更新于 {age}"
            )
            pinned = w["session_id"] or ""
            if pinned:
                sessions = service.get_session_states(w["id"])
                sname = sessions[pinned]["session_name"] if pinned in sessions else "（待首次抓取）"
                print(f"    场次: {sname}")
            for i, row in enumerate(plans.values(), 1):
                mark = "✓" if not picked or row["seat_plan_id"] in picked else "✗"
                print(f"      {mark} {i:>2}. {row['plan_name']:<12}"
                      f" ¥{row['price']:<8g} {format_status(row)}")
    finally:
        db.close()


async def cmd_pick(args) -> None:
    db = Database(DB_PATH)
    client = PxqClient()
    service = WatchService(db, client)
    try:
        watch = service.get_watch(args.id)
        if not watch:
            print(f"监控 #{args.id} 不存在。")
            return
        if args.numbers.strip().lower() in ("all", "全部"):
            service.set_picked_ids(args.id, set())
            print(f"监控 #{args.id} 已恢复监控全部票档。")
            return
        plans = await service.fetch_plans(watch["show_id"], watch["session_id"])
        numbers = parse_numbers(args.numbers, len(plans))
        if numbers is None:
            return
        picked_ids = {plans[n - 1]["pid"] for n in numbers}
        service.set_picked_ids(args.id, picked_ids)
        print(f"监控 #{args.id} 现在只监控 {len(picked_ids)} 个票档：")
        print_plans(plans, picked_ids)
    finally:
        await client.aclose()
        db.close()


async def cmd_set_status(args, status: str) -> None:
    db = Database(DB_PATH)
    service = WatchService(db)
    try:
        if not service.get_watch(args.id):
            print(f"监控 #{args.id} 不存在。")
            return
        if status == "removed":
            service.remove_watch(args.id)
            print(f"已删除监控 #{args.id}。")
        else:
            service.set_status(args.id, status)
            print(f"监控 #{args.id} 已{'暂停' if status == 'paused' else '恢复'}。")
    finally:
        db.close()


async def cmd_interval(args) -> None:
    db = Database(DB_PATH)
    service = WatchService(db)
    try:
        if not service.get_watch(args.id):
            print(f"监控 #{args.id} 不存在。")
            return
        interval = service.set_interval(args.id, args.seconds)
        print(f"监控 #{args.id} 轮询间隔已改为 {interval}s。")
    finally:
        db.close()


async def cmd_run(args, config: dict) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db = Database(DB_PATH)
    client = PxqClient()
    feishu: FeishuGateway | None = None
    try:
        command_queue: asyncio.Queue[IncomingCommand] = asyncio.Queue(maxsize=100)
        feishu = FeishuGateway(config, command_queue)
        service = WatchService(db, client)
        engine = MonitorEngine(db, client, feishu)
        worker = CommandWorker(command_queue, service, feishu)
        if args.once:
            await engine.tick()
        else:
            await asyncio.gather(
                engine.run_forever(),
                feishu.run_forever(),
                worker.run_forever(),
            )
    finally:
        if feishu is not None:
            await feishu.aclose()
        await client.aclose()
        db.close()


async def cmd_test_notify(config: dict) -> None:
    feishu = FeishuGateway(config)
    try:
        ok = await feishu.send_card(
            "✅ 测试消息", "**票星球监控**通知链路正常。", template="green"
        )
    finally:
        await feishu.aclose()
    print("发送成功" if ok else "发送失败（检查 App ID、App Secret 和 chat_id 配置）")


def main() -> None:
    parser = argparse.ArgumentParser(prog="pxq-monitor", description="票星球余票监控")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="按关键词搜索比赛")
    p.add_argument("keyword")
    p.add_argument("--page", type=int, default=1)

    p = sub.add_parser("add", help="添加监控（序号 / showId / 链接），锁定到具体场次")
    p.add_argument("target")
    p.add_argument("--interval", type=int, default=60, help="轮询间隔秒数（默认 60）")
    p.add_argument("--pick", default="", help="只监控指定编号的票档，如 --pick 3,5（编号见 add 预览）")
    p.add_argument("--session", type=int, default=0, help="多场次演出指定第几场（add 不带此参数时会列出场次）")

    sub.add_parser("list", help="查看所有监控项及最新状态")

    p = sub.add_parser("pick", help="改选监控的票档编号，如 pick 1 3,5；pick 1 all 恢复全部")
    p.add_argument("id", type=int)
    p.add_argument("numbers")

    for name, help_text in (("remove", "删除监控"), ("pause", "暂停监控"), ("resume", "恢复监控")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("id", type=int)

    p = sub.add_parser("interval", help="修改轮询间隔")
    p.add_argument("id", type=int)
    p.add_argument("seconds", type=int)

    p = sub.add_parser("run", help="启动监控和飞书双向指令服务")
    p.add_argument("--once", action="store_true", help="所有监控项各抓取一轮后退出")

    sub.add_parser("test-notify", help="发送一条测试消息验证飞书配置")

    args = parser.parse_args()
    config = load_config()

    if args.command == "search":
        run_async(cmd_search(args))
    elif args.command == "add":
        run_async(cmd_add(args))
    elif args.command == "list":
        run_async(cmd_list(args))
    elif args.command == "pick":
        run_async(cmd_pick(args))
    elif args.command == "remove":
        run_async(cmd_set_status(args, "removed"))
    elif args.command == "pause":
        run_async(cmd_set_status(args, "paused"))
    elif args.command == "resume":
        run_async(cmd_set_status(args, "active"))
    elif args.command == "interval":
        run_async(cmd_interval(args))
    elif args.command == "run":
        run_async(cmd_run(args, config))
    elif args.command == "test-notify":
        run_async(cmd_test_notify(config))
