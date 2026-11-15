# 大模型调用轮次梳理（只读）

> 范围：本仓库 `src/novel_drama` 中所有会向写作大模型（omlx，OpenAI 兼容 HTTP）发请求的功能路径。
> 方式：只读代码 + 只读配置；未修改业务代码、未重启服务。
> 定位约定：`路径:行号` 均指本仓库当前工作区文件；行号会随代码变动，引用时以函数名为准。
> 与并行开发相关的大纲文件（`writing/expand.py`、`web/stream_api.py` 中的扩写入口）只做只读引用。

**配置口径**：运行时配置按 `内置默认 ← config/config.yaml ← config/config.ui.yaml` 三层深合并（`src/novel_drama/config.py:285-324`、`141-155`）。本文“当前生效值”均按这三层合并后的结果描述；未在任一文件出现的键取代码默认值。

---

## 0. 结论速览

| 功能 | 请求轮次结构 | 正常一次操作的请求数 |
|---|---|---|
| 分层大纲（幕→章→节拍） | 严格串行三层：1 次幕 + 每幕 1 次章 + 每章 1 次节拍 | `1 + A + A×C` |
| 按幕多轮大纲（旧接口） | 每幕 1 次（只出章节）+ 每章 0~2 次节拍（正常 1 次） | `A + A×C`（不含重写） |
| 单轮大纲（旧接口） | 1 次生成整本 | 1 |
| 章节正文 | 单轮或按节拍分轮，串行 | `1` 或 `ceil(节拍数 / 每轮节拍数)`（受 `max_rounds` 保险） |
| 角色识别补跑 | 仅「节拍已存在但从未同步角色」的章节，每章 1 次（一次性） | 未同步章节数 |
| 章节摘要 / 全书滚动摘要 | 每章正文生成成功后 1 次 | `章数` |
| 正文续写 | 串行多轮，最多 3 轮 | `1..3` |
| 导演标注 | 按镜头分批，每批 1 次，串行 | `ceil(镜头数/8)` |
| 世界生成 | 按批，每批 1 次 | `ceil(条数/2)`，未给条数时 1 |
| 角色生成 | 整批 1 次或按批，每批 1 次 | 未给人数 1；指定 N 时 `ceil(N/5)` |
| 角色名单 + 逐个详情 | 1 次名单 + 每角色 1 次 | `1 + N` |
| 角色描述补全 | 每角色 1 次（只补缺失字段） | `待补角色数` |
| 音色描述 | 每角色 1 次（非流式工具调用），失败降级规则模板 | `角色数` |
| 素材（音效/配图/视频/BGM） | 正常链路 **不调用 LLM**；只有音效/BGM 的中文描述改写会调 | 0（改写路径：每条约 1 次） |

---

## 1. 公共调用层（所有模块共用）

### 1.1 适配器与请求体

- 适配器装配：`src/novel_drama/writing/service.py:93-121`（`build_writing_adapter`，`adapters.writing=omlx` 时实例化 `OmlxWritingAdapter`）。每个功能函数都会新建一个适配器实例（无共享连接池）。
- 统一请求结构：`src/novel_drama/adapters/base.py:93-106`（`TextGenRequest`：`task/system/messages/temperature/stop/stream/context_budget_tokens/metadata/prompt_cache_key`；`max_tokens` 字段已删除，由适配器按上下文剩余空间计算）。
- 请求体装配：`src/novel_drama/adapters/writing.py:967-1009`（`_build_payload`）。
- 结构化输出走工具调用：`src/novel_drama/adapters/writing.py:1123-1224`（`generate_tool`，强制 `tool_choice` 指定单个函数，模型参数即结果）。

请求体实际字段：

| 字段 | 取值来源 | 代码位置 |
|---|---|---|
| `model` | `omlx.model`（设置页写入 `config/config.ui.yaml`） | `writing.py:987` |
| `messages` | `req.system` + `req.messages` | `writing.py:977-981` |
| `max_tokens` | 见第 4 节 | `writing.py:982-989` |
| `temperature` | `req.temperature`（**恒有值**，默认 0.8） | `writing.py:990` |
| `stream` | `req.stream`（除润色/音色/音效/BGM 外均为 True） | `writing.py:991` |
| `stop` | `req.stop` 或 `omlx.stop` | `writing.py:993-995` |
| `chat_template_kwargs.enable_thinking` | `omlx.enable_thinking` | `writing.py:997-998`、工具调用覆盖 `1163-1165` |
| `reasoning_effort` | `omlx.reasoning_effort`（仅开启思考时） | `writing.py:1000-1001`、`1166-1167` |
| `cache_prompt` / `prompt_cache_key` | 多轮正文与续写显式设置 | `writing.py:1002-1005` |
| `tools` / `tool_choice` | 仅 `generate_tool` | `writing.py:1159-1160` |
| `seed` | 仅 `extra_body` 透传（见 1.4） | `writing.py:1006-1008` |

### 1.2 调用前的轻量校验（不产生生成轮次）

每次 `generate` / `generate_tool` / `stream` 前会做一次 `GET /v1/models` 校验模型存在，缓存 30 秒、探测超时 2 秒；失败不阻断真实请求。

- 代码：`src/novel_drama/adapters/writing.py:584-615`（`_check_model_available`）、`writing.py:93-94`（缓存 TTL）。
- 影响：请求计数中的“模型生成请求”不含它；但短时间连续调用时，每 30 秒仍可能多出 1 次 HTTP GET。

### 1.3 超时（当前生效值）

