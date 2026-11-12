# LLM 调用模式梳理（基于代码事实）

本文只做阅读与整理：所有结论都标注了源码位置，未调用任何模型服务，也没有改动生产代码。
文中「单次」指一次 HTTP 请求即结束，「多轮」指同一业务动作内发起多次**独立**请求（后一轮带上前几轮产出），
而不是 OpenAI 的「工具结果回灌再请求」循环——后者在本仓库中不存在（见 §4）。

## 1. 结论速览

| 业务调用点 | 形态 | 轮数 / 批量 | 关键参数 | 失败处理 |
| --- | --- | --- | --- | --- |
| 大纲生成（整本） | 单次或按幕多轮 | 1 轮，或「幕数」轮（每轮 1 幕） | 工具调用；temp 默认 0.8；预算按节拍数估算（上限 16384） | 任一轮失败即整体报错 |
| 大纲续写 | 按幕多轮 | 新增幕数轮，逐幕落盘 | 工具调用 `emit_act`；temp 0.8；预算按本幕节拍数估算 | 失败即停，保留已生成幕 |
| 世界设定（批量） | 单次或按批多轮 | 1 轮，或 `ceil(count/batch_size_worlds)` 轮 | 工具调用 `emit_worlds`；temp 0.7；预算 1800/条 + 固定开销 | 任一批失败即整体报错 |
| 世界 / 梗概 / 文风（分区） | 单次 | 1 轮 | 工具调用 `emit_world/emit_premise/emit_style`；temp 0.7 | 直接报错 |
| 单条世界 / 单个人物（区块生成） | 单次 | 1 轮 | 工具调用；temp 0.7 / 0.8；world 约 6.3k、character 约 6.9k | 直接报错 |
| 人物生成 | 单次或按批多轮 | 1 轮，或 `ceil(count/batch_size_characters)` 轮 | 工具调用 `emit_characters`；temp 0.8；预算 1600/人 + 2600 固定（5 人约 14.7k，上限 16k） | 任一批失败即整体报错 |
| 章节正文 | 单次或多轮续写 | 默认 ≤ 8 轮（目标字数 > 1200 时自动开） | 非工具、纯文本；temp 0.8；单轮 max_tokens 2048 | 空正文报错；多轮靠停止条件退出 |
| 正文续写 | 多轮续写 | 默认 ≤ 3 轮，新增 ≤ 3000 字 | 非工具；temp 0.8；max_tokens 2048 | 失败即停，保存未应用草稿 |
| 章节改写（LLM 编辑） | 单次或分块多轮 | 1 轮，或按块数（块阈值 1200 字） | 非工具；temp 0.8；max_tokens 2048 | 任一块失败即中止，正文不变 |
| 章节反向总结 | 单次或「分块 + 合并」 | 1 轮，或 `块数 + 1` 轮 | 非工具；temp 0.8；max_tokens 2048 | 任一步失败即不写盘 |
| 导演标注 | 单次或按批多轮 | 1 轮，或 `ceil(镜头数/8)` 轮 | 非工具；temp 0.8；max_tokens 2048 | 任一批异常即整体失败 |
| 音色描述生成 | 单次 | 1 轮 | 工具调用 `emit_voice_profile`；temp 0.6；超时 90s | 任意失败回退规则模板 |
| 音效 / BGM 英文改写 | 单次 | 1 轮 | 非工具；temp 0.2；max_tokens 220 / 200；超时 20s | `auto` 降级词典；`llm` 直接报错 |
| 字段润色 | 单次 | 1 轮 | 非工具（要求 JSON 文本）；temp 0.6；max_tokens 2048 | 解析失败按纯文本接受，空内容报错 |

所有业务调用点都走**非流式**（`stream=false`）。适配器虽实现了 SSE 流式接口，但生产代码中没有任何调用点使用（`grep ".stream("` 无业务命中）。

## 2. 适配层

### 2.1 位置与协议

