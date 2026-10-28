# QUICKSTART — 10 分钟跑通

目标：从零到「爬取 → 本地判定 → dry-run 打招呼」。不需要改代码，不需要读 YAML。

> 所有命令都在 **boss-pipeline 仓库根目录** 执行。
> 四个 skill 目录（`boss-pipeline`、`boss-resume-crawler`、`job-match-priority`、`boss-auto-greet`）放在同一父目录下，脚本会自动找到同级仓库。
> 报错先看 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

---

## 0. 先设一个变量（后面命令都用它）

```bash
# 画像 id 以向导状态为准（默认 me；若已存在同名画像，向导会让你换 id，
# 所以不要在这里写死 me，否则后面的命令会指向一个不存在的画像）
PROFILE=$(python3 scripts/onboard.py --status | sed -n 's/^画像：//p')
if [ -z "$PROFILE" ] || [ "$PROFILE" = "（尚未创建）" ]; then
  echo "还没有可用画像：先完成向导 python3 scripts/onboard.py"
fi
echo "当前画像：$PROFILE"
```

预期：输出 `当前画像：<id>`（还没走向导时会提示先完成向导）。
后面想换画像，重跑向导后用 `--status` 重新取值即可，不要手改成猜测的 id。

如果还没装 Python 依赖：

```bash
python3 -m pip install pyyaml requests websocket-client
```