| 项 | 生效值 | 来源 | 代码位置 |
|---|---|---|---|
| 连接超时 | 5 秒 | `omlx.connect_timeout_s` | `writing.py:168`、`writing.py:70-72` |
| 首字超时（流式） | 300 秒 | `config/config.ui.yaml` | `writing.py:169`、`1552-1600` |
| 分片空闲超时（流式） | 60 秒（未在 ui 覆盖，取默认） | `writing.py:74-76` | `writing.py:1567-1598` |
| 非流式读超时 | `min(request_timeout_s=300, stream_idle_timeout_s=60)` → **60 秒** | 见 `_nonstream_timeout` | `writing.py:335-352` |
| 总时长上限 | 无（`total_timeout_s=null`） | `config/config.yaml` | `writing.py:172-176`、`1556-1583` |
| 工具调用显式超时 | 音色 90 秒（非流式，实际仍被 60 秒空闲超时压住） | `voice_design.py:213` | `writing.py:335-352` |

### 1.4 重试（传输层）

- 非流式：可重试错误（超时/限流/5xx/不可达）最多重试 `omlx.max_retries=2` 次，退避 `min(2×attempt, 5)` 秒；对应 `writing.py:420-459`。
- 流式：**只在尚未收到任何分片前**允许整体重试，最多 2 次；已开始输出后不再重试，避免重复生成；对应 `writing.py:1621-1644`。
- 结论：单次“逻辑调用”最坏会变成 `1+2=3` 次 HTTP 生成请求。

### 1.5 温度与 seed

- 所有 `TextGenRequest` 都带 `temperature`（dataclass 默认 0.8，`base.py:100`），适配器只用 `req.temperature`（`writing.py:990`）。因此 `omlx.temperature` 这个配置项在当前代码路径上**实际不生效**（仅当构造时显式传 `None` 才会回落配置，但现有调用点都不传 `None`）。
- 各调用点温度：大纲/正文/续写/世界/角色 = 0.8 或 0.7/0.8；润色 0.6；音色 0.6；音效/BGM 改写 0.2（见第 5 节表）。
- seed：默认**不固定**。只有 `generate_settings(..., seed=...)`、`generate_section(..., seed=...)` 会通过 `metadata["extra_body"]={"seed": ...}` 真正下发（`service.py:3229-3231`、`writing.py:1006-1008`）。其余调用点 `metadata["seed"]=work.id` 只供离线适配器做确定性复现，**不会**发给真实服务（代码可确认：`_build_payload` 只读取 `extra_body`）。

### 1.6 上下文长度控制与输出口径（总则）

1. **提示词裁剪（仅章节/续写/编辑）**：`ContextAssembler` 预算取 `omlx.context_window`（未知时 32768，按“1 汉字≈1 token”粗算）。超预算时按序裁剪：前文摘录 → 最近章摘要（先删最旧）→ 全书前情摘要 → 世界细节（先地点/术语，后硬规则/势力）→ 角色卡细节（先背景，后性格/说话方式）；故事圣经与任务指令永不被裁剪，仍放不下时报中文错误。代码：`src/novel_drama/text/context.py`（装配与裁剪循环）。
2. **适配器只在请求侧如实交出剩余空间**：所有请求用 `上下文上限 − (提示词字数+256)` 计算 `max_tokens`，不额外留余量；上下文上限取“检测缓存值”和 `omlx.context_window` 的较小值。窗口未知时不传 `max_tokens`，由模型自身停止；提示词已占满窗口时报中文错误（`AdapterError(INVALID_INPUT)`）。代码：`writing.py` 的 `_build_payload` / `_available_output_tokens`。
3. **输出侧没有人为上限**：不再按字数推导、不再有 `omlx.max_tokens` / 每轮预算 / 摘要预算 / 工具预算。`finish_reason=length` 视为异常（模型自身输出上限或上游异常），记录并提示，不当正常结束。

---

## 2. 各功能模块的轮次结构

### 2.1 分层大纲（幕 → 章 → 节拍）

入口：Web `POST /api/ui/outline/hierarchical/stream`（`src/novel_drama/web/stream_api.py:546-580`），当前页面生成大纲走的就是这一条（`web-next/js/pages/novel.js:1983`）。实现：`src/novel_drama/writing/outline_hier.py:414-938`。

**严格串行**，中间结果每完成一幕/一章落盘 `<工程>/.ndw/outline_hier.json`，可 `resume=True` 只补缺失项（`outline_hier.py:46-93`、`455-467`）。

| 阶段 | 次数 | 请求内容 | 代码位置 |
|---|---|---|---|
| 一、幕 | 1 次（一次出全量幕） | 作品上下文 + “规划 N 幕，每幕约 C 章”，工具 `emit_acts` | `outline_hier.py:469-500` |
| 二、章 | 每幕 1 次（共 A 次） | 作品上下文 + 本幕标题/概要 + “输出本幕 C 章” + 前序幕摘要 + 后续幕标题（只衔接） | `outline_hier.py:597-632` |
| 三、节拍 | 每章 1 次（共 A×C 次） | 作品上下文 + 本幕概要 + 本章摘要 + “一次补齐 B 个节拍” + 本幕其他章节标题/摘要 | `outline_hier.py:735-767` |

上下文注入（三阶段共用，`outline_hier.py:165-185`）：作品名、一句话故事（logline）、题材、可用人物 `id=姓名` 列表；不注入前文正文、不注入已有节拍全文。

闸门与重写（逐层独立，只重写本层）：

| 层 | 允许偏差 | 重写上限 | 最坏调用数 | 代码位置 |
|---|---|---|---|---|
| 幕 | ±1 幕 | 2 次 | 3 | `outline_hier.py:503-546` |
| 每幕章 | `_tolerance(章数)`（20%，至少 ±1） | 2 次 | 该幕 3 次 | `outline_hier.py:634-682`、`service.py:454-463` |
| 每章节拍 | `_tolerance(节拍数)`（20%，至少 ±1） | 2 次 | 该章 3 次 | `outline_hier.py:769-851` |

