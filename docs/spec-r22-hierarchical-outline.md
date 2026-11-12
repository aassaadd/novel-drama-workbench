# spec-r22 三级分层大纲生成（幕 → 章 → 节拍）

## 目标

把「一次调用产出整本大纲」改为三级分层生成，节拍在生成阶段一次补齐，
避免整本 JSON 过大导致截断、失败重来与进度不可见。

- 阶段一：1 次调用，只出幕列表（标题、目标/概要、预计章数）；
- 阶段二：每幕 1 次调用，出该幕章节（标题 + 概要）；
- 阶段三：每章 1 次调用，一次补齐该章全部节拍。

## 接口与事件

新增 `POST /api/ui/outline/hierarchical/stream`（SSE，复用 `_progress_response`）：

```
{path, acts?, chapters_per_act?, beats_per_chapter?, words?, binding?, dry_run?, resume?}
```

事件序列：

| 事件 | 负载 | 用途 |
| --- | --- | --- |
| `start` | `{ok}` | 连接建立 |
| `stage` | `{stage:"acts", total:N}` | 正在规划幕结构 |
| `batch` | `{unit:"act", index, total, name}` | 第 k/N 幕（生成该幕章节） |
| `batch` | `{unit:"chapter", index, total, act_index, act_total, name}` | 第 k/N 章（生成该章节拍） |
| `chars` | 同上 + `{chars}` | 本层已接收的增量长度 |
| `done` | `{result:{chapters, beats, failures, ...}}` | 完成（含失败项） |
| `error` | `{message}` | 整体失败（阶段一无法开始） |

页面文案：「第 2/3 幕 · 第 4/9 章 · 已接收 1.2k 字」，已用时由蒙版统一追加。

## 失败与续跑

- 单幕失败：记录 `failures[{scope:"act", act_index, act_name, message}]`，其余幕继续；
- 单章失败：记录 `failures[{scope:"chapter", chapter_index, chapter_title, message}]`，其余章继续；
- 中间结果落盘 `<工程>/.ndw/outline_hier.json`（每完成一幕/一章即写）；
- `resume=true` 时复用中间结果：跳过已完成的幕/章，只补缺失部分，不重新规划幕；
- 页面在结束后列出失败项并询问是否「只重试失败项」，确认后带 `resume=true` 再跑一次。

## 预算

按层级分别估算，复用 `_tool_max_tokens`：

| 层级 | step | 估算依据 |
| --- | --- | --- |
| 幕列表 | `outline_acts` | 幕数 |
| 每幕章节 | `outline_chapters` | 该幕预计章数 |
| 每章节拍 | `outline_beats` | 每章节拍数 |

## 兼容性

- 原 `/api/ui/outline/generate` 与 `/api/ui/outline/generate/stream`、`service.generate_outline`
  （单轮 / 按幕多轮）保持不变；
- 未改 `setting-bible.schema.json`，组装结果仍走 `SettingsStore.new_act / new_outline_chapter / new_beat`；
- 章序号全局连续，复用同序号已有的章节 ID，已落地章节文件不受影响；
- 离线适配器新增 `emit_acts / emit_chapters / emit_beats` 分支，便于无模型时跑通。

## 涉及文件

- `src/novel_drama/writing/outline_hier.py`：三阶段编排、进度、失败记录、续跑、组装落盘；
- `src/novel_drama/writing/service.py`：新增三个工具 schema 与 `_gen_tool` 分支、预算档位；
- `src/novel_drama/adapters/offline_writing.py`：离线分层工具输出；
- `src/novel_drama/web/stream_api.py`：新增分层 SSE 路由；
- `src/novel_drama/web/static/core.js`：`progressApply` 支持 `unit=act/chapter`；
- `src/novel_drama/web/static/page-outline.js`：「生成大纲」切到分层接口，展示层级进度并可重试失败项。

## 验证

- 桩：3 幕 × 2 章 × 2 节拍；第二幕 `emit_chapters` 抛错 → 首次 4 章 / 8 节拍 + 1 条失败；
  `resume=true` 后只新增 1 次 `emit_chapters`、2 次 `emit_beats`，最终 6 章 / 12 节拍、0 失败；
  落盘经真实 setting-bible schema 校验通过，章序号 1..6 连续；
- 前端桩：`第 1/3 幕`、`第 2/3 幕 · 第 4/9 章 · 已接收 1.2k 字`，人物/批次旧文案回归通过；
- `python -m compileall -q src`、`node --check`（core.js / app.js / page-outline.js）通过；
- 按要求未启动服务、未做浏览器实测。
