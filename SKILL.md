---
name: boss-pipeline
description: "Boss 直聘全流程自动化：爬取 JD → Agent 判定优先级 → 定时打招呼，含实时监控网页。"
metadata:
  {
    "openclaw":
      {
        "emoji": "🔁",
        "requires": { "bins": ["python3", "curl"] },
        "subskills": ["boss-resume-crawler", "job-match-priority", "boss-auto-greet"],
      },
  }
read_when:
  - 用户要求执行 Boss 直聘全流程自动化
  - 用户提到"一键爬取"、"全流程"、"一体化"、"boss-pipeline"
  - 用户给出 Boss 直聘列表页 URL 要求爬取+判断+打招呼
allowed-tools: Bash,Read,Write,exec,message,cron,sessions_send
---

# Boss 直聘全流程自动化 (boss-pipeline)

> 编排者：龙虾CEO（main） — 执行者：boss-resume-crawler / zhizhi / boss-auto-greet

---

## 一、触发与输入

**触发词**：boss 直聘一体化 / 一键爬取打招呼 / boss-pipeline + 列表页 URL

**必需输入**：Boss 直聘列表页 URL（含 `zhipin.com/web/geek/jobs`）

**运行时询问**（不答用默认）：
| 问题 | 默认值 | 说明 |
|------|--------|------|
| 打招呼时段 | 次日 10:00 | 格式 `HH:MM` 或 `明天 HH:MM` |
| 打招呼语 | 内置默认 | 可自定义 |
| 单批上限 | 全部（即 medium+high 总数） | 限制单次数量，防风控 |

默认话术：
```
您好，看到贵司在招的岗位很感兴趣，我的背景和经验比较匹配，希望能有机会进一步沟通~
```

---

## 二、流程概览

```
ii 给出列表页 URL
  │
  ▼
[阶段0 环境]  main → 检查 CloakBrowser/CDP/登录/依赖
  │  输出：环境就绪 or 阻塞项
  ▼
[阶段1 爬取]  main → boss-resume-crawler (boss_extract_cdp.py)
  │  产出：01_raw/jobs_data_*.csv + progress.json + jobs_cache.json
  │  汇报：共 N 条 / 成功率 / 字段完整率
  ▼
[阶段2 判断]  main → sessions_send → zhizhi (job-match-priority)
  │  产出：02_result/*_判定结果.csv
  │  汇报：high / medium / low 分布
  ▼
[阶段3 打招呼]  main → cron at 定时 → boss-auto-greet
  │  产出：03_greet/greet_log.csv
  │  汇报：已打 K 条 / 失败 F 条
  ▼
ii 跟进沟通
```

---

## 三、阶段0：环境检查

**执行者**：main（龙虾CEO）

**检查清单**（全部通过才进入阶段1）：

| # | 检查项 | 验证方法 | 失败处理 |
|---|--------|---------|---------|
| 1 | Python3 + websocket-client | `python3 -c "import websocket"` | 提示安装 |
| 2 | CloakBrowser 已启动（有头） | `curl -s http://localhost:9222/json/version` | 引导启动脚本 |
| 3 | Boss 直聘已登录 | 导航到列表页 URL，检查页面标题不含"登录" | 暂停等扫码，登录后重导航 |
| 4 | 输出目录可写 | `mkdir -p` 测试 | 提示权限问题 |

**目录创建**（pipeline 启动即建）：
```
workspace/boss直聘一体化/YYYY-MM-DD-<搜索词>/
  ├── pipeline_state.json
  ├── 01_raw/
  ├── 02_result/
  ├── 03_greet/
  └── pipeline.log
```

**状态机**：`update_state(work_dir, stage="crawl", status="running")`

---

## 四、阶段1：爬取

**执行者**：main → boss-resume-crawler

**步骤**：
1. 启动 Dashboard 服务器（后台）：
   ```python
   python3 ~/.agents/skills/boss-pipeline/scripts/serve_dashboard.py \
     --output-dir 01_raw/ [--port 8765]
   ```
2. 初始化进度追踪器：`ProgressTracker(output_dir).start(list_total=0)`
3. 启动爬取（后台 exec，长任务）：
   ```bash
   python3 ~/.agents/skills/boss-resume-crawler/scripts/boss_extract_cdp.py \
     --output 01_raw/ \
     --max-scroll 50
   ```