单幕/单章异常（非闸门）只记失败项并继续后续项，不中断整份大纲（`outline_hier.py:688-698`、`856-869`）。

### 2.2 按幕多轮大纲（已删除）

`generate_outline(multi_turn=True)`、`_generate_outline_acts`、`.ndw/outline_acts.json` 续跑文件与 CLI `--multi-turn / --acts-per-round` 已全部删除，不再保留兼容开关。
大纲生成现在只走三级分层路径（见 2.1）：一次规划全部幕 → 逐幕出章 → 逐章出节拍。

### 2.3 单轮大纲（`generate_outline`，CLI 兼容入口）

- 1 次 `emit_outline` 产出整本大纲，`max_tokens` 取「上下文窗口 − 已用提示词」（窗口未知时不传）；只做校验不重写（`service.py` 中 `generate_outline`）。
- 页面不再使用该入口，分层大纲统一走 `/api/ui/outline/hierarchical/stream`。

### 2.4 大纲扩写/续写（并行开发中，只读引用）

实现：`src/novel_drama/writing/expand.py:838+`，入口 `web/stream_api.py:582-611`。

- 幕：1 次 + 最多 2 次重写（`expand.py:417-490`，`REWRITE_LIMIT=2` 见 `expand.py:46`）。
- 每新增幕：1 次章节请求 + 最多 2 次重写（`expand.py:493-618`）。
- 每新增章：1 次节拍请求 + 最多 2 次重写（`expand.py:621-732`）。
- 上下文：作品上下文 + 插入位置说明（`placement`/锚点）+ 目标数量，不重写已有幕章（`expand.py:435-440`、`530-540`、`663-671`）。

### 2.5 章节正文生成

实现：`service.py` 的 `generate_chapter`、`_generate_chapter_rounds`；上下文装配 `build_chapter_context`。

- 单次操作生成**一章**，停止条件按**本章节拍**收口，不再按字数截断：
  - 每轮写一个或一组节拍（`beats_per_round`；CLI/页面可指定，0 或缺省时按 `ceil(节拍数 / max_rounds)` 自动分配）；
  - 每轮写完推进节拍指针，全部节拍写完即结束（`stop_reason=beats_done`）；
  - `max_rounds`（默认 `writing.multi_turn.max_rounds=8`）只作保险上限，提前用尽时 `stop_reason=max_rounds`，剩余节拍数见 `beats_remaining`；
  - 关闭分轮或只有一组节拍时走单轮（`stop_reason=single_round`）。
- 字数只作提示：任务指令写「篇幅参考约 N 字」，结果返回 `word_deviation{target,actual,ratio,message}`；偏差不触发停止或重写。
- 每轮 1 次请求，轮间串行；前缀（system、首条 user、temperature、stop）不变，仅追加 `assistant(已生成正文)` + 一句「继续写第 X–Y 个节拍…」（`_beats_round_instruction`），保证前缀缓存可复用。
- 某轮返回空正文直接抛 `AdapterError`（指出第几轮、哪几个节拍），不做假数据兜底或静默收尾。
- 请求输入：
  - system：故事圣经 + 世界设定 + 角色卡（只取本章 POV 与节拍涉及角色）+ 写作准则（`text/context.py`）。
  - user：**全书滚动摘要**（`.ndw/rolling_summary.json` 的 `book_summary`）→ **最近 N 章摘要**（`writing.context.recent_summaries` 默认 6；摘要缺失时回退正文末尾 800 字）→ 本章大纲与节拍 → 前文摘录（上一章结尾 800 字、本章已有正文结尾 800 字）→ 任务指令。
  - **不注入**全剧梗概（`story_digest` 为空，仅续写使用）。
- 上下文预算裁剪顺序：前文摘录 → 最近章摘要（先删最旧）→ 全书前情摘要 → 世界细节（先地点/术语，后硬规则/势力）→ 角色卡细节（先背景，后性格/说话方式）。
- `max_tokens`：不再设置每轮预算；请求取「上下文窗口 − 已用提示词」的剩余空间（窗口未知时不传），模型自然停止。
- 生成成功后追加 1 次摘要调用（`writing/recap.py:update_rolling_summary`）：产出本章摘要写回 `chapter.synopsis`，并把旧全书摘要与本章合并成新的 `book_summary` 持久化；摘要请求同样不设输出预算；摘要失败时正文照常落盘，但结果带 `summary_error`，CLI/页面明确提示。

### 2.6 正文续写

实现：`src/novel_drama/writing/continuation.py:484-774`。

- 串行多轮，`max_rounds` 默认 3（`writing.continue` 未配置，取默认；`continuation.py:42`、`546`）。
- 每轮新增目标 `words`（默认 800），本次总目标 = 该值，达到即停（`continuation.py:545-557`、`694-703`）。
- 每轮 1 次请求，轮间追加 `assistant(本轮新增)` + `user(继续写下一段，剩余 N 字)`（`continuation.py:603-629`）。
- 停止条件：空输出 / 新增 <50 字（视为复述，存为草稿）/ 达到总目标 / 达到 80% 且停在句末 / 达到硬上限 `max_total_words=3000` / 失败（`continuation.py:650-703`）。
- 单轮失败即停止，**不自动重试业务轮**（传输层仍按 1.4 重试）；已生成内容保留为草稿（`continuation.py:630-639`、`712-740`）。
- 请求输入：
  - system/user 同 2.5，额外注入「全剧梗概」（最多 600 字，不参与裁剪）与「最近原文」（默认 2000 字）作为 `assistant` 前缀（`continuation.py:559-580`、`237-267`）。
  - 任务指令包含：用户意图（最高优先级）、剩余节拍、衔接锚点（正文末尾 200 字）、“禁止重复”参考（最近 1000 字）、上一段结尾（`continuation.py:313-367`）。
  - 前情摘要取当前章之前最多 5 章（`DEFAULT_PREVIOUS_SUMMARIES=5`，`continuation.py:48`、`549`）。
