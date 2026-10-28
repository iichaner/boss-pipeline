# TROUBLESHOOTING — 六类常见故障

先跑一遍自检，大多数问题它会直接告诉你怎么修：

```bash
python3 scripts/env_check.py
```

下面每类按「现象 → 原因 → 一条命令」组织。命令都在 **boss-pipeline 仓库根目录** 执行，画像 id 先设好：

```bash
# 画像 id 以 --status 为准（不要写死 me；重试/重名时可能是 me-2）
PROFILE=$(python3 scripts/onboard.py --status | sed -n 's/^画像：//p')
```

---

## 1. CDP 连不上（9222 无响应）

**现象**

```
❌ CDP 调试端口 9222：连不上 127.0.0.1:9222（Connection refused）
```

或爬取/打招呼时报 `CDP 未连接`、`WebSocket 连接失败`。

**原因**：浏览器没启动、启动时没带 `--remote-debugging-port=9222`，或 9222 被别的进程占用。

**一条命令**

```bash
curl -s http://127.0.0.1:9222/json/version
```

正常会返回一段 JSON（含 `"Browser"` 和 `"webSocketDebuggerUrl"`）。
返回空或报错 → 关闭所有测试用浏览器窗口，回到 Dashboard 点「🚀 启动浏览器」重新拉起。

爬取前想一次确认「CDP + 页面 + 登录」三件事，跑爬取自检（不抓取、不写文件）：

```bash
python3 ../boss-resume-crawler/scripts/boss_extract_cdp.py \
  --url "<列表页URL>" --dry-run-check
```

退出码：0 可以开始爬；4 CDP 不通；5 列表页打不开；8 未登录。
爬取失败也会返回非 0（6 没有职位卡片、7 一条都没写入 CSV），不会再静默成功。

---

## 2. 未登录 / 登录过期

**现象**

```
⚠️ Boss 直聘登录状态：未打开 Boss 直聘页面
```

或页面标题出现「登录」，爬取时提示「Boss 直聘未登录」。

**原因**：没有扫码、扫码后页面被关掉，或登录态过期。

**一条命令**

```bash
python3 scripts/env_check.py
```

看「Boss 直聘登录状态」那一行的 `↳ 修复`：在浏览器窗口里打开 `https://www.zhipin.com` 扫码，登录后再跑一次，直到显示 `✅ 已登录`。

---

## 3. 端口被占用（8765 起不来）

**现象**

```
❌ 端口 8765：已被 <进程> (pid <pid>) 占用
```

或双击启动器后浏览器打开却是别的服务。

**原因**：上一次 Dashboard 没退出、launchd 常驻服务已在跑，或其他程序占了 8765。

**一条命令**

```bash
curl -s http://127.0.0.1:8765/api/health
```

看返回里的 `home_fingerprint` 和自检里的「端口 8765」那一行：

- 指纹与本地一致（自检显示「已被本 Dashboard 占用（home 指纹一致 …）」）：就是本套数据，直接访问 `http://localhost:8765`，不要再起第二个。
- 指纹不同 / 返回 404 或非 JSON：这是另一套数据或别的程序。**不要直接用它**，双击启动器会自动在 8765–8775 里找一个空闲端口，并优先复用同指纹的实例。
- 想彻底释放端口：`lsof -nP -iTCP:8765 -sTCP:LISTEN` 找到 pid 后关闭对应进程。

---

## 4. 判定全是 low

**现象**

```
high：0 条（0%）
medium：0 条（0%）
low：<N> 条（100%）
```

**判定全 low 的五个原因**（每个给一条排查命令）

先把最近一次结果准备好：

```bash
RESULT=$(ls -t ~/.boss-pipeline/work/20*/02_result/*判定结果*.csv | head -1)
```

### 4.1 角色/标题不匹配（岗位方向被闸门否掉）

现象：理由里大量 `硬过滤：岗位方向不匹配`。标题必须命中「目标岗位关键词」之一。

```bash
python3 scripts/explain_low.py --result "$RESULT" --cause role
```

修：向导第 2 步「目标岗位关键词」补上真实会出现的标题词（≥2 字）。

### 4.2 城市 / 薪资不匹配

现象：`硬过滤：城市不符` 或 `硬过滤：薪资完全不覆盖`。后者在「只看高薪岗」时几乎必现。

```bash
python3 scripts/explain_low.py --result "$RESULT" --cause city
python3 scripts/explain_low.py --result "$RESULT" --cause salary
```

