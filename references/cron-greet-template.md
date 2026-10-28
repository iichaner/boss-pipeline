# T5 cron 定时打招呼模板

> pipeline 阶段3：匹配完成后，定时对 high/medium 岗位执行打招呼。

---

## 一、运行时询问（3 项，不答用默认）

pipeline 进入阶段3时，向用户发送询问卡片：

| # | 问题 | 默认值 | 说明 |
|---|------|--------|------|
| 1 | 打招呼时段？ | 次日 10:00 | 格式：`HH:MM` 或 `明天 HH:MM` |
| 2 | 打招呼语？ | 内置默认语 | 可粘贴自定义内容，或留空用默认 |
| 3 | 单批上限？ | 建议值 = medium+high 总数 | 限制单次最多打招呼条数，防风控 |

**交互原则**：一次性发三个选项，用户不回复则 10 秒后用默认值继续。

---

## 二、默认打招呼语

```
您好，我对贵司在招的岗位很感兴趣，想进一步了解岗位要求。方便的话希望能和您沟通，谢谢！
```

---

## 三、cron 任务创建

### 3.1 调用方式

pipeline 通过 `cron` 工具创建一次性定时任务：

```
cron(action="add", job={
    "name": "boss-greet-{搜索词}-{日期}",
    "schedule": {
        "kind": "at",
        "at": "<ISO-8601 时间戳，如 2026-08-06T10:00:00+08:00>"
    },
    "payload": {
        "kind": "agentTurn",
        "message": "<见 3.2 消息模板>"
    },
    "sessionTarget": "isolated",
    "delivery": {
        "mode": "announce"
    }
})
```

### 3.2 payload.message 模板

```
【自动打招呼任务】

请执行 boss-auto-greet 脚本（脚本绝对路径由 config_paths.greet_script() 解析）：

python3 <greet_script> \
  --csv "<02_result/ 判定结果 CSV 绝对路径>" \
  --message "<打招呼语>" \
  --max-greet <上限数量>

执行完成后汇报结果（成功/失败条数）。
```

> 四个 skill 在同一父目录时，`<greet_script>` 也可写成相对 boss-pipeline 根目录的
> `../boss-auto-greet/scripts/boss_auto_greet.py`。cron 的 cwd 不固定，优先用绝对路径。

### 3.3 时间戳计算

pipeline 解析用户给出的时段：
- `10:00` → 当天或次日（若当前已过该时间则次日），ISO 格式 `YYYY-MM-DDTHH:MM:00+08:00`
- `明天 14:30` → 次日对应时间
- 不回复 → 默认次日 10:00

---

## 四、打招呼状态汇报

cron 任务完成后，pipeline 汇报：

```
【打招呼完成】

总处理: N 条
成功: K 条
失败: F 条
日志: 03_greet/greet_log.csv
```

---

## 五、安全机制

| 机制 | 说明 |
|------|------|
| `--first-one` | 首次使用或用户要求测试时，先跑 1 条确认 |
| `--dry-run` | 仅展示待处理列表，不实际操作 |
| `--max-greet` | 限制单批数量，防风控 |
| 随机间隔 | 每条 3-6 秒（脚本内置） |
| 人工确认 | pipeline 默认询问用户后再创建 cron，不自动触发 |
