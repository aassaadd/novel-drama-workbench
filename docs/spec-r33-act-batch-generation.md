# R33 按幕一键生成整幕正文

## 目标

在小说视图的分层大纲里，每一幕提供「生成整幕正文」入口：按章节顺序一次跑完该幕所有章节，
不再需要逐章点击。复用现有单章生成链路，不新增第二套生成逻辑。

## 复用边界

批量只做调度，单章仍走 `writing/service.py::generate_chapter`：

- 按本章节拍分组推进，节拍写完即收尾；
- 章成后更新章摘要与全书滚动摘要（`writing/recap.py`）；
- `ProjectStore.save_chapter` 在正文变化时自动存档旧版本（版本记录可回退）。

因此批量入口不复制提示词、不复制轮次控制、不直接写章节文件。

## 后端

### `writing/act_batch.py`

- `resolve_act(store, act_id, act_index)`：在大纲里定位幕（ID 优先，其次 1 基序号），返回幕、
  全书幕数与章节列表；没有大纲 / 找不到幕 / 幕内无章节时抛中文 `ValueError`。
- `generate_act_chapters(...)`：按章节顺序逐章生成。
  - `mode=fill`：已有正文的章直接跳过并计入 `skipped`；
  - `mode=regen`：已有正文也重写（旧正文由版本存档保留）；
  - 单章异常只记该章失败原因，继续后续章节；
  - 章与章之间检查 `is_canceled`，命中时抛 `ActBatchCanceled(result)`，已完成章节已落盘；
  - 通过 `on_event` 上报 `act_start / batch / chapter_start / chapter_skip / chapter_done /
    chapter_error`，通过 `on_delta` 透传当前章正文增量。
- 返回 `{act_id, act_index, act_title, mode, total, succeeded, skipped, failed, chapters, canceled, message}`，
  `message` 形如「成功 3 章 / 跳过 2 章 / 失败 1 章」。

### SSE 接口

`POST /api/ui/act/generate/stream`

请求体：`{path, act, act_index, mode: fill|regen, multi_turn?, beats_per_round?, max_rounds?}`。

事件：

| 事件 | 负载 |
| --- | --- |
| `start` | `{ok, job_id}`（任务队列 ID，供停止） |
| `act_start` | 幕信息、总章数、模式、章节清单 |
| `batch` | 任务队列进度（`phase=chapters`，已完成 k/n 章，百分比） |
| `chapter_start` | `{index, total, chapter_id, chapter_index, title}` |
| `delta` | `{text}` 当前章正文增量 |
| `chapter_skip` | `{index, total, title, words, reason}` |
| `chapter_done` | `{index, total, title, words, rounds, beats_done, beats_total, summary_error, notices}` |
| `chapter_error` | `{index, total, title, code, message}`（`code` 如 `BINDING_POV_MISSING`） |
| `done` | `{result}` 汇总（chapters 每项含 `status / code / message / notices`） |
| `canceled` | `{message, result?}` |
| `error` | `{message, code?}` |

任务通过 `UiJob(kind="act")` 登记，任务页类型显示「整幕正文」，可在任务页或浮层停止。
任务终态与章节结果一致：零成功且有失败 → `failed`；有成功也有失败 → `partial`（部分完成）；
全部成功 → `succeeded`，三种情况都把「成功/跳过/失败」汇总写进任务进度。

## 前端

- `pages/novel.js`：幕标题行新增「✨ 生成整幕正文」按钮；弹窗展示本幕章数、已落章数、
  已有正文数，并选择「只补缺失 / 重新生成已有正文」，未落章会先提示。
- 单章与整幕共用同一个视角弹窗 `openPovSetupDialog(chapters, onDone, options)`：
  - 单章只列一行，整幕列出缺视角的多章；整幕多章时提供「全部使用全知视角」；
  - 打开即预选当前设置（角色 / 本章显式全知 / 沿用设定库默认全知），只提交真正改动的章；
  - 全局「叙事视角=全知」时，弹窗顶部直接写明「设定库默认视角：全知」，不做静默绕过。
- 两个生成弹窗都有一行只读视角状态，整行可点，点击打开同一视角弹窗：
  - 单章：「本章视角：沈砚（第三人称限知）」/「本章视角：全知（来自设定库默认）」/
    「本章视角：未设置（生成前需指定角色或选择全知）」；
  - 整幕：「本幕视角：2/5 章未设置（点击设置）」或「5 章均可用（未单独设置的章按设定库默认全知）」。