修：第 2 步改期望城市/薪资；异地可远程就勾「接受远程」。

### 4.3 行业排除

现象：`硬过滤：行业…` / 专业资质类理由。常见于「排除的行业」写得太宽（例如写了「互联网」）。

```bash
python3 scripts/explain_low.py --result "$RESULT" --cause industry
```

修：第 2 步「排除的行业」只留真正不能接受的行业。

### 4.4 僵尸岗规则（JD 短 + 高薪 / 标题特征 / 壳公司）

现象：`硬过滤：僵尸岗位(空泛高薪)` 等。**新画像出厂已关闭**；只有画像 prefs 里显式开了才会命中。

```bash
python3 scripts/explain_low.py --result "$RESULT" --cause zombie
```

修：把 `rules.hard_filters.zombie_empty_highpay` 改成 `{}`（或把 `zombie_title_keywords` 置空），规则机制仍在，只是不再默认误杀。

### 4.5 taxonomy 关键词太少 / 自评偏低 / 阈值偏高

现象：没有任何硬过滤，`匹配度` 低。能力维度没词 = JD 永远命中不了；阈值默认 95/90，偏高。

```bash
python3 scripts/explain_low.py --result "$RESULT" --cause keywords --profile-id "$PROFILE"
python3 ../job-match-priority/scripts/validate_profile.py ~/.boss-pipeline/profiles/$PROFILE/profile.json
```

修：第 2 步补关键词与自评；阈值在同一步「高级（可选）」里改（默认 `high=95, medium=90`，要求 `0 < medium < high ≤ 100`）。

判定方法细节见 `../job-match-priority/FRAMEWORK.md`。

---

## 5. 打招呼失败

**现象**

```
[1] <职位名称> ... ❌ 失败: 沟通按钮未找到
```

或全部条目的 `状态` 列都写成 `失败:<原因>`。

**原因**：未登录、页面结构变化、岗位已关闭、达到当日上限、或风控拦截（`ACCOUNT_RISK`）。

**一条命令**

```bash
python3 ../boss-auto-greet/scripts/boss_auto_greet.py \
  --csv ~/.boss-pipeline/work/<日期>-<搜索词>/02_result/判定结果.csv \
  --dry-run --max-greet 1
```

先确认 CSV 里确实有 high/medium 且链接可打开。dry-run 能列出目标但实际发送失败时：

- 先发 1 条验证登录和页面结构：加 `--first-one --max-greet 1`
- 出现 `ACCOUNT_RISK`：立即停止，换浏览器 profile 或等一段时间
- 岗位已关闭/已招满：换下一条，不必重试

CSV 里已有 `已打招呼` 的记录会被自动跳过，断点续打安全。

---

## 6. 缺少 Python 依赖

**现象**

```
❌ Python 版本与依赖：Python 3.x；缺少：pyyaml, websocket-client
```

或启动时报 `ModuleNotFoundError: No module named 'yaml'` / `'websocket'`。

**原因**：当前 Python 没装这三个包，或启动器选到了另一个 Python。

**一条命令**

```bash
python3 -m pip install pyyaml requests websocket-client
```

装完重跑 `python3 scripts/env_check.py`，确认第一项变成 `✅`。
如果机器上有多个 Python，用自检输出里的 `python` 路径执行同一条命令，例如：

```bash
<python路径> -m pip install pyyaml requests websocket-client
```

---

## 7. 向导提交失败 / 画像 id 变了

**现象**

```
提交失败：
  - E-SCHEMA-006 rules.taxonomy[0].keywords[0] 长度 < 2
```

或重试几次后画像 id 从 `me` 变成了 `me-3`，文档里的命令报「画像不存在」。

**原因**：早期版本只在最后一步做 schema 校验，关键词太短（<2 字）会拖到提交时才报，
且每次重试都会新建一个画像目录。新版已在输入时就逐字段校验，并复用同一份草稿 id。

**一条命令**

```bash
python3 scripts/onboard.py --status      # 看哪个字段不合格 + 当前画像 id
python3 scripts/onboard.py --step 2      # 回到第 2 步改（不会丢已填内容）
```

按提示把关键词改到 ≥2 字即可；已发布的同名画像不会被覆盖，若提示已存在，
在第 2 步换一个画像 id 再提交。

---

## 还是不行

```bash
python3 scripts/env_check.py --json
```

把这段 JSON 和出错时的完整终端输出一起发给维护者。不要只发截图里的一行，`detail` 字段通常才是有用信息。
