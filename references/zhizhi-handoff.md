# zhizhi 交接协议

> main（龙虾CEO）完成爬取后，通过 `sessions_send` 把任务交给 zhizhi 做匹配判断。
> 本文档定义：发什么、回什么、异常怎么处理。

---

## 一、发送消息模板

main → zhizhi 的 `sessions_send` 消息格式：

```
【JD 匹配任务】

raw_csv: <01_raw/ 下的 CSV 绝对路径>
result_dir: <02_result/ 绝对路径>
candidate_config: <候选人配置来源，见下方说明>

请执行 job-match-priority skill，完成后回执如下：
result_csv: <判定结果 CSV 绝对路径>
total: <总条数>
high: <高优先级条数>
medium: <中优先级条数>
low: <低优先级条数>
```

### 三项必填字段

| 字段 | 说明 | 示例 |
|------|------|------|
| `raw_csv` | 爬取产出的原始 CSV 绝对路径 | `<workspace>/boss直聘一体化/2026-08-05-人力数字化/01_raw/jobs_data_20260805.csv` |
| `result_dir` | 判定结果输出目录（绝对路径） | `<workspace>/boss直聘一体化/2026-08-05-人力数字化/02_result/` |
| `candidate_config` | 候选人配置来源，三选一：①路径（已有 profile 文件）②`new`（首次引导）③`last`（沿用上次） | `last` |

### candidate_config 取值规则

| 值 | 含义 | zhizhi 行为 |
|----|------|------------|
| 绝对路径 | 已有候选人画像文件 | 直接读取，跳过 Step 0 |
| `new` | 需要首次引导 | 走 job-match-priority Step 0（收集偏好→生成画像→校准） |
| `last` | 沿用上次画像 | 搜索 `references/candidate-profile.md`，找到则用，找不到等同 `new` |

---

## 二、回执格式

zhizhi 完成判断后，回执消息格式：

```
【匹配完成】

result_csv: <判定结果 CSV 绝对路径>
total: 30
high: 5
medium: 12
low: 13
```

### 回执字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `result_csv` | ✅ | 判定结果 CSV 的绝对路径（main 据此更新 pipeline_state） |
| `total` | ✅ | 总判断条数 |
| `high` | ✅ | 高优先级条数 |
| `medium` | ✅ | 中优先级条数 |
| `low` | ✅ | 低优先级条数 |

---

## 三、异常处理

### 3.1 zhizhi 无候选人画像

**触发条件**：`candidate_config=last` 但找不到 profile 文件，或 `candidate_config=new`。

**zhizhi 行为**：
1. 回复：`需要先建立候选人画像。请告诉我你的求职偏好（岗位方向、薪资、城市等），或发送已有画像文件。`
2. 等待 ii 输入（通过 main 转达或直接对话）。
3. 完成画像建立后，继续执行匹配。

**main 行为**：
- 收到"需要先建立候选人画像"类回执 → 通知 ii：`zhizhi 需要候选人画像才能判断，请在飞书上和 zhizhi 对话完成引导。`
- 不主动代 ii 回答偏好问题。

### 3.2 zhizhi 超时无响应

**判定标准**：`sessions_send` 后超过 30 分钟无回执。

**main 行为**：
1. 发送提醒：`匹配任务仍在进行中吗？raw_csv: xxx，已等待 30 分钟。`
2. 再等 15 分钟仍无响应 → 更新 pipeline_state status=error，通知 ii。

### 3.3 zhizhi 回执格式异常

**判定标准**：回执中缺少必填字段（result_csv / total / high / medium / low）。

**main 行为**：
1. 尝试从自然语言回执中解析（调用 `parse_handoff_reply.py`）。
2. 解析失败 → 回复 zhizhi：`回执格式不完整，请补充：result_csv 路径、total/high/medium/low 计数。`
3. 二次回执仍不完整 → 更新 pipeline_state status=error，通知 ii 人工介入。

### 3.4 zhizhi 判断结果异常

**判定标准**：high + medium + low ≠ total，或 total = 0。

**main 行为**：
- high + medium + low ≠ total → 回复 zhizhi 计数不一致，请修正。
- total = 0 → 可能是 CSV 为空或读取失败，通知 ii 检查 raw_csv。

---

## 四、sessionKey

zhizhi 的 sessionKey（用于 `sessions_send`）：

```
<zhizhi-session-key>
```

---

## 五、完整交互示例

### 正常流程

```
main → zhizhi:
【JD 匹配任务】
raw_csv: /Users/ii/.../01_raw/jobs_data_20260805.csv
result_dir: /Users/ii/.../02_result/
candidate_config: last

请执行 job-match-priority skill，完成后回执如下：
result_csv: ...
total: ...
high: ...
medium: ...
low: ...

zhizhi → main:
【匹配完成】
result_csv: /Users/ii/.../02_result/jobs_data_20260805_判定结果.csv
total: 30
high: 5
medium: 12
low: 13
```

### 首次使用（无画像）

```
main → zhizhi:
【JD 匹配任务】
raw_csv: /Users/ii/.../01_raw/jobs_data_20260805.csv
result_dir: /Users/ii/.../02_result/
candidate_config: new

zhizhi → main:
需要先建立候选人画像。请告诉我你的求职偏好。

main → ii:
zhizhi 需要候选人画像才能判断，请在飞书上和 zhizhi 对话完成引导。

（ii 在飞书上和 zhizhi 完成画像建立后）

zhizhi → main:
【匹配完成】
result_csv: /Users/ii/.../02_result/jobs_data_20260805_判定结果.csv
total: 30
high: 3
medium: 8
low: 19
```