- 点「开始生成」先禁用按钮并显示「正在检查视角…」，随后重新查绑定；缺 POV 就打开共用弹窗，
  保存后用已捕获的生成参数自动继续，不再出现「请再次点击生成」。
- 生成弹窗里的系统默认值全部标「默认值」并注来源，来源分三类：
  `page` 来自设置页 / `file` 来自配置文件 / `builtin` 内置默认（设置页未改过）。
  涉及每轮节拍数、最大轮数、按节拍分轮开关、温度、思考开关与强度。
  输出上限固定显示「无人工上限（请求 max_tokens 取上下文剩余空间）」，不作为可调项。
  整幕弹窗没有逐章参数输入，也以只读行展示这些值，不允许静默生效。
- `progress.js`：`openProgress` 新增可选 `onStop`，运行时在浮层头部显示「停止」按钮；
  `fail` 支持 `actions`，错误可直接跳转到修复入口。
- 生成中浮层显示「第 k/N 章 · 正在生成《标题》」、整体百分比与正文增量；完成展示
  「成功 N 章 / 跳过 M 章 / 失败 K 章」。
- 自动补救写明在结果里：视角补齐后的自动续跑写「自动续跑：…」；内容校验自动重试、
  达到最大轮数自动停止等写入 `notices`，逐章与最终汇总都会显示「自动处理：…」。
- 失败汇总按「错误码 + 原因」聚合：同一原因只写一句并给出章数与章号，保留错误码；
  含 POV 失败时提供「去设置视角（N 章）」按钮，点击打开共用视角弹窗，保存后按
  「只补缺失」续跑；另有「刷新目录与正文」按钮。
- 停止走 `POST /api/ui/jobs/cancel`；停止后已完成章节保留，浮层显示已处理汇总。
- 生成结束的刷新只更新左侧目录（保留滚动与焦点）、当前幕统计与当前正文卡，不整页重绘。
- `pages/tasks.js`：任务队列类型映射新增 `act → 整幕正文`，状态新增「部分完成」。

## 边界与取舍

- 章节尚未落地（没有章节文件）时该章记失败，原因提示先「落地成章节」，不影响其它章。
- 停止发生在章内时，当前章未完成的正文不落盘；已完成章节完整保留。
- 项目未上线，不保留旧的按幕入口兼容分支。

## 验证

- `tests/test_act_batch.py`（离线适配器，5 项）：
  - 只补缺失首次逐章生成，二次运行全部跳过；
  - 重新生成成功且 revision 递增；
  - 删除一个章节文件后该章失败、另一章成功；
  - 章间取消抛 `ActBatchCanceled`，第一章已落盘、第二章未生成；
  - 缺视角时逐章记 `BINDING_POV_MISSING` 且错误码随事件/结果回传；
  - 任务状态：零成功 → `failed`、混合 → `partial`、全成功 → `succeeded`，汇总写入进度。
- 进程内 FastAPI TestClient 真实失败场景（有限视角 + 章节无 POV）：
  - 失败轮：3 章全失败，`chapter_error` 均带 `BINDING_POV_MISSING`，任务 `failed`、
    进度为「整幕失败：3 章均未生成」、错误为「成功 0 章 / 跳过 0 章 / 失败 3 章」；
  - 只补一章视角：1 成功 2 失败，任务 `partial`，进度文案为汇总；
  - 补齐全部视角：2 成功 1 跳过，任务 `succeeded`，进度文案为汇总。
- 前端 `groupActFailures / actFailureDetail` 用真实源码 + 3 条同因失败验证：
  聚合为 1 组并输出「3 章（第 1、2、3 章）：…（BINDING_POV_MISSING）」。
- 视角状态文案用真实源码（node 抽取 `povStatusOf / chapterPovValue / actPovStatusOf`）验证：
  - 全局默认全知 → 「本章视角：全知（来自设定库默认）」；
  - 本章显式全知 → 「本章视角：全知（本章指定）」；
  - 有视角人物 → 「本章视角：沈砚（第三人称限知）」；
  - 缺视角 → 未设置（warn 态）；整幕缺 2/5 章显示「2/5 章未设置（点击设置）」。
- 默认参数来源：`GET /api/ui/config/connection` 返回 `generation.sources` 与 `llm.sources`，
  实测未改过的多轮参数为 `builtin`、设置页保存过的模型参数为 `page`。
- 自动补救可见：`max_rounds=1` 跑 3 拍章节返回
  `notices=['已达到最大轮数 1，自动停止：本章还有 2 个节拍未写…']`，`stop_reason=max_rounds`。
- `py_compile`、`node --check`、`ruff check`（新增文件）通过；全量单测 82 项通过。

