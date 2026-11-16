# 媒体生成总线（MediaBus）

> 状态：已实现（2026-11-16）。声音、音效、配乐、图片、视频、图生视频的生成全部走同一套任务模型与接口，页面统一用 `web-next/js/media-bus.js`。

## 为什么需要

改造前每条通道各有一套启动接口与进度查询（`/api/ui/sfx/job`、`/api/ui/tts/batch-all/progress`、音色批次轮询、`/api/ui/bgm/generate` 等），页面各自手写轮询与进度卡，任务面板只能看到其中一部分。现在统一为「一次登记 → 一个入口 → 一份任务 → 一处展示」。

## 分层

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 动作登记 | `src/novel_drama/application/support/media_plan.py` | `MediaActionSpec`：action、通道、任务类型、标题、阶段、是否可取消、是否占 GPU、参数说明 |
| 总线 | `src/novel_drama/application/support/media_bus.py` | 处理器注册、`plan/run/snapshot/cancel`、线程执行、终态归一化、操作日志 |
| 执行器 | `src/novel_drama/application/support/media_handlers.py` | 12 个动作的具体实现：单行/本章/全书 TTS、音色抽卡、批量音色、试听、单条/本章音效、BGM、配图、视频、图生视频 |
| 路由 | `src/novel_drama/interface/http/routers/media_bus.py` | 唯一的四个接口 |
| 任务记录 | `src/novel_drama/application/production/job_tracker.py` | `.ndw/ui_jobs.json`（`schema_version=2`），统一任务结构 |
| 前端 | `web-next/js/media-bus.js` + `media-format.js` | 计划确认、启动、统一进度卡、任务轮询、取消、任务卡片 |

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/ui/media/run` | 唯一启动入口，body `{action, params}`；立即返回任务快照，执行在后台线程 |
| GET | `/api/ui/media/plan` | 动作计划：通道、阶段、预计数量、是否排队、取消方式 |
| GET | `/api/ui/media/job/{job_id}` | 任务快照（统一字段） |
| POST | `/api/ui/media/cancel` | 按 `{path, job_id}` 取消，幂等 |
| GET | `/api/ui/queue` | 任务页/任务面板数据源：媒体任务 + GPU 队列任务统一字段 |

## 动作

`tts.line`、`tts.chapter`、`tts.book`、`voice.design`、`voice.batch`、`voice.preview`、`sfx.item`、`sfx.chapter`、`bgm`、`image.generate`、`video.generate`、`anim`。

## 统一任务结构

```
{
  id, job_id, kind, action, channel, parent_id, cancelable,
  title, target, status, percent, stage, current,
  error, artifacts, progress: {percent, current},
  meta: {llm_calls, result, ...}, timings, created_at
}
```

- 状态：`queued / running / succeeded / partial / failed / canceled / interrupted`。
- 页面重启后，早于进程启动时间的 `running/queued` 任务统一标记 `interrupted`。
- 任务文件只保留最近 300 条；`schema_version` 不匹配时直接丢弃旧文件（未上线，不做兼容）。

## 取消与暂停

- 取消：`MediaBus.cancel` 调用通用协作取消；执行器在关键节点检查 `cancel_requested()`，已完成产物保留，后续项不再执行。
- 批量配音暂停：执行器在批次间检查 `.ndw/tts.paused`，暂停时原地等待，点继续后恢复；现有 `GET/POST /api/ui/tts/pause` 不变。
- 媒体任务内嵌的 LLM 调用：执行期间用 `llm_cancel.begin_media_job(job_id)` 绑定父任务，`LlmBus` 自动登记子调用；取消父任务会级联取消子调用（在下一个分片检查点生效），任务 `meta.llm_calls` 记录嵌套调用次数。

## 前端约束

- 页面只能通过 `runMedia / watchMediaJob / cancelMediaJob / fetchMediaPlan` 操作媒体任务，不得直连 `/api/ui/media/*`。
- 页面不得自行轮询任务；进度卡轮询由 `media-bus.js` 负责。
- 任务展示统一使用 `mediaJobCardHTML / mediaJobRowsHTML / jobProgressText / jobErrorSummary`。
- 守卫测试：`tests/test_media_bus_guard.py`（后端接口唯一性、旧接口回流、页面轮询）与 `tests/test_llm_progress_guard.py`（动作登记）。
