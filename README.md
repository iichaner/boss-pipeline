# boss-pipeline

Boss 直聘全流程自动化：爬取 JD → Agent 判定优先级 → 定时打招呼，含实时监控面板。

## 功能

- 自动爬取 Boss 直聘列表页职位
- 实时监控爬取进度、成功率、薪资/地区分布
- 支持断点续爬、跳过滚动
- 对接 Agent 做职位匹配判定
- 定时自动打招呼（高/中优先级）
- 本地 Dashboard 可视化

## 目录结构

```
boss-pipeline/
├── SKILL.md               # 编排流程说明
├── README.md
├── dashboard/
│   └── index.html         # 监控面板
├── launcher/
│   └── Boss直聘监控.command
├── references/
│   ├── cron-greet-template.md
│   ├── judgment-prompt-template.md
│   └── zhizhi-handoff.md
├── scripts/
│   ├── serve_dashboard.py
│   ├── progress_tracker.py
│   ├── pipeline_state.py
│   ├── schedule_greet.py
│   └── parse_handoff_reply.py
```

## 快速开始

```sh
# 1. 进入技能目录
cd ~/.agents/skills/boss-pipeline

# 2. 启动 Dashboard
python3 scripts/serve_dashboard.py --output-dir <workspace>/boss直聘一体化/YYYY-MM-DD-<搜索词>/01_raw --port 8765

# 3. 打开浏览器访问 http://localhost:8765
```

或双击 `launcher/Boss直聘监控.command` 启动。

## 依赖

- Python 3
- websocket-client
- CloakBrowser（CDP 9222）

## 流程

1. 准备环境并登录 Boss 直聘
2. 在 Dashboard 中粘贴列表页 URL 开始爬取
3. 查看实时进度和统计
4. 使用 Agent 判定优先级（或手动导入判定结果）
5. 确认后定时执行打招呼

## 配置说明

- `serve_dashboard.py` 会自动检测 CloakBrowser 并启动
- 打招呼语可从 `boss-auto-greet/greetings` 目录选择
- 默认输出目录：`<workspace>/boss直聘一体化/`

## License

MIT