4. 汇报 Dashboard URL 给 ii：`🌐 监控页面: http://localhost:8765`
5. 轮询 `pipeline_state.json` 等待爬取完成（每 30 秒检查一次，最长 2 小时）

**产出**：`01_raw/jobs_data_*.csv`（含 progress.json + jobs_cache.json）

**汇报内容**：
```
【爬取完成】
共爬取: N 条
成功: S 条
失败: F 条
字段完整率: X%
平均耗时: X 秒/条
输出文件: 01_raw/jobs_data_*.csv
监控页面: http://localhost:8765
```

**状态机**：`update_state(work_dir, stage="crawl", status="done", raw_csv=output_file)`

---

## 五、阶段2：判断

**执行者**：main → sessions_send → zhizhi

**前置条件**：`01_raw/` 下有爬取完成的 CSV（非空）

**步骤**：
1. 在 `02_result/` 目录为空时（首次）进入此阶段
2. 读取 `pipeline_state.json` 获取 `raw_csv` 路径
3. 通过 `sessions_send` 发送任务给 zhizhi（协议见 `references/zhizhi-handoff.md`）：
   ```
   raw_csv: <01_raw/ 下的 CSV 绝对路径>
   result_dir: <02_result/ 绝对路径>
   candidate_config: last | new | <绝对路径>
   ```
4. 等待 zhizhi 回执（最长 30 分钟）：
   - 收到回执 → 解析（调用 `parse_handoff_reply.py`）
   - 回执表示需要画像 → 通知 ii 在飞书与 zhizhi 完成引导
   - 超时无响应 → 重试一次，仍失败则标记 error
5. 更新 `pipeline_state.json`

**candidate_config 取值规则**：
| 值 | 含义 | 行为 |
|----|------|------|
| 绝对路径 | 已有固定画像文件 | 直接读取，跳过 Step 0 |
| `new` | 首次引导 | zhizhi 走 job-match-priority Step 0 |
| `last` | 沿用上次 | 默认值，搜索上次画像；找不到等同 `new` |

**产出**：`02_result/*_判定结果.csv`

**汇报内容**：
```
【匹配完成】
总判断: N 条
高优先级: H 条
中优先级: M 条
低优先级: L 条
结果文件: 02_result/*_判定结果.csv
```

**状态机**：`update_state(work_dir, stage="judge", status="done", result_csv=result_file, counts={total, high, medium, low})`

---

## 六、阶段3：打招呼

**执行者**：main → cron at 定时 → boss-auto-greet

**前置条件**：`02_result/` 下有判定结果 CSV，且 high/medium 合计 > 0

**步骤**：
1. 询问 ii 三项（不答用默认）：时段 / 话术 / 单批上限
2. 调用 `schedule_greet.py` 构建 cron job 参数
3. 通过 `cron` 工具创建一次性定时任务
4. 汇报定时信息给 ii

**cron 任务结构**：
```javascript
cron(action="add", job={
    "name": "boss-greet-{搜索词}-{日期}",
    "schedule": { "kind": "at", "at": "<ISO-8601 时间戳>" },
    "payload": {
        "kind": "agentTurn",
        "message": "【自动打招呼任务】\n\n请执行 boss-auto-greet 脚本：\n\npython3 ... \\\n  --csv \"<02_result CSV>\" \\\n  --message \"<话术>\" \\\n  --max-greet <上限>\n\n执行完成后汇报结果。"
    },
    "sessionTarget": "isolated",
    "delivery": { "mode": "announce" }
})
```

**产出**：`03_greet/greet_log.csv`（由 boss-auto-greet 脚本自动写入）

**汇报内容**：
```
【定时打招呼已设置】
执行时间: 2026-08-06 10:00
打招呼语: 您好，...
单批上限: 5
高优先级: 3 条
中优先级: 7 条
```

**状态机**：`update_state(work_dir, stage="greet", status="done", greet={scheduled_at: "..."})` → `update_state(work_dir, stage="done")`

---

