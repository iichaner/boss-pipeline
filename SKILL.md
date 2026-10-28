---
name: boss-pipeline
description: "Boss 直聘全流程自动化：爬取 JD → 本地判定优先级（零 token）→ 定时打招呼，含实时监控网页。"
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

> 默认判定引擎是本机 Python 引擎（`job-match-priority`），零 token。
> Agent 协作是**可选**路径，见文末附录；主路径不需要任何外部 agent。

---

## 一、触发与输入

**触发词**：boss 直聘一体化 / 一键爬取打招呼 / boss-pipeline + 列表页 URL

**必需输入**：Boss 直聘列表页 URL（含 `zhipin.com/web/geek/jobs`）

**运行时询问**（不答用默认）：
| 问题 | 默认值 | 说明 |
|------|--------|------|
| 打招呼时段 | 次日 10:00 | 格式 `HH:MM` 或 `明天 HH:MM` |
| 打招呼语 | 内置通用模板 | 可用 `--message` 覆盖 |
| 单批上限 | 全部（即 medium+high 总数） | 限制单次数量，防风控 |

默认话术：

```
您好，我对贵司在招的岗位很感兴趣，想进一步了解岗位要求。方便的话希望能和您沟通，谢谢！
```

---

## 二、主路径

```
用户给出列表页 URL
  │
  ▼
[阶段0 环境]  env_check.py 六项自检
  │  输出：环境就绪 or 阻塞项（带修复命令）
  ▼
[阶段1 爬取]  Dashboard / boss_extract_cdp.py
  │  产出：01_raw/jobs_data_*.csv + progress.json + jobs_cache.json
  │  汇报：共 N 条 / 成功率 / 字段完整率
  ▼
[阶段2 判定]  本地引擎 run_match.py（零 token）
  │  产出：02_result/*_判定结果.csv
  │  汇报：high / medium / low 分布
  ▼
[阶段3 打招呼]  dry-run → 首条测试 → 批量 / 定时
  │  产出：03_greet/greet_log.csv
  │  汇报：已打 K 条 / 失败 F 条
  ▼
用户跟进沟通
```

默认数据根：`~/.boss-pipeline/`。产物根优先级：`BOSS_PIPELINE_WORK_ROOT` > `config.json.work_root` > `~/.boss-pipeline/work`。

---

## 三、阶段0：环境检查

```bash
python3 scripts/env_check.py
```

六项：Python 与依赖、CloakBrowser、CDP 9222、Boss 登录、端口、画像配置。
每项失败都会给一条 `↳ 修复` 命令。带 `❌` 的阻塞项（目前只有 Python 依赖）必须先处理。

只想看配置是否就绪：

```bash
python3 scripts/onboard.py --status
```

- `needs_onboarding=true` → 引导用户跑 `python3 scripts/onboard.py`，或双击启动器进 `/wizard.html`
- `needs_onboarding=false` → 直接进入阶段1

---

## 四、阶段1：爬取

**推荐**：双击 `launcher/Boss直聘监控.command`，在 Dashboard 里点「🕷️ 新建爬取任务」并粘贴 URL。
启动器会先跑 `env_check.py`；没有画像时自动把浏览器指到向导页。

**命令行等价**：

```bash
python3 ../boss-resume-crawler/scripts/boss_extract_cdp.py \
  --url "<列表页URL>" \
  --output ~/.boss-pipeline/work/$(date +%F)-<搜索词> \
  --max-scroll 30
```

- `--max-jobs 5` 可先做连通性测试
- 中断后重跑同一条命令会自动续爬、跨文件去重

**产出**：`01_raw/jobs_data_*.csv`、`progress.json`、`jobs_cache.json`

**汇报模板**：

```
【爬取完成】
共爬取: N 条
成功: S 条
失败: F 条
输出文件: 01_raw/jobs_data_*.csv
监控页面: http://localhost:8765
```

---

## 五、阶段2：本地全量判定（默认，零 token）

**前置**：`01_raw/` 下有非空 CSV，画像已就绪。

Dashboard：岗位详情区点「⚡ 进行全量判定」，完成后提示已判定条数与跳过条数。

命令行等价：

```bash
mkdir -p ~/.boss-pipeline/work/<日期>-<搜索词>/02_result
python3 ../job-match-priority/scripts/run_match.py \
  --profile-id <画像id> \
  --input ~/.boss-pipeline/work/<日期>-<搜索词>/01_raw/jobs_data_<时间>.csv \
  --out ~/.boss-pipeline/work/<日期>-<搜索词>/02_result/判定结果.csv
```

- 默认关键词模式，不联网、不消耗 token
- 需要更高召回时再加 `--light-embed`（本地 TF-IDF）或 `--api-embed`（需要 API Key）
- 画像 id 省略时取 `config.json.active_profile`

**产出**：`02_result/*_判定结果.csv`，在原始列后追加 `优先级判断`、`匹配度`、`优先级判断理由`、`能力条目得分`、`叠加信号` 等列。

**汇报模板**：

```
【匹配完成】
总判断: N 条
高优先级: H 条
中优先级: M 条
低优先级: L 条
结果文件: 02_result/*_判定结果.csv
```

判定全低时的排查：见 `TROUBLESHOOTING.md` 第 4 节，或先跑