- `max_tokens`：不再按字数收紧；请求取「上下文窗口 − 已用提示词」的剩余空间（窗口未知时不传），由模型自然停止。

### 2.7 导演标注（NDW 标注文本）

实现：`service.py:1785-2032`。

- 批次切分：把有台词的镜头按 `index` 升序拍平，每批 `script_scenes_per_round=8` 个，`批数 = ceil(镜头数/8)`；批间串行，每批 1 次请求（`service.py:1887-1915`、`1919-1990`）。
- **每批带哪些上文**（`service.py:1929-1940`）：
  1. 固定前缀：作品上下文（作品名/简介/题材/人物 id=姓名）+ 完整格式规范（@shot/!sfx/!visual/!anim/!bgm/!silence 规则）；
  2. 本批范围说明「本批镜头 N–M（全章共 S 个，编号沿用全局序号）」；
  3. 若 `start>0`：**前一个镜头**全文（标记“参考·”，只读不要输出）；
  4. 本批镜头全文（标题/地点/时间/情绪 + 逐行说话人与台词）；
  5. 若 `end<total`：**后一个镜头**全文（只读）；
  6. 任务说明：只标本批、保留台词原文与顺序、`@shot` 编号必须落在 N–M。
- 批与批之间**不累积**上文（只借相邻一个镜头），因此单批提示词长度基本恒定；跨批一致性靠全局镜头编号与旁白/说话人规则。
- 无批次内业务重试：失败批次记录后保留，`resume=True` 只重试失败/未完成批次（`service.py:1896-1921`、`1983-1990`）。
- `max_tokens`：不设人为上限；适配器按「上下文窗口 − 已用提示词」计算（窗口未知时不传）。若上游 `finish_reason=length` 按异常提示，不再当正常结束（`service.py` `_generate_validated`）。
- 标注文本解析（`parse_markup`）是本地规则解析，不再调用 LLM（`service.py:2006-2010`、`director/markup.py:447`）。

### 2.8 世界生成

实现：`service.py:2994-3137`。

- 条数范围 1..6；未给条数时 1 次请求，由模型决定条数；给了 N 且 `N>batch_size_worlds=2` 时按批，`批数=ceil(N/2)`（`service.py:3021-3029`、`3067-3113`）。
- 每批在固定前缀后追加「本批 N 条（第 i/n 批）」+ 已生成世界名单（防重名），前缀保持稳定（`service.py:3087-3092`）。
- 输入：作品/梗概/文风 + 已有世界（时代、名称）+ 已有世界库首套世界的总述/规则/势力/地点/术语名（`service.py:3047`、`2880-2947`）。
- 温度 0.7；工具 `emit_worlds`；无业务重试（`service.py:3049-3066`）。
- 世界封面等配图提示词由 `writing/worlds.py:418`（`build_art_prompt`）**规则拼接**，不调 LLM（`media/art.py:22`、`150`）。

### 2.9 角色生成与角色描述补全

三条路径：

1. **整批生成** `generate_characters`（`service.py:3265-3431`）
   - 未指定人数：1 次请求，`max_tokens` 取「上下文窗口 − 已用提示词」（不再按人数预留）；
   - 指定 N 且 `N>batch_size_characters=5`：`批数=ceil(N/5)`，每批追加人数与已生成姓名（`service.py:3341-3380`）；
   - 一次返回 `characters + stock_characters(龙套池) + narrator_profile(旁白音色)`（工具 schema `service.py:2579-2604`）；
   - 温度 0.8；无业务重试。
2. **名单 + 逐个详情**：`plan_characters` 1 次（`character_plan.py:142-256`），`generate_from_plan` 每个待生成角色 1 次（`character_plan.py:353-417`，请求 `384-399`）；单个失败只记录，继续下一个（`412-417`）。
3. **描述补全** `fill_character_descriptions`（`character_plan.py:475+`）
   - 默认只补缺失字段（`voice_description_zh/en`、`appearance_desc_zh/en`、`appearance_negative_zh/en`），每角色 1 次请求（`character_plan.py:559-595`）；
   - `overwrite=True` 时重新生成全部描述字段并覆盖；
   - 跳过：旁白、已齐全角色；失败只记录，不改其他人。
4. 单角色重写：`generate_section(section="character")` 1 次请求（`service.py:3805-3836`）。

角色提示词的公共上下文：作品/梗概/文风/世界（总述+规则+势力/地点/术语名）/已有角色名（最多 20 个）+ 人物描述规则 `_CHARACTER_DESC_RULES`（`service.py:2880-2947`、`2304-2307`）。

### 2.10 音色描述生成

实现：`src/novel_drama/tts/voice_design.py:193-221`（`build_profile`）。

- 每角色 1 次请求：非流式、工具调用 `emit_voice_profile`、`temperature=0.6`、`timeout_s=90`；输出侧不设人为上限。
- 输入：system 为拟音导演规则（`voice_design.py:74-84`），user 为角色摘要（姓名/定位/性别/年龄/性格/背景 600 字/语速/音区/口音，`voice_design.py:148-162`）。
- 失败或输出不合法 → **不再重试**，回退规则模板并记录 `source=rule`（`voice_design.py:216-221`、`165-190`）。
- 批量入口（音色补齐/选角）：每个角色首次生成时调用一次 `build_profile`（`tts/voice_fill.py:1112-1125`；另有 `voice_batch.py:479`、`cli_cast.py:81`、`web/app.py:4759`）。

### 2.10.1 角色识别补跑（cast-only）

