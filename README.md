# pxq-monitor — 票星球球赛余票监控

轮询票星球公开接口（无需登录），余票状态变化时通过飞书企业自建应用机器人推送卡片；同时使用飞书长连接接收搜索、监控、暂停等文字指令，无需公网 IP、域名或回调服务。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. 搜索比赛
python -m pxq_monitor search 大连英博

# 2. 添加监控（支持 搜索结果序号 / showId / 含 showId 的链接）
python -m pxq_monitor add 1 --interval 30          # 监控全部票档，输出会列出票档编号
python -m pxq_monitor add 1 --pick 3,5             # 只盯编号 3、5 的票档
python -m pxq_monitor pick 1 10,11                 # 对已有监控 #1 改选票档
python -m pxq_monitor pick 1 all                   # 恢复监控全部票档

# 3. 启动监控 + 飞书双向指令（正式部署用 systemd，见下文）
python -m pxq_monitor run
```

## 配置飞书企业自建应用

1. 在[飞书开放平台](https://open.feishu.cn/)创建企业自建应用并开启机器人能力；
2. 开通 `im:message.p2p_msg:readonly`、`im:message.group_at_msg:readonly` 和 `im:message:send_as_bot`；
3. 事件订阅选择**长连接**，订阅 `im.message.receive_v1`，然后创建并发布版本；
4. 把应用机器人加入通知群，并把应用可用范围限制为需要使用的人；
5. 首次运行 `run` 或 `test-notify` 后会生成 `~/.pxq-monitor/config.json`，填入：

```json
{
  "feishu_app_id": "cli_xxxxxxxxx",
  "feishu_app_secret": "应用密钥",
  "feishu_admin_open_ids": ["ou_xxxxxxxxx"],
  "feishu_default_chat_id": "oc_xxxxxxxxx"
}
```

首次不知道 `open_id` 和 `chat_id` 时，可以先只填写 App ID/Secret 并启动 `run`，给机器人发一条消息，再从日志中的 `收到飞书消息` 复制两项 ID，补入配置并重启。程序会在 Linux 上自动把配置文件权限收紧为 `0600`。

验证主动推送：`python -m pxq_monitor test-notify`，收到测试卡片即通。App Secret 只放在本地配置文件中，不要提交到 Git。

## 飞书文字指令

单聊只接受 `feishu_admin_open_ids` 中的用户；群聊需要先 @ 机器人，普通成员可以搜索和查看列表，修改类指令仍只允许管理员执行。

| 指令 | 说明 |
|---|---|
| `帮助` | 显示指令列表 |
| `搜索 大连英博` | 搜索赛事；搜索结果只保存在当前进程内存中 |
| `监控 1 [场次 N] [票档 3,5] [间隔 秒]` | 按最近一次飞书搜索结果添加监控 |
| `列表` | 查看监控项和票档状态 |
| `暂停 1` / `恢复 1` / `删除 1` | 管理监控项 |
| `票档 1 3,5` / `票档 1 全部` | 修改监控票档 |
| `间隔 1 30` | 修改轮询间隔，最低 15 秒 |

搜索结果按“群聊/单聊 + 发送者”隔离并保留 15 分钟。服务重启后结果会清空，重新发送一次 `搜索` 即可。收到的指令先进入单进程内存队列并顺序执行；队列与消息去重记录不会跨重启保留。

## 数据结构：演出 → 场次 → 票档

```
演出（show）         「2026赛季大连英博足球俱乐部主场赛事」＝add 时给出的对象
 └─ 场次（session）   「07-18 周六 19:35 大连英博海发 VS 山东泰山」＝监控实际锁定的对象
     └─ 票档（seat plan）「80元」「400元」……余票数量挂在这一层