CloakBrowser 的安装见 [README 的「依赖」](README.md#依赖)。装完再进第 1 步。

---

## 1. 环境自检（约 10 秒）

```bash
python3 scripts/env_check.py
```

首次运行、还没配置画像时，预期类似：

```
✅ Python 版本与依赖：Python 3.12.x；pyyaml / requests / websocket-client 已安装
✅ CloakBrowser 可执行文件：<浏览器可执行文件路径>
⚠️ CDP 调试端口 9222：连不上 127.0.0.1:9222（Connection refused）
   ↳ 修复：在 Dashboard 点「🚀 启动浏览器」，或检查是否已有浏览器占用 9222
⚠️ Boss 直聘登录状态：未打开 Boss 直聘页面
   ↳ 修复：在浏览器里打开 https://www.zhipin.com 并扫码登录
✅ 端口 8765：空闲（Dashboard 尚未启动时可 bind）
⚠️ 配置就绪（画像 + 偏好）：未初始化用户数据目录
   ↳ 修复：python3 scripts/onboard.py  # 走向导创建画像
汇总：3/6 项通过
```

自检只读，不会改任何文件。带 `❌` 的是阻塞项，按它下面那行 `↳ 修复` 执行后重跑。
只想看 JSON（给脚本用）：

```bash
python3 scripts/env_check.py --json
```

预期：输出一个 JSON 对象，含 `checks` 数组和 `summary`。

---

## 2. 启动 Dashboard，完成首次配置（约 3 分钟）

```bash
bash launcher/Boss直聘监控.command
```

预期（路径随安装位置变化）：

```
▶ 使用 Python: <python>
▶ 环境自检
……六项结果……
⚠️ 尚未配置画像：首次使用请完成向导（网页会自动打开）
   命令行方式：<python> <skill目录>/scripts/onboard.py
```

浏览器会自动打开向导页（`http://localhost:8765/wizard.html`）。按 5 步填写：

1. **环境自检**：确认没有阻塞项，点下一步
2. **偏好配置**：身份、目标岗位关键词、期望城市/薪资、能力自评、档位阈值。基本项有默认值；**能力自评必须填**（回车=0 分，会拉低全部档位），每个自评维度至少 1 个关键词（每个关键词 ≥2 字）。任何一项不合格都会在输入框下方直接标红并给中文原因，改完再继续。「高级（可选）」里可改 `high` / `medium` 阈值（默认 95/90，要求 `0 < medium < high ≤ 100`）
3. **试判定**：用 6 条内置样例 JD（运营/技术/职能各 2 条，仅用于预演）跑本地判定，看看档位是否符合直觉；不符合就回第 2 步补关键词/调阈值
4. **试爬取（可选）**：只爬 5 条，可以直接跳过
5. **试打招呼（dry-run）**：只生成计划，不会真的发送；确认发布要**手动输入 `y` 或 `yes`**（回车或其它键都视为取消，避免误触）

中途关掉浏览器不会丢进度，重新打开会从断点继续。

不想用网页，也可以走命令行：

```bash
python3 scripts/onboard.py
```

预期开头：

```
从第 1 步继续（共 5 步）

[1/5] 环境自检
……
```

随时查看进度：

```bash
python3 scripts/onboard.py --status
```

预期：

```
boss-pipeline 向导状态
──────────────────────────────────────────
结论：已就绪（needs_onboarding=false）
画像：<画像id>
当前步骤：第 <n> 步 / 共 5 步 · <步骤名>
……
```

---

## 3. 登录 Boss 直聘（约 1 分钟）

在 Dashboard 顶部点「🚀 启动浏览器」，在弹出的浏览器窗口里扫码登录。

回到终端验证：

```bash
python3 scripts/env_check.py
```

预期这一行变成：

```
✅ Boss 直聘登录状态：已登录（页面标题：<页面标题>）
```

---

## 4. 粘贴列表页 URL，开始爬取

1. 在 Boss 直聘网页里搜索岗位，复制地址栏里的列表页 URL（形如 `https://www.zhipin.com/web/geek/jobs?...`）
2. 回到 Dashboard，点「🕷️ 新建爬取任务」，粘贴 URL，确认开始
3. 页面会显示滚动进度和已抓条数；产物写入 `~/.boss-pipeline/work/YYYY-MM-DD-<搜索词>/01_raw/`

先跑一次爬取自检（不抓取、不写文件，10 秒）：

```bash
python3 ../boss-resume-crawler/scripts/boss_extract_cdp.py \
  --url "<列表页URL>" --dry-run-check
```

它只会检查 CDP / 页面 / 登录，并用中文告诉你卡在哪一步：

```
✅ CDP 调试端口 9222：已连通
✅ 列表页：https://www.zhipin.com/web/geek/jobs?query=...
✅ 登录状态：已登录
✅ 职位卡片：30 条
自检完成：可以开始爬取。
```

自检通过后再只爬 5 条做连通性测试（命令行）：

```bash
python3 ../boss-resume-crawler/scripts/boss_extract_cdp.py \
  --url "<列表页URL>" \
  --output ~/.boss-pipeline/work/$(date +%F)-手动 \
  --max-scroll 3 --max-jobs 5
```

失败会返回非 0 退出码并给一条修复命令，不再静默 exit 0：

| 退出码 | 含义 |
|--------|------|
| 0 | 成功（至少写入 1 条 CSV；或本次无新增但已有 CSV） |
| 4 | 连不上 CDP 9222 |
| 5 | 打不开/找不到列表页（有 `--url` 时会自动开新标签页导航） |
| 6 | 列表页没有职位卡片（风控/验证页） |
| 7 | 一条都没写入 CSV |
| 8 | 未登录 |

预期结尾：

```
==================================================
爬取完成
==================================================
本次新增:     5 条
本次失败:     0 条
去重/限量跳过: 0 条
累计总量:     5 条
输出文件:     ~/.boss-pipeline/work/<日期>-手动/jobs_data_<时间>.csv
```

字段说明见 `../boss-resume-crawler/README.md`。爬取中断后重跑同一命令会自动续爬、去重。

---

## 5. 全量判定（本地引擎，零 token）

Dashboard：在岗位详情区点「⚡ 进行全量判定」，等待提示「全量判定完成」。

命令行等价做法：

```bash
WORK=$(ls -td ~/.boss-pipeline/work/20*/ | head -1)
WORK=${WORK%/}
RAW=$(ls -t "$WORK"/01_raw/jobs_data_*.csv | head -1)
mkdir -p "$WORK/02_result"
python3 ../job-match-priority/scripts/run_match.py \
  --profile-id "$PROFILE" \
  --input "$RAW" \
  --out "$WORK/02_result/判定结果.csv"
```

预期：

```
[profile] 使用画像 id=<画像id>（来自 ~/.boss-pipeline）
读取 <N> 条（去重 0 条）
匹配完成
总计：<N> 条（去重后）
high：<H> 条（<百分比>%）
medium：<M> 条（<百分比>%）
low：<L> 条（<百分比>%）
结果文件：~/.boss-pipeline/work/<日期>-<搜索词>/02_result/判定结果.csv
```

结果 CSV 在原始列后追加 `优先级判断`、`匹配度`、`优先级判断理由`、`能力条目得分`、`叠加信号` 等列。
如果全是 low，先看 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 的「判定全低」。

参数含义：

```bash
python3 ../job-match-priority/scripts/run_match.py --help
```

预期：列出 `--profile-id`、`--input`、`--out`、`--limit`、`--light-embed`、`--api-embed` 等参数。

---

## 6. dry-run 打招呼（默认不发）

先看会发给谁，不做任何操作：

```bash
WORK=$(ls -td ~/.boss-pipeline/work/20*/ | head -1)
WORK=${WORK%/}
python3 ../boss-auto-greet/scripts/boss_auto_greet.py \
  --csv "$WORK/02_result/判定结果.csv" \
  --dry-run --max-greet 3
```

预期：

```
============================================================
Boss直聘自动打招呼（CDP 模式）
============================================================
CSV 总记录: <N>
待打招呼: 3 条 (high=<H>, medium=<M>)

============================================================
DRY RUN - 待处理列表
============================================================
  [1] <职位名称> | <公司名称> | medium | <岗位链接>
  [2] ...
  [3] ...

共 3 条待处理。去掉 --dry-run 参数执行实际操作。
```

确认名单没问题后，先测 1 条：

```bash
WORK=$(ls -td ~/.boss-pipeline/work/20*/ | head -1)
WORK=${WORK%/}
python3 ../boss-auto-greet/scripts/boss_auto_greet.py \
  --csv "$WORK/02_result/判定结果.csv" \
  --first-one --max-greet 1
```

首条发送成功并确认后，再去掉测试参数批量执行。打招呼不可撤回，务必先 dry-run。

---

## 7. 没有 Boss 账号也能试（本地判定预演）

只想先验证「判定引擎」是否靠谱，不需要登录、不需要爬取：

```bash
python3 ../job-match-priority/scripts/run_match.py \
  --profile-id "$PROFILE" \
  --input ../job-match-priority/templates/samples/jds_onboarding.json \
  --out /tmp/jd_preview.csv
```

`jds_onboarding.json` 是 6 条中性岗位（运营/技术/职能各 2 条），
**仅用于预演**：公司名是「示例…」，薪资写「面议」，不含任何真实招聘信息或个人数据。
用它跑出来的档位只说明「你的画像对这三类岗位的区分度」，不代表真实市场结果。

想用 CSV 版本演练下游（打招呼 dry-run）：

```bash
python3 ../job-match-priority/scripts/run_match.py \
  --profile-id "$PROFILE" \
  --input ../job-match-priority/sample/jds_sample.csv \
  --out /tmp/jd_sample_result.csv
python3 ../boss-auto-greet/scripts/boss_auto_greet.py --csv /tmp/jd_sample_result.csv --dry-run
```

后者只列出名单，不连浏览器、不发送。样例行的 `job_id` 是占位值，
`https://www.zhipin.com/job_detail/sample-00x` 打不开——这里只演示「名单 + 档位」格式。

---

## 跑通之后

- 日常只需要：双击启动器 → 粘 URL → 爬取 → 「⚡ 进行全量判定」→ dry-run 确认 → 发送
- 想调阈值、加关键词、换岗位方向：重新打开 `/wizard.html` 或跑 `python3 scripts/onboard.py`（阈值在第 2 步「高级（可选）」）
- 判定方法说明：`../job-match-priority/FRAMEWORK.md`
- 出问题：`TROUBLESHOOTING.md`