实现：`src/novel_drama/writing/cast_capture.py`。

- 触发：正文生成/补写前，若设定库章节没有 `cast_synced` 且节拍未绑定任何现有角色，先发 1 次 `emit_cast`；已同步章节、旧版已绑定章节都不再调用。
- 输入：现有节拍摘要 + 龙套 + 现有角色表 + 统一的 `cast_prompt_block`；不重新生成节拍、不写正文。
- 落地：`normalize_cast` → `apply_chapter_cast`，与节拍生成同一套「第二次出现才建卡」语义；随后写 `cast_synced`、`cast_created/reused/extras/pending`。
- 反馈：识别到哪些角色、新建几张卡、谁进入待定、未识别到，都会进入结果 notices 与任务通知；识别为空也写标记，避免重复调用。
- 手动补跑：画本页「补角色识别」按钮（单章 / 整幕）走 `/api/ui/cast/capture/start`，只做角色识别、不写正文；开始前显示「本次将调用 N 次模型」与预估耗时，进度按 `第 k/N 章` 展示，逐章列出识别结果与失败原因，可停止（当前章识别结束后生效）。任务落在 `.ndw/cast_capture_job.json`，页面刷新后仍可读取。
- 待定队列：角色页展示 `.ndw/cast_pending.json`（姓名、首次出现章节、出现次数、章数、归属线索），可「建卡」或「丢弃」；建卡复用 `apply_chapter_cast` 的 `_new_character` 字段，丢弃不影响已建角色。
- 准确性：设定库章节 ID 与章节文件不一致时按 `index` 兜底匹配；生成弹窗通过 `/api/ui/cast/capture-plan` 提前显示未同步章节数与预估耗时，章节徽标与 `/api/ui/cast/status` 使用同一判定口径（`synced/bound/missing/absent`）。

### 2.11 设定与辅助（与大纲/角色相关，供计数参考）

| 功能 | 请求数 | 说明 | 代码位置 |
|---|---|---|---|
| 梗概 premise | 1 | 工具 `emit_premise`，温度 0.7 | `service.py:3178-3262` |
| 文风 style | 1~2 | 若 `visual_style` 为空追加 1 次定向补写 | `service.py:3245-3249`、`3140-3175` |
| 单字段润色 | 1 | 非流式，温度 0.6 | `service.py:3839-3913` |
| 章节反向总结 | `1` 或 `ceil(字数/3000)+1` | 超 3000 字先逐块提取要点，再合并 1 次 | `summarize.py:405-436`、`339-365` |
| 整章/选区编辑 | `ceil(字数/1200)` | 分块改写，每块 1 次 | `editing.py:286-344` |

### 2.12 素材（音效 / 配图 / 视频 / BGM）里的 LLM 介入

| 素材 | 正常链路 | 是否调 LLM | 代码位置 |
|---|---|---|---|
| 配图（世界封面/角色立绘/特写/全身） | 提示词由 `appearance_prompt` 等字段规则拼接 | **否** | `writing/worlds.py:418`、`media/art.py:150/184/254` |
| 视频段 | 提示词来自标注里的 `!visual`/`!anim` | **否** | `director/markup.py:447`（解析）、`media/generate.py:275+` |
| 音效 | 渲染时只给已存英文提示词补时长后缀 | **否**（`finalize_prompt`） | `media/sfx.py:172-180`、`sfx/prompting.py:330-346` |
| 音效（中文描述临时改写） | `rewrite_prompt`：LLM 优先、词典兜底 | 是，每条 1 次 | `sfx/prompting.py:349-401`、`291-327` |
| BGM | 标注已带 `prompt_en` 时直接用 | **否** | `director`/`media/music.py:736-753` |
| BGM（缺英文提示词时） | `bgm.prompting.rewrite_prompt` | 是，每条 1 次 | `media/music.py:739-753`、`bgm/prompting.py:272-320` |

音效/BGM 改写参数：`temperature=0.2`、`enable_thinking=False`、非流式、超时 `llm_timeout_s=20`（`sfx/prompting.py:268-288`、`bgm/prompting.py:214-233`）；输出侧不设人为上限。
说明：标注阶段已把中文+英文提示词一起产出，所以常规出素材链路**零 LLM 调用**；改写只是兜底/调试路径。

场景切分与台词切分是规则实现（`text/splitter.py:48`、`143`），不调 LLM。

### 2.13 统一模型任务提示（前端）

- 前端组件：`web-next/js/llm.js` 的 `confirmLlm()`；所有会调用文本大模型的入口在开始前调用它，统一展示「这次做什么 / 模型调用几次 / 预计耗时 / 是否开启思考 / 能否中途取消」。
- 事实来源：`GET /api/ui/llm/plan?path=&action=...`（`src/novel_drama/web/llm_plan.py`）。次数、阶段、思考与取消能力都由后端给出，前端不硬编码；次数只能给下限时返回 `at_least=true` 并在文案里写「至少 N 次」。
- 思考判定：复用 `writing/thinking.py` 的 `resolve_thinking()`，按动作涉及的阶段分别解析；多个阶段时输出「开启 / 关闭 / 部分步骤开启」。
- 已接入动作：设定生成/润色、世界设定、分层大纲与扩充、单章正文（含压缩/重写/补写）、续写、整幕生成、角色名单生成、角色档案补全、角色识别补跑、一致性审校、角色状态回流、导演标注、音色描述、音效提示词、全链路编排。
- 规则实现、不弹提示：模型状态检查、角色/音色绑定检查、场景与台词切分、导出前体检、用字统计、待定角色建卡/丢弃、章节角色识别状态读取。
- 取消：进度面板的「停止」对正文/续写/角色生成/标注走 `AbortController` 断开流，服务端在下一个增量处中断并保留已完成轮次；整幕生成沿用任务取消接口；不支持中断的动作在提示里明确写「不能中途取消」，不会显示假停止按钮。