```bash
python3 ../job-match-priority/scripts/validate_profile.py ~/.boss-pipeline/profiles/<画像id>/profile.json
```

---

## 六、阶段3：打招呼

**前置**：`02_result/` 有判定结果，且 high/medium 合计 > 0。

**顺序固定：dry-run → 首条 → 批量**。

```bash
# 1) 只看名单，不发
python3 ../boss-auto-greet/scripts/boss_auto_greet.py \
  --csv ~/.boss-pipeline/work/<日期>-<搜索词>/02_result/判定结果.csv \
  --dry-run --max-greet 3

# 2) 首条测试（发 1 条后暂停确认）
python3 ../boss-auto-greet/scripts/boss_auto_greet.py \
  --csv ~/.boss-pipeline/work/<日期>-<搜索词>/02_result/判定结果.csv \
  --first-one --max-greet 1

# 3) 确认后批量
python3 ../boss-auto-greet/scripts/boss_auto_greet.py \
  --csv ~/.boss-pipeline/work/<日期>-<搜索词>/02_result/判定结果.csv \
  --max-greet <上限>
```

定时执行（可选）：用 `cron` 工具创建一次性任务，payload 里放上面第 3 条命令；模板见 `references/cron-greet-template.md`。
时间格式与默认时段规则同该模板。

**产出**：CSV 的 `状态` 列；pipeline 目录下同时写 `03_greet/greet_log.csv`。

---

## 七、状态机与续跑

目录结构：

```
~/.boss-pipeline/work/YYYY-MM-DD-<搜索词>/
├── pipeline_state.json
├── 01_raw/
├── 02_result/
├── 03_greet/
└── pipeline.log
```

| 字段 | 说明 |
|------|------|
| `stage` | `crawl\|judge\|greet\|done` |
| `status` | `running\|waiting\|done\|error` |
| `raw_csv` | 01_raw 下的 CSV 绝对路径 |
| `result_csv` | 02_result 下的判定结果 CSV 绝对路径 |
| `counts` | `{total,high,medium,low}` |
| `greet` | `{scheduled_at,done,failed}` |
| `created_at` / `updated_at` | ISO 时间戳 |

中断后：`load_state(work_dir)` → `next_action(state)` 返回 `crawl / judge / greet / done / error`，按返回值续跑。

---

## 八、文件与配置

| 文件 | 用途 |
|------|------|
| `scripts/config_paths.py` | 统一路径解析（`~/.boss-pipeline`、画像、skill 目录、work root） |
| `scripts/env_check.py` | 六项环境自检 |
| `scripts/onboard.py` | 五步向导（CLI） |
| `scripts/serve_dashboard.py` | Dashboard 本地服务器 |
| `scripts/pipeline_state.py` | 状态机读写 |
| `scripts/progress_tracker.py` | 爬取进度追踪 |
| `scripts/schedule_greet.py` | cron 任务参数构建 |
| `dashboard/index.html` | 主看板 |
| `dashboard/wizard.html` | 首次配置向导 |

路径解析优先级：

1. `--home` / `--profile-id` 命令行参数
2. `BOSS_PIPELINE_HOME` / `BOSS_PIPELINE_SKILLS_ROOT`
3. `~/.boss-pipeline`
4. 旧版 `<skill>/config/` 只读回退（会打一次 `[legacy]` 警告）

四个 skill 放在同一父目录时，`config_paths` 会自动定位同级仓库；安装到别处时设 `BOSS_PIPELINE_SKILLS_ROOT`。

---

## 九、异常处理

| 场景 | 处理 |
|------|------|
| CDP 连不上 | 跑 `python3 scripts/env_check.py`，按 CDP 项的修复命令重启浏览器 |
| Boss 未登录 | 暂停等扫码，登录后重跑 `env_check.py` 确认 |
| 端口被占用 | `lsof -nP -iTCP:8765 -sTCP:LISTEN`；本 Dashboard 已在跑就直接用，不要起第二个 |
| 判定全低 | 先跑 `validate_profile.py`，再回向导补关键词/核对目标岗位 |
| 打招呼失败 | 先 `--dry-run` 核对名单，再 `--first-one` 验证页面结构 |
| 缺 Python 依赖 | `python3 -m pip install pyyaml requests websocket-client` |
| 进程中断 | 重跑同一命令，按 `pipeline_state.json` 续跑 |

完整六类故障（现象 → 原因 → 一条命令）见 `TROUBLESHOOTING.md`。

---

## 附录 A（可选 / 历史）：Agent 交接判定

> 默认不需要。只有当用户明确要求把判定交给另一个 agent 时才走这里。
> 本机 Python 引擎已经覆盖日常判定，且零 token；交接路径保留是为了兼容旧工作流。

历史协议：`references/` 下以 `handoff` 结尾的交接说明（旧版 Agent 交接：`sessions_send` 消息格式、回执解析、超时处理）。
相关脚本：`scripts/parse_handoff_reply.py`。

使用前提：

1. 用户明确指定接收方 agent 及其 sessionKey
2. `01_raw/` 下已有爬取完成的 CSV
3. 接收方能够读取用户本机的画像（或者由用户提供画像文件路径）

不满足以上任一条，直接走第五节的本地判定，不要引导用户去找外部 agent。
