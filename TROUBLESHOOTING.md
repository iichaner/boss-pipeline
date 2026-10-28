# TROUBLESHOOTING — 六类常见故障

先跑一遍自检，大多数问题它会直接告诉你怎么修：

```bash
python3 scripts/env_check.py
```

下面每类按「现象 → 原因 → 一条命令」组织。命令都在 **boss-pipeline 仓库根目录** 执行，画像 id 先设好：

```bash
PROFILE=me
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
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

- 进程是 `python` / `Python`：大概率就是本 Dashboard，直接访问 `http://localhost:8765` 即可，不要再起第二个。
- 是别的程序：关掉它，或让启动器自己往后找端口（它会自动尝试 8765–8775）。

---

## 4. 判定全是 low

**现象**

```
high：0 条（0%）
medium：0 条（0%）
low：<N> 条（100%）
```

**原因**（按概率排序）

1. 能力维度没有关键词：JD 永远命中不了任何维度
2. 画像的维度键不在 taxonomy 里，或自评深度普遍低于 JD 要求
3. 阈值设得太高，或硬过滤（城市/薪资/学历/标题）把所有岗位都否掉了
4. 目标岗位关键词与 JD 标题对不上，被标题闸门降档

**一条命令**

```bash
python3 ../job-match-priority/scripts/validate_profile.py ~/.boss-pipeline/profiles/$PROFILE/profile.json
```

先看有没有 `E-DIM-*`（维度不在 taxonomy）和 `W-TAX-001`（维度没关键词）。
没有硬错误但仍全 low，回向导第 2 步补关键词、核对目标岗位和期望城市/薪资；阈值在向导里也能改。

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

## 还是不行

```bash
python3 scripts/env_check.py --json
```

把这段 JSON 和出错时的完整终端输出一起发给维护者。不要只发截图里的一行，`detail` 字段通常才是有用信息。