---

## 3. 每次请求的输入组成（按模块）

| 模块 | system 主要部分 | 注入的上下文 | 是否带前文正文 |
|---|---|---|---|
| 分层大纲-幕 | “结构编辑，只按工具参数输出幕结构” | 作品名、logline、题材、人物 id=姓名、幕数/每幕章数 | 否 |
| 分层大纲-章 | 同上（章节列表） | 共同上下文 + 本幕标题/概要 + 前序幕摘要 + 后续幕标题 | 否 |
| 分层大纲-节拍 | 同上（节拍） | 共同上下文 + 本幕概要 + 本章摘要 + 同幕其他章标题/摘要 | 否 |
| ~~按幕多轮大纲~~（已删除） | — | — | — |
| 章节正文 | 故事圣经 + 世界设定 + 角色卡（本章相关）+ 写作准则 | 全书滚动摘要 + 最近 6 章摘要 + 本章大纲/节拍 + 上一章结尾 800 字 + 本章已有正文结尾 800 字 + 任务指令 | 是（摘录） |
| 正文续写 | 同上 | 全剧梗概（≤600 字）+ 前情摘要（≤5 章）+ 本章大纲/节拍 + 最近原文（默认 2000 字，作为 assistant 前缀）+ 锚点/禁止重复/意图 | 是（最近原文） |
| 导演标注 | 有声剧导演 + 只标本批镜头 | 作品上下文 + 格式规范 + 前一个镜头 + 本批镜头 + 后一个镜头 | 否（用镜头/台词代替） |
| 世界生成 | 世界观编辑 + 只按工具输出 | 作品/梗概/文风 + 已有世界（名称/时代）+ 首套世界总述与分类名 + 本批人数与已生成名单 | 否 |
| 角色生成 | 人物编辑 + 只按工具输出 | 作品/梗概/文风 + 世界摘要 + 已有角色名 + 本批人数与已生成姓名 | 否 |
| 角色描述补全 | 人物编辑（只输出描述字段） | 项目上下文 + 该角色已有档案 JSON + 缺失字段清单 | 否 |
| 音色描述 | 拟音导演规则 | 角色摘要（姓名/定位/性别/年龄/性格/背景 600 字/语速/音区/口音） | 否 |
| 反向总结 | 结构编辑（反推大纲） | 作品/文风/主要角色 + 当前标题/大纲/节拍 + 正文（或分块要点） | 是（全文或要点） |

---

## 4. max_tokens 如何确定

### 4.1 统一规则（所有模块）

输出侧没有人为上限，也不再有按字数/条目数的估算：

```
prompt_tokens = 所有 messages 字符数之和 + 256
context_limit = min(检测到的窗口, omlx.context_window)   # 未知时为 None

有窗口：max_tokens = context_limit − prompt_tokens        # 不额外留人为余量
无窗口：请求体不带 max_tokens
prompt_tokens >= context_limit：抛 AdapterError(INVALID_INPUT)，提示缩短输入
```

- 代码：`writing.py` 的 `_available_output_tokens` / `_build_payload`。
- 旧设置项（`omlx.max_tokens`、每轮预算、摘要预算、工具预算）全部删除；`TextGenRequest.max_tokens` 字段也已移除。
- 「要写多少字」只由提示词里的目标字数与每轮节拍数决定；`finish_reason=length` 视为异常并提示。

---

## 5. 参数一览

| 模块 | 温度 | 流式 | 思考 | max_tokens | 代码 |
|---|---|---|---|---|---|
| 分层大纲（三阶段） | 0.8（默认） | 是 | 系统自动开启（幕/章/节拍） | 上下文剩余空间 | `outline_hier.py:475-490`、`609-625`、`749-767` |
| 按幕/单轮大纲 | 0.8 | 是 | 开启（系统自动） | 上下文剩余空间 | `service.py:197-213`、`939-943` |
| 章节正文 | 0.8 | 是（页面）/ 否（CLI） | 关闭（系统自动） | 上下文剩余空间 | `service.py:generate_chapter` |
| 章节摘要 / 滚动摘要 | 0.3 | 否 | 关闭（系统自动） | 上下文剩余空间 | `writing/recap.py` |
| 正文续写 | 0.8 | 是（页面）/ 否（CLI） | 关闭（系统自动） | 上下文剩余空间 | `continuation.py:579-584` |
| 导演标注 | 0.8 | 是 | 关闭（系统自动） | 上下文剩余空间 | `service.py:1941-1948` |
| 世界生成 | 0.7 | 是 | 开启（系统自动） | 上下文剩余空间 | `service.py:3049-3066` |
| 角色生成/名单/补全 | 0.8 / 0.7 / 0.7 | 是 | 开启（系统自动） | 上下文剩余空间 | `service.py:3318-3334`、`character_plan.py:201-215`、`578-595` |
| 梗概/文风 | 0.7 | 是 | 开启（系统自动） | 上下文剩余空间 | `service.py:3232-3240` |
| 润色 | 0.6 | 否 | 关闭（系统自动） | 上下文剩余空间 | `service.py:3884-3897` |
| 音色描述 | 0.6 | 否 | 关闭（系统自动） | 上下文剩余空间 | `voice_design.py:203-213` |
| 音效改写 | 0.2 | 否 | 关闭（系统自动） | 上下文剩余空间 | `sfx/prompting.py:278-316` |
| BGM 改写 | 0.2 | 否 | 关闭（系统自动） | 上下文剩余空间 | `bgm/prompting.py:224-260` |