## 七、状态机参考

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `stage` | `crawl\|judge\|greet\|done` | 当前阶段 |
| `status` | `running\|waiting\|done\|error` | 当前状态 |
| `raw_csv` | str | 01_raw/ 下的 CSV 绝对路径 |
| `result_csv` | str | 02_result/ 下的判定结果 CSV 绝对路径 |
| `counts` | `{total,high,medium,low}` | 判断结果计数 |
| `greet` | `{scheduled_at,done,failed}` | 打招呼状态 |
| `search_query` | str | 搜索词，用于目录命名溯源 |
| `list_url` | str | 原始列表页 URL |
| `created_at` / `updated_at` | ISO 时间戳 | 创建/更新时间 |

### 阶段流转

```
crawl:running  →  crawl:done  →  judge:running  →  judge:done  →  greet:done  →  done
       ↓ error                     ↓ error                     ↓ error
    error                        error                        error
```

### 续跑逻辑

中断后重新启动 → `load_state(work_dir)` → `next_action(state)` 返回下一步应执行的动作：

| 返回值 | 含义 | 执行动作 |
|--------|------|---------|
| `crawl` | 爬取未完成或未开始 | 跳转到阶段1 |
| `judge` | 爬取完成但未判断 | 跳转到阶段2 |
| `greet` | 判断完成但未打招呼 | 跳转到阶段3 |
| `done` | 全流程完成 | 汇报已完成 |
| `error` | 出错 | 汇报错误信息 |

---

## 八、子 skill 引用

| 子 skill | 路径 | 用途 |
|----------|------|------|
| `boss-resume-crawler` | `~/.agents/skills/boss-resume-crawler/` | 爬取 JD |
| `job-match-priority` | `~/.agents/skills/job-match-priority/` | 匹配判定（zhizhi 执行） |
| `boss-auto-greet` | `~/.agents/skills/boss-auto-greet/` | 自动打招呼 |

### 本 skill 辅助文件

| 文件 | 用途 |
|------|------|
| `scripts/pipeline_state.py` | 状态机读写 |
| `scripts/parse_handoff_reply.py` | zhizhi 回执解析 |
| `scripts/schedule_greet.py` | cron 任务构建 |
| `scripts/progress_tracker.py` | 爬取进度追踪 |
| `scripts/serve_dashboard.py` | Dashboard 本地服务器 |
| `dashboard/index.html` | 实时监控页面 |
| `references/zhizhi-handoff.md` | 交接协议 |
| `references/cron-greet-template.md` | cron 模板 |

---

## 九、Dashboard 集成

爬取阶段自动启动本地监控页面。

| 组件 | 端口 | 访问 |
|------|------|------|
| HTTP 服务器 | 8765 | `http://localhost:8765` |
| 默认端口冲突 | 自动尝试 8765-8775 | — |

启动方式（由 main 在阶段1 自动执行）：
```python
from serve_dashboard import start_dashboard_server
url = start_dashboard_server("01_raw/", port=8765)
```

---

## 十、完整交互示例

```
ii → main:
boss-pipeline 帮我跑一下 https://www.zhipin.com/web/geek/jobs?query=搜索词示例&city=101020100

main → ii:
【环境检查】CloakBrowser 已启动 ✅ | 已登录 ✅ | 依赖就绪 ✅
【阶段1 爬取】开始爬取...
🌐 监控页面: http://localhost:8765

（爬取完成后）

【爬取完成】共爬取 30 条 / 成功 28 条 / 失败 2 条 / 平均耗时 25 秒/条
【阶段2 判断】交给 zhizhi 匹配中...

（判断完成后）

【匹配完成】高 5 条 / 中 12 条 / 低 13 条
【阶段3 打招呼】默认次日 10:00 执行（共 17 条 high+medium），确认吗？

ii → main:
确认

main → ii:
【定时打招呼已设置】2026-08-06 10:00 → 17 条
```

---

## 十一、异常处理速查

| 异常场景 | 处理方式 |
|----------|---------|
| CloakBrowser 未启动 | 引导启动：`open ~/.cache/cloakbrowser/Chromium.app --args --remote-debugging-port=9222 --remote-allow-origins=*` |
| Boss 未登录 | 暂停等扫码，登录后重导航到目标 URL |
| 爬取超时（>2h） | 标记 error，通知 ii 人工介入 |
| zhizhi 无画像 | 通知 ii 在飞书和 zhizhi 完成引导 |
| zhizhi 超时 | 重试一次，仍失败标记 error |
| 打招呼 cron 创建失败 | 记录错误，建议 ii 手动执行 |
| 进程中断 | 重启后 `load_state` → `next_action` 续跑 |