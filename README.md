# boss-pipeline

把 Boss 直聘列表页变成一份排好优先级的岗位清单：爬取 JD → 本地判定 high/medium/low → 按需打招呼（默认先 dry-run）。

判定用本机 Python 引擎完成，不调用大模型，不消耗 token。岗位数据、画像和判定结果都只存在本机。

---

## 三步主路径

| 步骤 | 做什么 | 入口 |
|------|--------|------|
| 1 | 启动并完成首次配置 | 双击 `launcher/Boss直聘监控.command`，首启自动进向导 |
| 2 | 粘贴 Boss 列表页 URL，开始爬取 | Dashboard 顶部「🚀 启动浏览器」→「🕷️ 新建爬取任务」 |
| 3 | 全量判定 → 导出结果 → dry-run 打招呼 | 详情区「⚡ 进行全量判定」；打招呼先加 `--dry-run` |

第一次用，按 [QUICKSTART.md](QUICKSTART.md) 走，约 10 分钟。遇到报错先看 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

---

## 它能做什么

- **爬取**：从列表页滚动加载，逐条打开详情页，落成 CSV；支持断点续爬
- **本地判定**：按你的画像和能力词表给出 `high / medium / low`、匹配度和理由，零 token
- **打招呼**：读取判定结果，对 high/medium 岗位发送打招呼语；支持 dry-run、首条测试、断点续打
- **看板**：本地网页实时显示爬取进度、薪资/城市分布和历史任务
- **长期监控**：定时重跑同一搜索，发现新岗位后提醒（可选）

---

## 目录

```
boss-pipeline/
├── SKILL.md                 # Agent 执行手册（含可选的历史交接附录）
├── QUICKSTART.md            # 新用户 10 分钟上手
├── TROUBLESHOOTING.md       # 六类常见故障
├── dashboard/
│   ├── index.html           # 主看板
│   └── wizard.html          # 首次配置向导
├── launcher/
│   ├── Boss直聘监控.command  # 双击启动（会先跑环境自检）
│   └── run-dashboard.sh     # 常驻启动（launchd 用）
├── scripts/                 # 路径解析、向导、自检、判定桥接、看板服务
└── references/              # cron 模板、历史交接协议（可选）
```

四个 skill（`boss-pipeline`、`boss-resume-crawler`、`job-match-priority`、`boss-auto-greet`）建议放在同一父目录，脚本会自动找到同级仓库。

---

## 数据放在哪

| 内容 | 位置 |
|------|------|
| 画像与偏好 | `~/.boss-pipeline/profiles/<id>/`（权限 600） |
| 爬取产物 | `~/.boss-pipeline/work/YYYY-MM-DD-<搜索词>/`（可用 `BOSS_PIPELINE_WORK_ROOT` 覆盖） |
| 日志 | `~/.boss-pipeline/logs/`（常驻启动时） |

skill 目录只放代码和模板；升级或重装不会覆盖你的画像。需要换目录时设 `BOSS_PIPELINE_HOME`。

---

## 想调判定

判定方法、三层配置、阈值和闸门说明见 [job-match-priority/FRAMEWORK.md](../job-match-priority/FRAMEWORK.md)。
新用户不用读：向导会把必填项问完，之后想微调再看。

---

## 依赖

- Python 3.10+
- `pyyaml`、`requests`、`websocket-client`
- CloakBrowser（反检测 Chromium，用于登录和爬取）

自检会逐项告诉你缺什么、怎么装：

```bash
python3 scripts/env_check.py
```

---

## License

MIT