- seed：默认不固定；仅 setting/section 显式传参时下发（见 1.5）。
- 思考：全局总开关关闭时全部关闭；开启时按调用点三态（系统自动 / 开 / 关）解析，设置页「思考」分组可逐项覆盖。强度统一跟随全局，不按阶段分档。日志字段：`task / stage / enabled / source / effort`。
- 清单外补充：`script`（导演标注）实际会发起请求，已按默认关闭列入；`outline_extract`（正文反推大纲）同理。

---

## 6. 重试与重写：什么情况触发、几次、额外多少请求

### 6.1 传输层（适用于所有模块）

| 条件 | 行为 | 额外请求 |
|---|---|---|
| 超时 / 限流 / 5xx / 不可达 | 非流式可重试；流式仅首片前可重试 |最多 +2 |

代码：`writing.py:420-459`、`1621-1644`、`max_retries=2`（`config.yaml`）。

### 6.2 业务层（逐层闸门）

| 模块 | 触发条件 | 重写次数 | 最坏单次操作请求数 | 代码 |
|---|---|---|---|---|
| 分层大纲-幕 | 幕数与设定差 >1 | 2 | 3 | `outline_hier.py:503-546` |
| 分层大纲-章 | 该幕章数超出 ±20%（≥±1） | 2 | 该幕 3 | `outline_hier.py:634-682` |
| 分层大纲-节拍 | 该章节拍数超出 ±20%（≥±1） | 2 | 该章 3 | `outline_hier.py:769-851` |
| 大纲扩写 | 每层数量越界 | 2 | 每层 3 | `expand.py:417-490`、`493-618`、`621-732` |
| 导演标注 | 批次失败 | 无自动重试 | 0（可 `resume`） | `service.py:1967-1990` |
| 世界/角色/角色补全 | 请求失败 | 无自动重试（单角色失败跳过） | 0 | `service.py:3093-3121`、`412-417` |
| 章节正文 | 单轮失败 | 无业务重写 | 0 | `service.py:1609-1656` |
| 正文续写 | 轮失败 | 停止并保留草稿 | 0 | `continuation.py:630-639` |
| 音色描述 | 失败/不合法 | 不重试，降级模板 | 0 | `voice_design.py:216-221` |
| 反向总结 | 失败 | 报错不写盘 | 0 | `summarize.py:437-448` |

> 闸门失败是“停在该层并保留已完成内容”，不是无限重试（`retryable=True` 仅表示可手动续跑）。

---

## 7. 可计算的请求总数公式

记号：`A`=幕数，`C`=每幕章数，`B`=每章节拍数，`S`=有台词的镜头数，`W`=世界条数，`N`=角色数，`L`=单章目标字数，`seg=800`，`max_rounds=8`。

### 7.1 大纲

- 分层大纲（当前页面路径）：
  ```
  R_outline = 1 + A + A×C
  最坏       = 3 + 3A + 3A×C          （每层都用满 2 次重写）
  期望       = 1 + A(1+2p_c) + A×C(1+2p_b) + 2p_a
  ```
  （`p_a/p_c/p_b` 为各层首次不达标的概率，代码无法给出，需实测统计。）
- 按幕多轮（旧接口）：已删除，不再计入。
- 单轮：`R = 1`（最坏 3，仅传输层重试）。

**示例 1：10 幕 × 5 章 × 4 节拍**

| 路径 | 正常 | 最坏 |
|---|---|---|
| 分层大纲 | `1+10+50 = 61` 次 | `3+30+150 = 183` 次 |
| 单轮 | 1 次 | 3 次 |

### 7.2 章节正文

```
每轮节拍数 = 用户指定；缺省 = ceil(节拍数 / max_rounds)
每章轮数   = ceil(节拍数 / 每轮节拍数)        且不超过 max_rounds（默认 8）
R_text     = 章数 × 每章轮数
摘要       = 章数（每章生成成功后 1 次）
```

**示例 2：全书 10 幕、每幕 3 章、每章 4 个节拍**

- 30 章，`max_rounds=8` → 每轮 1 个节拍 → 每章 4 轮 → **约 120 次**；另加每章 1 次摘要 → **约 150 次**。
- 若把 `beats_per_round` 设为 2 → 每章 2 轮 → **约 60 次**。
- 轮数不再由「目标字数 ÷ 每段字数」决定；字数只影响 `word_deviation` 提示。

### 7.3 其他模块

| 模块 | 公式 | 示例 |
|---|---|---|
| 导演标注 | `ceil(S / 8)` | 40 个镜头 → 5 次 |
| 世界生成 | 未给条数：1；给 `W`：`ceil(W/2)` | 6 条 → 3 次 |
| 角色整批 | 未给人数：1；给 `N`：`ceil(N/5)` | 12 人 → 3 次 |
| 角色名单模式 | `1 + N待生成` | 12 人 → 13 次 |
| 角色描述补全 | `待补角色数`（旁白与已齐全跳过） | 8 人缺字段 → 8 次 |
| 音色描述 | `角色数`（失败不重试，降级模板） | 8 人 → 8 次 |
| 章节摘要 | `1`（≤3000 字）或 `ceil(L/3000)+1` | 3000 字 → 1 次 |

---

## 8. 标注/分轮的调用与上文关系

