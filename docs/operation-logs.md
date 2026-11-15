# 全平台操作日志与 LLM 调用日志（R85）

定位：给用户自查问题用的**调试工具**（不是合规审计）。目标是能回看平台内部调用细节：
真实请求体、真实返回、内部调用链与失败堆栈。

## 1. 目标

- 记录平台内所有有副作用的操作（生成 / 保存 / 删除 / 切分 / 配音 / 素材 / 编排 / 导入导出 / 配置修改）。
- 完整记录每一次大模型调用：模型名、请求参数、system 与 user 全文、返回文本、思考过程、
  工具调用参数、首字与总耗时、token 用量、HTTP 地址与状态码、原始 SSE 事件、异常堆栈。
- 本地落盘、按天滚动、可按工程 / 类型 / 时间 / 关键字 / trace 查询。
- 磁盘占用有上限，超限清理最旧分卷；API Key 等敏感值写入前脱敏。
- 写日志异步、失败不影响业务请求。

## 2. 实现

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 日志核心 | `src/novel_drama/oplog.py` | 记录构造、脱敏、异步写入、按天分卷、磁盘清理、查询、LLM / CLI 采集钩子 |
| HTTP 层 | `src/novel_drama/web/logs_api.py` | 操作日志中间件（写接口 → 中文动作名 / 目标 / 参数 / 结果 / 耗时 / 错误）+ 查询接口 |
| 页面 | `web-next/js/pages/logs.js`、`#/logs` | 左下角图标栏「日志」，筛选 + 列表 + 分段详情 + 复制 + trace 串联 + 清空 |
| 挂载点 | `web/app.py`（注册一行）、`web/actions.py`（CLI 记录）、`cli.py`（子进程初始化） | 最小插入，不改动既有逻辑 |

### 记录结构

```json
{
  "id": "lg_...", "ts": "2026-11-14T16:11:53.114+08:00",
  "kind": "op | llm", "action": "chapter.save", "label": "保存章节正文",
  "trace_id": "tr_...", "project": "/abs/project",
  "target": "章节 chp_...", "summary": "content 66 字 · 字段 chapter、content",
  "status": "ok | error | partial", "duration_ms": 4061, "error": "...",
  "detail": { "http": {...}, "params": {...}, "system": "...", "messages": [...], "response": {...}, "steps": [...], "error": {...} }
}
```

LLM 记录额外包含 `params`（temperature / max_tokens / chat_template_kwargs.enable_thinking /
tool_choice 等原样请求参数）、`payload`（完整原始请求体）、`response.raw`（非流式原始响应 /
流式首尾 SSE 事件）、`response.usage`（上游未返回时标注）。

### 写入与磁盘保护

- 后台守护线程 + 队列批量追加；队列满直接丢弃，任何写入异常静默处理。
- 单行一次 `write`，并用 `fcntl.flock` 做跨进程互斥（Web 主进程 / CLI 子进程可能同时写）。
- 文件名 `YYYY-MM-DD.jsonl`，超过 `logs.max_file_mb` 滚动为 `YYYY-MM-DD.001.jsonl`；
  全目录超过 `logs.max_total_mb` 按修改时间删除最旧分卷（保留最新一卷）。
- 单条超过 `logs.max_record_mb` 时逐步压缩长文本并标记 `detail.truncated`。
- CLI 子进程退出时通过 `atexit` 同步补写，避免「跑完即退」丢失最后几条。
- 配置（均可省略）：`logs.enabled`、`logs.dir`、`logs.max_file_mb`（默认 32）、
  `logs.max_total_mb`（默认 512）、`logs.max_record_mb`（默认 4）。

### 调用链串联

- 每个写请求生成 `trace_id`，通过 contextvar 传给同请求内的 CLI / LLM 记录；
- 长命令子进程通过环境变量 `NDW_LOG_TRACE / NDW_LOG_PROJECT / NDW_LOG_ACTION / NDW_LOG_DIR`
  继承 trace，因此「页面操作 → CLI → LLM」属于同一条 trace；
- 详情页「看同一次操作」按 trace 过滤，列表行同时展示 trace 短码。

## 3. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/logs` | 过滤查询：`kind`(op/llm)、`project`、`q`、`since`、`until`、`trace`、`limit`、`offset`；最新在前 |
| GET | `/api/logs/record` | `file` + `line` 回读完整记录（含完整提示词与返回文本） |
| GET | `/api/logs/stats` | 目录、分卷、总量与各项上限 |
| GET | `/api/logs/facets` | 最近 N 天出现过的工程（筛选下拉） |
| POST | `/api/logs/clear` | 删除全部日志文件（界面需二次确认） |

## 4. 安全

- 递归脱敏字段名含 `api_key / apikey / token / secret / password / authorization / cookie` 的值；
- 文本中的 `Bearer xxx`、`sk-xxx`、`api_key=xxx` 统一替换为 `***`；
- CLI 参数中敏感开关的后续值替换为 `***`；
- 不记录请求头，密钥不会因记录头部而泄漏。

## 5. 已知边界

- token 用量：上游流式接口未返回 `usage` 时只能标注「上游未返回」，不做估算。
- ComfyUI / 本地 TTS：以 CLI 步骤（实际参数、退出码、耗时、stderr）记录，未逐次记录轮询请求，
  避免长任务轮询把日志刷爆。
- 日志目录默认 `var/logs/`（已加入 `.gitignore`），不入库。
