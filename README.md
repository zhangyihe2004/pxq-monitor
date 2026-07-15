# pxq-monitor — 票星球球赛余票监控

轮询票星球公开接口（无需登录），余票状态变化时通过飞书群机器人推送卡片提醒。

## 快速开始

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

# 3. 启动监控（正式部署用 systemd，见下文）
python -m pxq_monitor run
```

## 配置飞书推送

1. 飞书建一个群（可以只有自己）→ 群设置 → 群机器人 → 添加**自定义机器人**；
2. 安全设置勾选**签名校验**，复制 webhook 地址和密钥；
3. 首次运行任意命令后会生成 `~/.pxq-monitor/config.json`，填入：

```json
{
  "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx",
  "feishu_secret": "签名密钥"
}
```

4. 验证：`python -m pxq_monitor test-notify`，群里收到测试卡片即通。

未配置 webhook 时提醒降级打印到控制台/日志，监控本身照常运行。

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
- 余票数 ≥9999 显示为"充足"（票星球以 999999 表示不限量）；
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
| `run [--once]` | 启动守护进程；`--once` 抓一轮就退出（配 cron 用，**别和守护进程同时跑**） |
| `test-notify` | 发送测试卡片验证飞书配置 |

增删改监控项**不需要重启守护进程**，它每个调度周期自动重读数据库。

## 目录说明

| 位置 | 内容 |
|---|---|
| 项目目录（如 `~/pxq-monitor`） | 代码，更新时整个覆盖 |
| `~/.pxq-monitor/` | 你的数据：`config.json` + `monitor.db`，更新代码不影响 |

数据目录可用环境变量 `PXQ_MONITOR_DIR` 覆盖（注意 CLI 和守护进程要指向同一个）。

## Linux 部署（systemd）

以代码在 `/home/ubuntu/pxq-monitor`、用户 `ubuntu` 为例：

```bash
sudo tee /etc/systemd/system/pxq-monitor.service > /dev/null <<'EOF'
[Unit]
Description=PXQ ticket availability monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/pxq-monitor
Environment=PXQ_MONITOR_DIR=/home/ubuntu/.pxq-monitor
ExecStart=/home/ubuntu/pxq-monitor/.venv/bin/python -m pxq_monitor run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pxq-monitor
journalctl -u pxq-monitor -f       # 看实时日志
```

更新代码后：`sudo systemctl restart pxq-monitor`。

## 涉及的票星球接口（全部公开只读）

| 接口 | 用途 |
|---|---|
| `GET /home/pub/v3/show_list/search` | 关键词搜索 |
| `GET /show/pub/v3/show/{showId}/sessions_static_data` | 比赛名、场次名（仅新场次抓一次） |
| `GET /show/pub/v3/show/{showId}/sessions_dynamic_data` | 场次在售状态 |
| `GET /show/.../show_session/{sessionId}/seat_plans_static_data` | 票档名/价格（仅新票档抓一次） |
| `GET /show/.../show_session/{sessionId}/seat_plans_dynamic_data` | **每票档余票数**（核心轮询） |

稳态下每个监控项每轮 1 + 场次数 个请求，静态信息落库缓存。

## Roadmap

- [x] M1/M2：监控引擎 + 飞书 webhook 推送 + SQLite 多监控项 + systemd
- [ ] M3：飞书自建应用长连接，双向指令（在飞书里发 `搜索/监控/列表/暂停`）
- [ ] M4：交互式卡片按钮、每日摘要

> 仅作个人余票提醒用途：只读接口、低频轮询，不涉及自动下单。