| 场景 | 批次如何切分 | 每批/每轮带的上文 | 跨批累积 | 代码 |
|---|---|---|---|---|
| 导演标注 | 有台词镜头按 `index` 升序，每 8 个一批 | 固定项目上下文 + 格式规范 + 前一个镜头（只读）+ 本批镜头 + 后一个镜头（只读） | 否（只借相邻 1 个镜头） | `service.py:1887-1940` |
| 世界分批 | 每 2 条一批，`ceil(W/2)` 批 | 固定项目上下文 + 本批条数/批次 + 已生成世界名称 | 仅名称列表 | `service.py:3087-3092` |
| 角色分批 | 每 5 人一批 | 固定项目上下文 + 本批人数/批次 + 已生成姓名 | 仅姓名列表 | `service.py:3350-3357` |
| 章节正文分轮 | 每轮一组节拍（用户指定或按节拍数自动分配） | 固定 system/首条 user + 已生成正文（assistant）+ 本轮节拍范围与篇幅参考 | 累积全文 | `service.py:_generate_chapter_rounds` |
| 章节摘要 / 滚动摘要 | 每章 1 次 | 旧全书摘要 + 本章标题/大纲 + 本章正文（开头 3000 + 结尾 9000 字） | 合并进新全书摘要 | `writing/recap.py` |
| 正文续写多轮 | 每轮 800 字目标 | 固定 system/user（含最近 2000 字原文作为 assistant）+ 本轮已生成 + 剩余额度指令 | 累积本轮新增 | `continuation.py:603-629` |
| 反向总结分块 | 每 3000 字一块 | 固定块提取 system + 本块正文；最后合并请求带上所有块要点 | 要点汇总 | `summarize.py:413-433` |

---

## 9. 待确认项（代码无法单独确认，勿当结论）

1. **思考 token 的实际计入方式**：代码注释说明 reasoning tokens 占用 `max_tokens`（`writing.py:77`），但服务端（omlx/vLLM 实现）实际如何计入、以及 `reasoning_effort=low` 的真实思考量，未在本机验证。
2. **前缀缓存是否真实命中**：`cache_prompt`/`prompt_cache_key` 只是透传（`writing.py:1002-1005`），服务端可忽略；命中率与实际节省无法从代码确认。
3. **各层重写触发概率**：`p_a/p_c/p_b` 需实际生成统计，代码只能给出上限。
4. **并发**：写作请求没有客户端串行锁，`omlx.max_concurrency` 只是配置项（`config.py:41`、`web/models_api.py:810`）；页面同时触发多个生成任务时是否会并行打服务端，本次未验证。
5. **素材模板内部**：ComfyUI 模板（`config/comfy/*.json`）是否会额外拼接提示词，未逐模板核对；当前证据只到 `media/art.py`/`media/music.py`/`media/sfx.py` 这一层。
6. **“全书 10 幕”口径**：总章数取决于“每幕章数”参数（页面默认 3、生成弹窗默认 4 节拍/3 章），公式中已参数化 `C`，具体数值需按实际设定确认。
7. **CLI 与页面流式差异**：CLI 无 `on_delta` 时走 `adapter.generate`（非流式），页面走 `stream_text`/SSE；两者请求轮次相同，但超时口径不同（非流式受 60 秒空闲超时约束，见 1.3）。

---

## 10. 代码位置索引

| 主题 | 文件:行 |
|---|---|
| 请求结构与默认参数 | `src/novel_drama/adapters/base.py:93-106` |
| 适配器装配 | `src/novel_drama/writing/service.py:93-121` |
| 请求体与参数下发 | `src/novel_drama/adapters/writing.py:967-1009` |
| 上下文上限 / 请求 max_tokens | `src/novel_drama/adapters/writing.py` 的 `_available_output_tokens` / `_build_payload` |
| 工具调用与思考下发 | `src/novel_drama/adapters/writing.py` 的 `generate_tool` |
| 传输层重试（非流式/流式） | `src/novel_drama/adapters/writing.py:420-459`、`1621-1644` |
| 流式超时判定 | `src/novel_drama/adapters/writing.py:1552-1600` |
| 上下文状态接口 | `src/novel_drama/web/models_api.py` 的 `GET /api/ui/config/generation` |
| 分层大纲三阶段与闸门 | `src/novel_drama/writing/outline_hier.py:414-938` |
| ~~按幕多轮大纲~~ | 已删除，仅保留三级分层（`outline_hier.py`） |
| 大纲扩写（并行开发） | `src/novel_drama/writing/expand.py:417-732`、`838+` |
| 章节上下文装配 | `src/novel_drama/writing/service.py:build_chapter_context` |
| 章节正文生成（按节拍分轮） | `src/novel_drama/writing/service.py:generate_chapter`、`_generate_chapter_rounds` |
| 章节摘要 / 全书滚动摘要 | `src/novel_drama/writing/recap.py:update_rolling_summary` |
| 章节版本历史（存档/恢复） | `src/novel_drama/store/project_store.py:save_chapter`、`list_chapter_versions`、`load_chapter_version`；`src/novel_drama/writing/editing.py:restore_chapter_version` |
| 上下文裁剪算法 | `src/novel_drama/text/context.py:400-510` |
| 正文续写 | `src/novel_drama/writing/continuation.py:313-367`、`484-774` |
| 导演标注批次与提示词 | `src/novel_drama/writing/service.py:1663-1685`、`1785-2032` |
| 世界生成 | `src/novel_drama/writing/service.py:2994-3137` |
| 角色生成 / 名单 / 补全 | `src/novel_drama/writing/service.py:3265-3431`、`src/novel_drama/writing/character_plan.py:142-439`、`475+` |
| 音色描述 | `src/novel_drama/tts/voice_design.py:193-221` |
| 设定 / 润色 | `src/novel_drama/writing/service.py:3140-3262`、`3839-3913` |
| 反向总结 | `src/novel_drama/writing/summarize.py:119-177`、`339-471` |
| 音效/BGM 描述改写 | `src/novel_drama/sfx/prompting.py:268-401`、`src/novel_drama/bgm/prompting.py:214-320` |
| 素材渲染（无 LLM） | `src/novel_drama/media/sfx.py:172-180`、`media/music.py:736-753`、`media/art.py:150-254` |
| 场景/台词切分（规则） | `src/novel_drama/text/splitter.py:48`、`143` |