```

一个监控项锁定**一个场次**。场次结束/取消后监控自动暂停并通知；想盯下一场再 `add` 一次即可（球队的赛季演出会随赛程在同一演出下追加新场次）。

## 提醒规则

**会提醒**（红色 = 需要马上行动）：

| 层级 | 变化 | 卡片标题 | 颜色 |
|---|---|---|---|
| 票档 | 余票 0 → 有票（回流） | 🎫 有回流票 | 红 |
| 票档 | 未开售 → 开售 | 🎫 票档开售 | 红 |
| 票档 | 刚加监控时已有票（仅一次） | 🎫 当前在售 | 绿 |
| 场次 | 锁定的场次从票星球消失（已结束/取消/下架，连续 3 轮确认） | ⚠️ 场次已结束或下架，**监控自动暂停** | 橙 |
| — | 连续 5 次抓取失败 | ⚠️ 监控异常 | 橙 |

**不提醒**：变为售罄、有票期间的数量波动、场次状态的任何变化（状态翻成"在售"不代表真有票，实测存在空翻；状态变化只记运行日志，真有票时回流卡自然会来）。安静 = 没有值得行动的变化。

**降噪机制**：

- 回流提醒按"归零后再有票"触发：有票期间数量怎么波动都不重复提醒；售罄后再次回流会**立即**再提醒，没有冷却等待，一次机会都不漏；
- 同一轮抓取的多条提醒合并为一张「🎫 余票提醒」卡片（避免刷屏和飞书限频）；
- 飞书发送失败时不推进本轮状态，并在当前进程的内存队列中持续重试；不建立持久化提醒表，因此服务重启会丢失尚未送达的提醒；
- 余票数 ≥9999 显示为"充足"（票星球以 999999 表示不限量）；
- 最多并发抓取 3 个监控项，同一轮内相同演出共享场次请求；
- 抓取失败按指数退避放慢轮询（最长 16 倍间隔），恢复后自动复原。

## 全部命令

| 命令 | 说明 |
|---|---|
| `search <关键词> [--page N]` | 搜索比赛，结果带序号 |
| `add <序号\|showId\|链接> [--interval 秒] [--pick 3,5] [--session 序号]` | 添加监控并锁定到具体场次（单场次自动锁定，多场次不带 `--session` 时会列出场次供选择）；打印带编号的票档预览，`--pick` 按编号只监控部分票档 |
| `pick <id> <编号,编号>` / `pick <id> all` | 改选已有监控的票档（编号见 `add` 预览或 `list`，✓=监控中 ✗=已排除）；内部按票档 ID 存储，票档增减不会错位 |
| `list` | 查看所有监控项及每个票档的最新状态 |
| `pause <id>` / `resume <id>` / `remove <id>` | 暂停 / 恢复 / 删除 |
| `interval <id> <秒>` | 修改轮询间隔（最低 15s） |
| `run [--once]` | 默认启动监控、飞书长连接和指令 worker；`--once` 只抓一轮就退出（配 cron 用，**别和守护进程同时跑**） |
| `test-notify` | 通过应用机器人发送测试卡片 |

可用 `python -m pxq_monitor --version` 查看版本。

通过 CLI 或飞书增删改监控项都**不需要重启守护进程**，它每个调度周期自动重读数据库。

## 目录说明

| 位置 | 内容 |
|---|---|
| 项目目录（如 `~/pxq-monitor`） | 代码，更新时整个覆盖 |
| `~/.pxq-monitor/` | 你的数据：`config.json`、`monitor.db` 和 CLI 搜索缓存 `last_search.json`，更新代码不影响 |

数据目录可用环境变量 `PXQ_MONITOR_DIR` 覆盖（注意 CLI 和守护进程要指向同一个）。

## Linux 部署（systemd）

仓库内的 `deploy/pxq-monitor.service` 是唯一的服务模板，默认代码和数据都位于 `/opt/pxq-monitor`，使用独立的 `pxq-monitor` 系统用户：

```bash
sudo useradd --system --home /opt/pxq-monitor --shell /usr/sbin/nologin pxq-monitor
sudo chown -R pxq-monitor:pxq-monitor /opt/pxq-monitor
sudo cp deploy/pxq-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pxq-monitor
sudo journalctl -u pxq-monitor -f
```

路径或用户不同时，先修改服务文件中的 `User`、`Group`、`WorkingDirectory`、`Environment` 和 `ExecStart`。更新代码后执行 `sudo systemctl restart pxq-monitor`。

## 涉及的票星球接口（全部公开只读）

| 接口 | 用途 |
|---|---|
| `GET /home/pub/v3/show_list/search` | 关键词搜索 |
| `GET /show/pub/v3/show/{showId}/sessions_static_data` | 比赛名、场次名（守护进程仅在场次名未知时抓取） |
| `GET /show/pub/v3/show/{showId}/sessions_dynamic_data` | 场次在售状态 |
| `GET /show/.../show_session/{sessionId}/seat_plans_static_data` | 票档名/价格（守护进程仅在发现新票档时抓取） |
| `GET /show/.../show_session/{sessionId}/seat_plans_dynamic_data` | **每票档余票数**（核心轮询） |

稳态下每个监控项每轮请求一次票档动态接口；同一演出的场次动态接口在本轮只请求一次。静态信息按需抓取并落库缓存。CLI 的 `add` 和指定票档的 `pick` 会即时读取静态与动态票档数据。

> 仅作个人余票提醒用途：只读接口、低频轮询，不涉及自动下单。