- 唯一实现：[writing.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L33-L52) 中的 `OmlxWritingAdapter`，OpenAI 兼容 HTTP（`/v1/chat/completions`、`/v1/models`、`/v1/models/{id}/unload`），Bearer 鉴权，`urllib.request` 直连，无 SDK 依赖。
- 统一请求结构：[base.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/base.py#L92-L106) 的 `TextGenRequest`，默认值 `max_tokens=0`（沿用配置）、`temperature=0.8`、`stream=True`（字段存在但业务不读）、`context_budget_tokens=32768`。
- 装配入口有两个，行为不同：
  - [service.py `build_writing_adapter`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L64-L85)：`omlx` 分支只传 `config.get("omlx")`，**不合并工程级 `settings.writing`**。
  - [adapters/__init__.py `build_adapters`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/__init__.py#L37-L68)：会把工程设置里非空的 `writing.*` 合并进 `omlx`（音色设计 CLI/页面走这条）。
- 兜底：[offline_writing.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/offline_writing.py#L126-L212) 的 `OfflineWritingAdapter`，按 `task` 走本地规则；它的 `generate_tool` 是「先跑规则生成 JSON，再按工具名做形状适配」，不是真实 function calling。

### 2.2 请求体构造

[writing.py `_build_payload`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L572-L607)：

```python
payload = {
    "model": self.model,
    "messages": messages,
    "max_tokens": int(req.max_tokens or self.settings.get("max_tokens") or 2048),
    "temperature": float(req.temperature if req.temperature is not None else self.settings.get("temperature", 0.8)),
    "stream": stream,
}
```

- `max_tokens`：请求值优先；`TextGenRequest.max_tokens` 默认 0，表示沿用配置 `omlx.max_tokens`（再回落到 2048），因此设置页的「最大输出」对未显式传参的调用生效；工具调用由业务层取 `max(配置值, 条目估算)`。
- 上下文守卫：`_build_payload` 会估算 prompt tokens，`prompt + max_tokens` 超过上下文上限时自动下调 max_tokens；连 256 输出都放不下时返回中文错误「输入超过模型上下文上限」。
- 上限探测：设置页 `/api/ui/settings/model-limits` 只读 OpenAI 兼容接口（`/v1/models` → `/v1/models/{id}` → `/v1/models/status` → `/props` 等），来源只标 server / unknown，不读本机文件、不伪造默认值。
- `temperature`：请求值优先；`TextGenRequest.temperature` 默认 0.8 且永不为 `None`，所以 `omlx.temperature` 对业务调用同样不生效。
- 工具调用额外抬预算：[`generate_tool`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L699-L703) 把 `max_tokens` 抬到 `max(当前值, 4096)`（`TOOL_MIN_MAX_TOKENS`），注释明确说明 reasoning tokens 也计入该预算。
- `stop` / `prompt_cache_key`：`stop` 来自请求或 `omlx.stop`；`prompt_cache_key` 非空且 `omlx.cache_prompt=true` 时才加 `cache_prompt: true` 与 `prompt_cache_key`（多轮正文/续写/编辑在用）。
- `metadata.extra_body`：若调用方在 metadata 里塞了 dict，会整体 `payload.update(extra_body)`——这是一个预留的覆盖口，但当前**没有任何调用点使用**。

### 2.3 超时与重试

- 默认超时 300s，连接超时 5s（[writing.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L26-L37)），可由 `omlx.request_timeout_s` / `omlx.connect_timeout_s` 覆盖；单次调用可覆盖（音色描述 90s、音效/BGM 改写 20s）。
- HTTP 重试在 [`_request_json`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L180-L209)：仅对 `retryable=True` 的错误（429、5xx、超时、不可达）重试，次数上限 `omlx.max_retries`（默认 2），退避 `sleep(min(2.0*attempt, 5.0))`（即 2s、4s）。
- 非 JSON 响应、401/403/404、空 `choices`、未返回 `tool_calls`、`arguments` 非法——都直接抛 `AdapterError`，**不重试、不自动纠错、不二次追问**。
- 流式接口的建连重试是另一套循环（[`stream`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L472-L482)），但业务未使用。

## 3. 单次调用 vs 多轮

本仓库的「多轮」全部是**业务层自己发起的多次独立请求**，共同点：固定前缀（system + 首条 user + temperature/stop）不变，只在末尾追加本轮信息，以便复用前缀缓存。

### 3.1 单次即结束

- `generate_settings`（world/premise/style）
- `generate_section` 的 premise/style 分支（内部调用 `generate_settings`）；world/character 分支各 1 轮
- `polish_field`
- 音色描述 `build_profile`
- 音效 / BGM 英文改写
- `generate_outline(multi_turn=False)`（默认）
- `generate_worlds` / `generate_characters` 在数量不超过单批上限时

### 3.2 业务级多轮

| 场景 | 轮数决定 | 每轮带什么历史 | 终止条件 |
| --- | --- | --- | --- |
| 章节正文多轮 | `ceil(target_words / segment_words)`，上限 `writing.multi_turn.max_rounds`（默认 8） | 前缀 + 每轮追加「assistant: 已生成全文」+「user: 续写指令」 | 空输出 / 本轮新增 < 50 字 / 总字数 ≥ 目标 ×1.1 / 到达轮数上限 |
| 正文续写 | `writing.continue.max_rounds`（代码默认 3） | 固定前缀 + 最近原文（默认 2000 字）+ 每轮追加已新增文本与短指令 | 空输出 / 新增 < 50 字 / 累计 ≥ `min(seg*rounds, max_total_words=3000)` / 达到目标 ×1.1 / 停在自然段边界 |
| 大纲按幕 | 幕数（每轮 1 幕） | 固定前缀 + 本轮第 N 幕要求 + 已完成幕的「标题+概要」 | 全部幕完成 |
| 大纲续写 | 新增幕数 | 固定前缀 + 本轮幕要求 + 最近 8 条已完成幕摘要 | 全部完成，或某幕失败即 break（保留已生成） |
| 世界批量 | `ceil(count / batch_size_worlds=2)` | 固定前缀 + 本批条数 + 已生成名称 | 达到目标条数 / 批次用尽 |
| 人物批量 | `ceil(count / batch_size_characters=5)` | 固定前缀 + 本批条数 + 已生成姓名 | 同上 |
| 导演标注批量 | `ceil(场景数 / script_scenes_per_round=8)` | 固定前缀 + 本批镜头台词 + **上一批输出末尾 200 字** | 批次用尽 |
| 章节改写分块 | 块数（阈值 1200 字） | 固定前缀 + 本块正文 + 前后各 200 字 | 批次用尽 |
| 章节反向总结分块 | 块数 + 1 | 每块只带该块正文；最后 1 轮带各块要点 | 所有块完成 + 合并 1 次 |

注意：多轮的**历史是全量累积**（章节正文/续写把已生成正文整段塞进 assistant 消息），没有按 token 再裁剪；`context_budget_tokens` 只作用于首轮的 `ContextAssembler` 装配。

### 3.3 不存在的形态

- 没有「工具调用 → 回灌 tool 结果 → 再请求」的循环。
- 没有「校验失败 → 自动重发提示要求修正」的循环。
- 没有并发多路请求（多轮/多批都是串行 for 循环）。

## 4. 工具调用（function calling）

- 入口：[`generate_tool`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L672-L768)，一次请求内置 `tools=[tool]` 且 `tool_choice` 强制指定该函数。
- 一轮只取**第一个** `tool_calls[0].function.arguments`；同一响应里若有多个调用，其余被忽略。
- 参数必须是 JSON 对象：dict 直接用；字符串则 `json.loads`，失败或不是对象都抛 `UPSTREAM_ERROR`。
- 输出预算：适配器保证不低于 4096（`TOOL_MIN_MAX_TOKENS`）；业务层再用 [`_tool_max_tokens`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L50-L73) 按「固定开销 + 条目数」估算（人物 1600/条、世界 1800/条、大纲/单幕 2600/2200 每节拍），最终取 `max(配置值 omlx.max_tokens, 估算值)`；估算上限 16384、配置兜底上限 32768。
- 思考模式：`generate_tool` 默认强制 `enable_thinking=false`（结构化输出优先保证参数完整），调用方可传 `metadata.enable_thinking` 显式覆盖。
- 截断：`finish_reason=length` 且没有 `tool_calls` 时返回中文错误「模型输出被截断…可重试，或减少本次生成数量后分批生成」，`retryable=True`，detail 保留 `finish_reason`、工具名、prompt/completion tokens 与响应片段；参数 JSON 被截断时给同样提示。
- 失败策略：除此之外不重试、不追问、不降级到文本 JSON 解析（项目记忆里的「不再保留文本 JSON 兜底」与代码一致）。
- 工具定义集中在 [service.py `_gen_tool`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L1199-L1280)：`emit_outline` / `emit_act` / `emit_premise` / `emit_style` / `emit_world` / `emit_worlds` / `emit_character` / `emit_characters`；音色描述用 [voice_design.py `_PROFILE_TOOL`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/tts/voice_design.py#L155-L165)。
- 结构化结果之后仍要做业务清洗与校验（长度上限、枚举归一、重名、至少 1 名主角等），校验失败直接抛中文错误，**不会重新请求模型**。

## 5. 思考模式（reasoning / thinking）

- 开关只有一个：`omlx.enable_thinking`，默认 `false`（[config.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/config.py#L22-L40)、[config/config.yaml](file:///Users/zhaohaochen/git/novel-drama-workbench/config/config.yaml#L24-L46)），设置页可改（[web/app.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/web/app.py#L80)）。
- 表达方式：只要该键不是 `None`，每次请求都带 `chat_template_kwargs: {"enable_thinking": bool(...)}`（[writing.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L598-L600)）。服务端不认这个字段时会被忽略。
- 显式关闭的两个调用点：音效改写与 BGM 改写各自构造 omlx 设置时写死 `enable_thinking=False`（[sfx/prompting.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/sfx/prompting.py#L278-L288)、[bgm/prompting.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/bgm/prompting.py#L224-L233)），并在同一处把温度压到 0.2、max_tokens 压到 220/200。
- 其余调用点（service / continuation / summarize / editing / voice_design）全部**继承全局配置**，没有单独打开 thinking 的地方；唯一的例外是工具调用：`generate_tool` 无论全局开关如何都默认按 `enable_thinking=false` 下发，可用 `metadata.enable_thinking` 覆盖。
- 响应侧：`generate` 在 `content` 为空时回退 `reasoning_content`（[writing.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L663)），流式同理（同文件 L818）。开启 thinking 且输出被 `max_tokens` 截断时，存在把推理文本当正文返回的路径。

## 6. 业务调用点逐项

### 6.1 大纲（`generate_outline`）

- 入口 [service.py L104-L237](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L104-L237)。
- 单轮：`_generate_json(adapter, req, "outline")`，工具 `emit_outline`，一次产出全部幕/章/节拍。
- 多轮：`multi_turn=True` 时走 [`_generate_outline_acts`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L241-L292)，每轮工具 `emit_act`，已生成幕只回传「标题 + 概要」；CLI 用 `--multi-turn` 打开，页面 `multi_turn` 为真才打开，默认关闭。
- 参数：`temperature` 未显式设置 → 0.8；输出预算按 `acts × chapters_per_act × beats_per_chapter` 估算（上限 16384）；`stream=False`。

### 6.2 大纲续写（`continue_outline`）

- [continuation.py L784-L944](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/continuation.py#L784-L944)：逐幕 `emit_act`，每幕成功且非 dry-run 就写盘；某幕失败 → 记 warning 并 break，保留已生成部分。
- 上下文：作品名/题材/一句话故事/可用人物 id=姓名/上一章结尾 800 字。

### 6.3 世界设定（`generate_worlds` / `generate_settings` / `generate_section`）

- 批量世界 [L1708-L1835](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L1708-L1835)：工具 `emit_worlds`，`temperature=0.7`，每批 `batch_size_worlds`（默认 2），每批追加已生成名称防重；预算 1800/条 + 1600 固定。
- 分区设定 [L1838-L1909](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L1838-L1909)：`emit_world/emit_premise/emit_style`，`temperature=0.7`，单轮；非 `overwrite` 时用 `_is_meaningful` 保留已有非空字段。
- 单条世界 [L2366-L2406](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L2366-L2406)：工具 `emit_world`，改写已有条目时把现有 name/era/summary 放进提示词。

### 6.4 人物（`generate_characters` / `generate_section` character）

- 批量 [L1912-L2075](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L1912-L2075)：工具 `emit_characters`（同时产出 `stock_characters`、`narrator_profile`），`temperature=0.8`，每批 `batch_size_characters`（默认 5）；输出预算 `_tool_max_tokens(config, "characters", 本批人数)`（未指定数量时按 8 人预留，5 人约 14.7k，估算上限 16k）。
- 校验：姓名必填且不许重复、至少 1 个 `protagonist`；`voice_profile` 有长度上限；不满足直接抛中文错误。
- 单个人物 [L2408-L2453](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L2408-L2453)：兼容模型平铺返回（不包 `character` 键）。

### 6.5 章节正文（`generate_chapter`）

- 入口 [L469-L615](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L469-L615)。
- 单轮 / 多轮判定：`multi_turn` 显式参数 > 配置 `writing.multi_turn.enabled`，且目标字数 > `threshold_words`（默认 1200）才开多轮；目标字数取节拍 `target_words` 之和。
- 多轮实现 [`_generate_chapter_rounds`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L672-L716)：`round_max_tokens` 默认 2048；`seg_words` 默认 800；`max_rounds` 默认 8；`prompt_cache_key = "{work_id}:{chapter_id}"`。
- 停止原因会写回返回值：`single_round` / `target_reached` / `max_rounds` / `empty_output` / `short_output`。

### 6.6 正文续写（`continue_chapter`）

- [continuation.py L448-L663](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/continuation.py#L448-L663)。
- 默认：每轮 800 字、最多 3 轮、新增硬上限 3000 字、注入最近 2000 字原文、前情摘要 5 章、去重窗口 400 字（常量见同文件 L37-L60）。
- 上下文额外注入「全剧梗概」（由大纲幕摘要或最近章节摘要派生，不额外调模型，上限 600 字，固定不裁剪），并保留「禁止重复」参考（最近 1–2 段，上限 1000 字）。
- 失败：本轮异常 → 记录 `failed` 并 break，已生成内容作为未应用草稿保留；重抽（`--retry`）覆盖旧草稿。

### 6.7 导演标注（`suggest_script`）

- [L719-L880](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L719-L880)。
- 提示词里显式给出 NDW 标注语法（`@shot` / `!sfx` / `!visual` / `!bgm` / `!anim`），并要求 `!sfx` 用 `||` 附英文提示词、`!bgm` 带 `en=`。
- 超过 `script_scenes_per_round`（默认 8）个镜头才分批；每批只带上一批输出末尾 200 字。
- 输出必须能被 `parse_markup` 解析，失败抛 `INVALID_INPUT`，不重试。

### 6.8 音色描述（`build_profile`）

- [voice_design.py L145-L169](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/tts/voice_design.py#L145-L169)：工具调用，`max_tokens=900`（被适配器抬到 4096），`temperature=0.6`，`timeout_s=90`。
- 任意异常（含工具未返回）→ 回退 `_rule_profile` 规则模板，并把原因写进来源备注；不阻断功能。

### 6.9 音效 / BGM 英文改写

- [sfx/prompting.py L268-L327](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/sfx/prompting.py#L268-L327)、[bgm/prompting.py L214-L269](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/bgm/prompting.py#L214-L269)。
- 每次调用新建一个 `OmlxWritingAdapter`；温度 0.2、max_tokens 220/200、`enable_thinking=False`、默认超时 20s（`sfx.prompt.llm_timeout_s` / `bgm.prompt.llm_timeout_s`）。
- 模式 `auto|llm|dict`：`auto` 在 LLM 不可达/超时/输出仍含中文时降级词典；`llm` 直接报错；`dict` 不调 LLM。
- 注意：业务运行时（`media/sfx.py`、`media/music.py`）已改为只读标注阶段落盘的 `prompt_en`，这条改写链路目前只服务 CLI `--rewrite-prompt` 调试场景。

### 6.10 字段润色（`polish_field`）

- [L2456-L2530](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L2456-L2530)。
- 非工具调用：system 要求「只输出 JSON（键为 text）」，实际用 `_extract_json_object` 宽松提取；解析失败则把原文当纯文本接受；结果按 `SCALAR_LIMITS` 截断。
- `temperature=0.6`，未设 `max_tokens`（沿用配置，默认 2048）；注入 `_project_context`（作品/梗概/文风/世界摘要）+ 调用方给的字段上下文。

### 6.11 章节改写与反向总结

- 改写 [editing.py L243-L390](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/editing.py#L243-L390)：选区或整章；超过 `edit_chunk_words`（默认 1200）分块，每块请求带本块正文与前后 200 字；整章改写时把「预算」抬高到 `min(正文字数+32768, 262144)` 并把全文作为一个摘录块。
- 反向总结 [summarize.py L368-L471](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/summarize.py#L368-L471)：短正文 1 轮；长正文先逐块提取要点（块阈值 `summary_chunk_words` 默认 3000），再用 1 轮合并成 JSON；非分块路径对超长正文只取开头 12000 字 + 结尾 6000 字（L30-L31）。

## 7. 上下文注入与裁剪

### 7.1 章节类任务（正文 / 续写 / 改写）

统一由 [text/context.py `ContextAssembler`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/text/context.py#L400-L510) 装配：

- 注入顺序：故事圣经 → 世界设定 → 角色卡 → 前情摘要 → 本章大纲与节拍 → 前文摘录 → 任务指令；另有固定的「写作准则」段落（人物按人设行动、不可越权知道信息等）。
- 角色范围：优先只取本章 POV + 节拍涉及角色；一个都没命中才回退全量非旁白角色。
- 前情：index 更小的章节，按倒序最多取 3 章（`build_chapter_context`），每章优先用 `synopsis`，缺失时取正文末尾 800 字。
- 摘录：上一章结尾 + 本章已有正文结尾，各最多 800 字（续写时改成最近 2000 字）。
- 预算：`writing.context_budget_tokens`（默认 32768），中文按 1 字 ≈ 1 token 粗算。
- 降级顺序（代码里的 while 循环，每次只降一级）：
  1. 删前文摘录（先本章、再上一章）
  2. 删最旧的一条前情摘要
  3. 删世界细节（先地点/术语，再硬规则/势力）
  4. 角色卡降级（先关背景，再关性格/说话方式）
  5. 仍超预算 → 只在 `warnings` 里提示，故事圣经与任务指令永不裁剪
- 续写额外固定保留「全剧梗概」（`story_digest`），不参与上述降级。

### 7.2 非章节类任务

- 设定/人物/润色/导演标注共用 [`_project_context`](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L1602-L1661)：作品名与题材、一句话故事、主题、核心冲突、结局意图、目标读者、基调、文笔风格（截 200 字）、时代、世界规则/势力（各前 6 条）、关键地点（前 6 个）、最多 3 条世界摘要、最多 20 个已有角色名。**没有 token 预算与降级逻辑**，只是各字段定长截断。
- 导演标注另外把场景/台词全文拼进 user（metadata 不发给真实模型）。
- 音色描述只取人物档案精简版（背景截 600 字）。
- 润色是 `_project_context` + 可选字段上下文 + 待润色原文。

## 8. 已知问题与风险（代码可见）

1. **`omlx.temperature` 对多数业务调用无效，`omlx.max_tokens` 现在生效**：`TextGenRequest.temperature` 默认 0.8 永远非空，请求值优先；`max_tokens` 默认改为 0 后会回落到配置值，工具调用则取 `max(配置值, 业务估算)`（估算上限 16384，配置兜底 32768）。
2. **工程级 `settings.writing` 只对 `build_adapters` 生效**：`build_writing_adapter` 的 omlx 分支只读 `config.omlx`，因此工程内覆盖 endpoint/model/temperature 等对 service 层写作调用不生效（`build_adapters` 在音色设计、`cli_cast` 链路生效）。
3. **工具调用没有纠错闭环**：未返回 `tool_calls`、参数非法 JSON、缺 `function.name` 都直接抛错；上层不重问，用户只能手动重试。截断（`finish_reason=length`）现在会转成可读中文提示并保留 token 用量，但**不会自动重发**。批量场景（人物/世界/导演标注/章节编辑/总结分块）中任一批失败会中断整个动作，已完成的批次结果不落盘（大纲续写例外：已生成的幕会保留）。
4. **多轮历史无二次裁剪**：章节正文/续写每轮把已生成文本整段追加进 `messages`，`context_budget_tokens` 只约束首轮；长轮次时请求体持续增长，只能靠服务端上下文窗口兜底，没有超限保护。
5. **整章改写的超长边界**：预算上限 `min(len+32768, 262144)`，若正文超过约 23 万字，全章正文摘录会在裁剪第 1 步被整块删掉（该块 `kind=current` 优先被弹出），模型可能完全看不到原文，只在 `warnings` 里提示超预算。
6. **`content` 为空时回退 `reasoning_content`**：开启 thinking 后被 `max_tokens` 截断时，可能把推理片段当正文返回（章节正文、润色、续写都会接收）。
7. **润色的 JSON 约束是软约束**：解析失败会把模型原文直接当润色结果；system 提示与「只输出 JSON」不一致时不会报错，用户可能拿到带解释文本的结果（仅按 `SCALAR_LIMITS` 截断）。
8. **导演标注分批只带回上一批末尾 200 字**，跨批次的镜头风格/编号衔接依赖模型自觉；`@shot` 序号错位只会在解析阶段报错。
9. **音效/BGM 改写每次新建适配器实例**，无连接复用；`auto` 模式降级是静默的（CLI 会打印提示，但返回值里只有 `path` 字段区分）。
10. **`stream` 字段与 `omlx.max_concurrency` 是摆设**：业务全部非流式、串行；这两个配置不会改变调用行为。

## 9. 证据索引

| 主题 | 位置 |
| --- | --- |
| 适配器默认常量 / 请求体 | [adapters/writing.py L26-L37、L572-L607](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L26-L37) |
| 非流式生成 / 工具调用 / 流式 | [adapters/writing.py L644-L824](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L644-L824) |
| 上下限探测 / 上下文守卫 | [adapters/writing.py L311-L462、L521-L570](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L311-L462) |
| 统一请求结构默认值 | [adapters/base.py L92-L106](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/base.py#L92-L106) |
| 本机默认配置 | [config.py L18-L40](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/config.py#L18-L40)、[config/config.yaml L23-L61](file:///Users/zhaohaochen/git/novel-drama-workbench/config/config.yaml#L23-L61) |
| 适配器装配（两套） | [writing/service.py L64-L85](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L64-L85)、[adapters/__init__.py L37-L68](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/__init__.py#L37-L68) |
| 上下文装配与降级 | [text/context.py L400-L510](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/text/context.py#L400-L510) |
| 章节多轮停止条件 | [writing/service.py L672-L716](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L672-L716) |
| 续写默认值与停止条件 | [writing/continuation.py L37-L60](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/continuation.py#L37-L60)、[L578-L663](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/continuation.py#L578-L663) |
| 思考模式下发 | [adapters/writing.py L598-L600](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L598-L600) |
