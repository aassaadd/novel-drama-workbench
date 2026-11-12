# R19：大纲与正文续写设计

> 状态：设计完成，待确认（未改任何生产代码）
> 日期：2026-11-12
> 关联：`src/novel_drama/writing/service.py`、`src/novel_drama/writing/editing.py`、`src/novel_drama/text/context.py`、`src/novel_drama/cli.py`、`src/novel_drama/web/app.py`、`src/novel_drama/web/static/page-outline.js`、`src/novel_drama/web/static/page-chapter.js`、`schema/chapter.schema.json`、`schema/setting-bible.schema.json`
> 目标：在已有内容基础上「再写一段 / 再排几章」，而不是每次从零重写。
> 约束：只追加或替换指定范围；不整篇重写；不破坏已有正文、场景、台词与下游产物。

---

## 0. 一句话结论

现在缺的不是「多轮写」，而是「**从已有内容接着写**」：
- 正文侧已有 `_generate_chapter_rounds`（分段、重叠裁剪、停止条件、前缀缓存），但多轮是从**空正文**起步，`chapter generate` 遇到已有正文还会要求 `--regen`（[service.py:462](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L462)）。
- 大纲侧 `generate_outline` 每次都会用新结果整体替换 `bible.outline.acts`（[service.py:185](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L185)），没有「在已有第 N 章之后追加」的语义。
- `chapter.continuations[]` 与 `mode=continue|outline_driven|rewrite_range` 的 schema 早就存在（[chapter.schema.json:53](file:///Users/zhaohaochen/git/novel-drama-workbench/schema/chapter.schema.json#L53)），但代码只用了 `rewrite_range`（[editing.py:420](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/editing.py#L420)）。

因此本设计的核心是：**复用现有上下文装配、多轮分段与 `continuations[]` 记录，新增「续写」入口与进度/确认语义**；schema 首版零改动。

---

## 1. 现状核查（代码事实）

| 项 | 现状 | 位置 |
| --- | --- | --- |
| 大纲生成 | 单轮 `emit_outline` 或按幕多轮 `_generate_outline_acts`；轮次内只在内存累计，最后整体 `bible.outline.acts = built_acts` | [service.py:69-202](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L69-L202)、[service.py:205-257](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L205-L257) |
| 大纲结构 | `outline.acts[]`，每幕有 `chapters[]`；章节有全书序号 `index`、`summary`、`beats[]`；`act` 无索引字段；schema `additionalProperties:false` | [setting-bible.schema.json:82-131](file:///Users/zhaohaochen/git/novel-drama-workbench/schema/setting-bible.schema.json#L82-L131) |
| 大纲落地 | `apply_outline` 按 `index` 排序：无章节则建、有章节只同步标题/大纲文本，不覆盖正文 | [service.py:260-314](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L260-L314) |
| 正文生成 | 有正文且未 `--regen` 直接报错；多轮从 `content=""` 开始，按 `segment_words/max_rounds` 循环 | [service.py:431-577](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L431-L577) |
| 多轮停止条件 | `target_reached`（≥目标 1.1 倍）、`max_rounds`、`empty_output`、`short_output`（本轮新增 <50 字） | [service.py:634-678](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L634-L678) |
| 去重 | `_append_segment` 与末尾最多 200 字做重叠裁剪 | [service.py:592-619](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L592-L619) |
| 上下文 | `ContextAssembler` 固定顺序：故事圣经→世界→角色卡→前情摘要→本章大纲/节拍→前文摘录→任务指令；超预算按固定顺序裁剪；中文按 1 字≈1 token | [context.py:53-501](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/text/context.py#L53-L501) |
| 前情 | 只取当前章之前最多 **3 章**，有 `synopsis` 用摘要，没有用正文末尾 **800 字**；上一章/本章已有正文各取末尾 **800 字** | [service.py:384-399](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L384-L399)、[context.py:16-20](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/text/context.py#L16-L20) |
| 摘要 | `chapter summarize` 反向生成 `title/synopsis/outline/beats`，可写回章节与设定库；`synopsis` 是「滚动摘要，供后续章节上下文使用」 | [summarize.py:368-471](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/summarize.py#L368-L471)、[chapter.schema.json:15](file:///Users/zhaohaochen/git/novel-drama-workbench/schema/chapter.schema.json#L15) |
| 续写记录 | `editing.py` 已按 schema 写 `continuations[]`（目前只用 `rewrite_range`），含 `produced` 字符区间、`usage`、`model` | [editing.py:420-447](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/editing.py#L420-L447) |
| 模型窗口 | 配置有 `omlx.context_window: 262144`、`omlx.max_tokens: 2048`；上下文预算另有 `writing.context_budget_tokens`（代码默认 32768） | [config.example.yaml:22-39](file:///Users/zhaohaochen/git/novel-drama-workbench/config/config.example.yaml#L22-L39)、[service.py:504](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/service.py#L504) |
| 输入超限 | 适配器只把消息原样发给 OpenAI 兼容接口，**没有发送前的长度预检** | [adapters/writing.py:294-329](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L294-L329) |
| Web 入口 | 大纲页只有「生成大纲 / 落地」；章节页只有「生成正文（含 regen 勾选）/ 预览」 | [page-outline.js:369-420](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/web/static/page-outline.js#L369-L420)、[page-chapter.js:394-447](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/web/static/page-chapter.js#L394-L447) |

**结论**：多轮分段、上下文裁剪、续写记录三块地基都在，续写是「新增编排 + 新增入口」，不是重写生成器。

---

## 2. 两种续写对象

### 2.1 大纲续写

**语义**：已有 N 章大纲，追加后面若干章（跨越新幕时自动建幕），不改动已有 N 章。

| 项 | 设计 |
| --- | --- |
| CLI | `ndw outline continue --chapters 3 [--chapters-per-act 3] [--beats-per-chapter 3] [--words 1200] [--acts 0] [--brief "..."] [--dry-run] [--apply]` |
| 默认量 | `--chapters 3`；`--acts` 与 `--chapters` 二选一，同时给时以 `--chapters` 为准 |
| 幕处理 | 从 `len(acts)+1` 开始新增幕；若 `--chapters` 不能整除 `chapters_per_act`，最后一幕章数可少于每幕章数 |
| 起始索引 | `start_index = max(已有大纲章节最大 index, 已有章节文件最大 index) + 1`；两者不一致时返回 warning「大纲与章节文件存在序号空洞，已按最大值续写」 |
| 生成方式 | 每幕一轮（复用 `_generate_outline_acts` 的「已完成幕标题+摘要」只读上下文），每轮结束**立即追加并落盘**（见 §7） |
| 上下文 | `premise.logline / theme / core_conflict` + 题材 + 可用人物 + **已有幕标题/摘要** + **最近 10 章标题/摘要** + 「已写到第 N 章，不要重复」 |
| 落盘 | 只追加 `bible.outline.acts[]`，不删除、不重排；`SettingsStore.save` 校验后 `settings_revision+1` |
| 落地 | 生成后仍需 `outline apply` 才会创建 `chapters/<id>.json`；已有章节只更新标题/大纲文本（现状不变） |
| Web | 大纲页新增「续写大纲」按钮 + 「追加章数」输入；调用 `POST /api/ui/outline/continue` |
| 预览 | `--dry-run` 返回 `{start_index, new_chapters:[{index,title,summary}], acts_added, chapters_added}`，不写盘 |

**为什么不用 `outline generate --append`**：`generate` 的语义是「生成一整本」，会让参数（acts/chapters_per_act）与已有内容互相矛盾；独立 `continue` 子命令能让 Web 与 CLI 的参数、预览、落盘都更清晰。

### 2.2 正文续写

**语义**：已有正文，从最后一个字符之后继续写；只追加，不重写。

| 项 | 设计 |
| --- | --- |
| CLI | `ndw chapter continue --chapter X [--words 800] [--rounds 3] [--instruction "..."] [--from-beat K] [--dry-run] [--apply]` |
| 默认量 | 每轮 `--words 800`，最多 `--rounds 3`（单次最多约 2400 字）；均可配置 |
| 默认行为 | **预览优先**：不带 `--apply` 只返回预览，不写盘；带 `--apply` 才追加 |
| 上下文 | 复用 `ContextAssembler`；把 `include_recent_chars` 提高到 2000（可配置），前情摘要 3→5 章；本章只注入**剩余节拍** |
| 衔接锚 | 现有正文末尾 200 字作为「衔接锚点」原样放入指令；另取末尾 500 字作为「禁止重复」参考 |
| 生成方式 | 复用 `_generate_chapter_rounds`，但首轮 assistant 前缀改为「已有正文末尾 2000 字」，而不是空串 |
| 落盘 | `content = 已有正文 + "\n\n" + 新增段`；更新 `word_count`、`revision+1`、`updated_at`；追加 `continuations[]`，`mode="continue"` 或 `"outline_driven"`，`produced={char_start,char_end}` |
| 下游 | 若已有 `scenes` 或 `script_path`：`status="stale"` + warning「正文已变更，需重新切分场景与台词」（沿用 [editing.py:399-401](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/editing.py#L399-L401) 的既有约定）；否则 `status="written"` |
| Web | 章节页新增「续写正文」按钮：先预览（显示追加字数/位置/预览文本），用户点「应用」后再落盘 |

**为什么与 `chapter generate` 分开**：`generate` 的既有约定是「有正文必须 `--regen` 才写」，且 `--regen` 是整章覆盖；改成「有正文自动续写」会破坏现有 CLI 与用户预期。

---

## 3. 上下文怎么给

### 3.1 复用现有装配器，不另起一套

`ContextAssembler` 已经解决「固定顺序 + 超预算裁剪 + 中文 1 字≈1 token」，续写只需增加两类素材：

1. **全剧梗概（新增，结构化拼装，不额外调模型）**
   - 来源：`bible.outline.acts[].title/summary` + 已落地章节的 `index/title/summary`。
   - 组装：`第 1 幕《X》：<幕摘要>` 逐幕一行；最近 10 章再加 `第 N 章《标题》：<章节摘要>`。
   - 上限：幕摘要合计 ≤ 2000 字；章节摘要只取最近 10 章、合计 ≤ 2000 字；更早章节只保留 `第 N 章《标题》`。
   - 若设定库没有大纲：退化为最近 5 章的 `synopsis` 拼接（≤ 1000 字）。
   - **不新增持久化字段**，每次从现有结构派生。

2. **本章剩余节拍（新增）**
   - 由 `--from-beat K` 指定起始节拍（1 起）；`K` 之后（含 K）的节拍才注入，已经写完的节拍不再出现，避免模型回头复述。
   - 不传 `K` 时按「已写字数 / 本章目标字数」估算：`K = floor(已写字数 / 章目标字数 × 节拍数) + 1`，并在返回里回显估算值。

### 3.2 摘要从哪来（能否复用现有「章节总结」）

**能，直接复用**：

| 素材 | 来源 | 现状 |
| --- | --- | --- |
| 前情章节摘要 | `chapter.synopsis`（`chapter summarize` 生成，schema 上限 4000 字） | 已有；`build_chapter_context` 目前只取前 3 章 |
| 设定库章节摘要 | `outline.acts[].chapters[].summary`（上限 2000 字） | 已有；`summarize --apply` 会同步 |
| 全剧梗概 | 由上述两类摘要 + 幕摘要拼装 | **新增派生逻辑**，零新字段 |
| 最近原文 | `chapter.content` 末尾切片 | 已有（800 字），续写扩大到 2000 字 |

缺摘要的章节沿用现有兜底：取正文末尾 800 字（[context.py:17](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/text/context.py#L17)），并写 warning。

### 3.3 预算怎么分配

**总输入预算**：

```
input_budget = min(writing.context_budget_tokens（默认 32768）,
                   omlx.context_window（默认 262144） - 单轮 max_tokens（默认 2048）)
```

推荐分段上限（中文按字≈token；典型占用约 10K，远低于 32K）：

| 段 | 上限 | 可裁剪 |
| --- | --- | --- |
| 故事圣经 + 文风 + 写作准则 | ≈ 1500 | 否 |
| 全剧梗概（派生） | 600 | 是（整段删） |
| 世界设定 | 1200 | 是（地点/术语 → 规则/势力） |
| 角色卡（仅本章出场） | 2000 | 是（背景 → 性格/说话方式） |
| 前情摘要（最多 5 章 × 200 字） | 1000 | 是（先删最旧） |
| 本章剩余节拍 | 800 | 否 |
| 最近原文（衔接锚 + 禁止重复） | 2000 + 500 | 是（先降到 800 + 300） |
| 任务指令（字数/格式/从锚点之后写） | 500 | 否 |
| 输出预留 | `round_max_tokens`（默认 2048） | 否 |

**超限降级顺序（续写专用，和现有顺序略有不同）**：

1. 删「全剧梗概」；
2. 删世界细节（先地点/术语，再规则/势力）；
3. 删角色卡细节（先背景，再性格/说话方式）；
4. 删最旧的前情摘要（5→3→0）；
5. 缩短最近原文（2000→1200→800，最低 400）；
6. 仍超：**不静默截断正文**，直接抛出中文错误：
   「上下文超出模型窗口（约 X tokens > 可用 Y）：请减少前情摘要数量或最近原文长度」。
   同时在返回里带上 `context_warnings`。

**发送前预检**：现有适配器不检查输入长度，超限只能等上游 400。设计在 `generate_chapter`/`continue_chapter` 内先按 `len(system)+len(user)+max_tokens` 预检；超限走上面的降级，降级到底仍超则中文报错，不把裸 400 抛给用户。

---

## 4. 一次写多少、什么时候停

| 参数 | 默认 | 可配置 | 说明 |
| --- | --- | --- | --- |
| 正文每轮字数 | 800 | `writing.continue.segment_words` / `--words` | 一轮约 2~4 个自然段 |
| 正文最多轮数 | 3 | `writing.continue.max_rounds` / `--rounds` | 单次最多约 2400 字 |
| 正文单次硬上限 | 3000 字 | `writing.continue.max_total_words` | 达到即停，避免一次写飞 |
| 大纲每次追加章数 | 3 | `--chapters` | 跨幕自动建幕 |
| 大纲每章节拍 | 3 | `--beats-per-chapter` | 沿用现有生成参数 |
| 大纲单章目标字数 | 1200 | `--words` | 用于节拍 `target_words` 分配 |

**正文停止条件（在现有 4 个之上补 1 个）**：

| stop_reason | 触发 |
| --- | --- |
| `target_reached` | 累计新增 ≥ 目标字数 |
| `paragraph_boundary` | 本轮新增 ≥ 目标的 80%，且最后一个字符是 `。！？…”` 或换行（优先停在自然段末） |
| `max_rounds` | 达到轮数上限 |
| `empty_output` | 本轮模型返回空 |
| `short_output` | 本轮实际新增 < 50 字（模型在复述/卡住） |
| `max_total_words` | 累计新增达到硬上限 |

**大纲停止条件**：新增章节数达到 `--chapters`；或模型连续返回空/重复（沿用 `empty_output`，立即停并保留已完成幕）。

**自然段收尾**：每轮提示词要求「本轮写到自然段结束就停，不要停在半句」；接收端若本轮末尾既不是句末标点也不是换行，则用 `_append_segment` 后追加一句提示，不强行改写模型输出。

---

## 5. 避免重复与跳跃

### 5.1 防复述（具体手段）

1. **衔接锚点**：指令中原样引用现有正文末尾 200 字：

   ```
   【衔接锚点】……（最后 200 字）
   【写作起点】从锚点最后一个字之后开始写第一句，禁止复述锚点里的任何句子或段落。
   ```

2. **禁止重复参考**：再给末尾 500 字，标注「以下内容已写过，不得以任何形式重复」。
3. **代码级去重**：沿用 `_append_segment` 的末尾重叠裁剪，比较窗口由 200 字提高到 400 字；若裁剪后本轮新增 < 50 字，直接判 `short_output` 停止，不把复述写进正文。
4. **相似度兜底**：新增段的前 80 字若出现在「已有正文最后 500 字」中，按重叠裁剪；裁剪后仍大面积重合则整轮丢弃并提示「模型在复述上一段，本轮已丢弃，可重试或补充续写思路」。

### 5.2 防时间线/场景跳跃

1. **剩余节拍绑定**：只注入 `--from-beat K` 之后的节拍，并在指令里写「当前必须推进：第 K 个节拍 <摘要>」；不允许跳过节拍。
2. **接续状态块**（从现有数据派生，不额外调模型）：

   ```
   【接续状态】上一段结尾：<最后一段>
   出场人物：<最近一段命中的角色名>
   时间/地点提示：<本章大纲摘要中的时间地点关键词；没有则省略>
   ```

3. **上一章摘要**：前情摘要最后一条固定是「上一章」，让模型知道跨章边界已经发生。
4. **一致性告警**（只提示不阻断）：新增段里出现未在角色表/世界词条中登记的人名或术语时，返回 warning「出现未登记名称：X，请确认是否为笔误」。

---

## 6. 不破坏已有内容

### 6.1 只追加 / 只替换指定范围

- 正文续写：只在 `content` 末尾追加，`produced.char_start = 旧长度`，`char_end = 新长度`；不触碰 `scenes/lines/script`。
- 大纲续写：只 `acts.append(...)`；已有幕与章节对象原样保留；不重排、不重新编号。
- 首版不提供「续写时顺便改写前文」；改写仍走既有 `chapter edit --range`。

### 6.2 先看再落盘（diff / 确认）

**正文**（默认预览，必须二次确认）：

```
本次将追加 812 字（当前 3400 字 → 4212 字）
追加位置：正文末尾（第 3400 字之后）
衔接锚点：……最后 60 字……
[预览新增文本]
[取消] [应用并追加]
```

- CLI `--dry-run` 返回：`{preview, added_words, before_words, after_words, append_start, stop_reason, from_beat, covered_beats, context_warnings}`。
- Web 用现有 `modal.js` 弹确认框；「应用」按钮再调 `--apply`，避免模型写一次、用户看不到就落盘。

**大纲**（同样默认预览）：

```
本次将新增 1 幕 / 3 章：第 13~15 章
13.《...》 14.《...》 15.《...》
[取消] [写入大纲]  （写入后仍需「落地」才建章节文件）
```

### 6.3 落盘细节

- 字数按非空白字符统计（复用 `_count_words`）。
- `content` 末尾补 `\n`，与 `generate_chapter`、`save_chapter_content` 保持一致。
- `chapter.content` schema 上限 200000 字符：追加前检查，超限返回中文错误「本章已接近长度上限，无法继续追加」。
- `revision + 1`、`updated_at` 更新；`continuations[]` 追加一条记录（复用 `editing.py` 的字段形状）。
- 下游存在时 `status="stale"`，并提示重新切分；不存在时 `status="written"`。

---

## 7. 中断与续批

### 7.1 正文

| 场景 | 处理 |
| --- | --- |
| 预览模式中断 | 什么都没写，直接丢弃 |
| 应用模式，第 k 轮模型报错 | 前 k-1 轮已成功段落**逐轮落盘**（每轮结束即保存），第 k 轮整体丢弃（模型没返回完整文本，不保留半句）；`continuations[].status="failed"`，`produced` 指向已落盘末尾 |
| 用户主动停止 | 已成功的轮次保留，`status="canceled"`；下一次续写从新的正文末尾继续 |
| 下一轮从哪继续 | 永远从 `chapter.content` 的实际末尾继续；衔接锚点、禁止重复参考都重新取末尾，不依赖上次的临时状态 |
| 重复触发 | 续写进行中按钮禁用；同一章节同时只允许一个续写请求（Web 端 busy + 服务端按章节加进程内锁，可选） |

**逐轮落盘**是这次设计的关键改动：现有 `_generate_chapter_rounds` 把结果攒在内存，失败就全丢；续写模式改成每轮 `_append_segment` 后立即 `save_chapter`，保证「已写的不会白写」。

### 7.2 大纲

| 场景 | 处理 |
| --- | --- |
| 按幕生成，第 m 幕失败 | 前 m-1 幕**已逐幕写入** `bible.outline.acts`，返回 `{partial: true, acts_added: m-1, warnings:[...]}` |
| 用户停止 | 已写入的幕保留；未开始的幕不写 |
| 下次继续 | `continue` 重新计算 `start_index` 与 `len(acts)`，从断点继续，不需要额外进度字段 |
| 预览模式 | 全部成功才返回完整预览，不写盘 |

---

## 8. 改动面与 schema 结论

### 8.1 需要动的模块

| 模块 | 改动 |
| --- | --- |
| `src/novel_drama/writing/service.py` | 新增 `continue_outline(...)`、`continue_chapter(...)`；`_generate_chapter_rounds` 增加 `initial_content`、`append_callback` 参数（不改变现有调用行为）；新增「全剧梗概」拼装函数 |
| `src/novel_drama/text/context.py` | 新增 `story_digest` 素材与 `excerpt_chars/previous_summary_count` 可调参数；保持现有默认值不变 |
| `src/novel_drama/writing/summarize.py` | 不改；续写只读 `synopsis` |
| `src/novel_drama/cli.py` | 新增 `outline continue`、`chapter continue` 子命令；参数见 §2 |
| `src/novel_drama/cli_writing.py` | 新增两个命令处理函数 |
| `src/novel_drama/web/app.py` | 新增 `POST /api/ui/outline/continue`、`POST /api/ui/chapter/continue` |
| `src/novel_drama/web/static/page-outline.js` | 新增「续写大纲」按钮、章数输入、预览确认 |
| `src/novel_drama/web/static/page-chapter.js` | 新增「续写正文」按钮、字数/轮数/起始节拍输入、预览确认弹窗 |
| `config/config.yaml` + `config/config.example.yaml` | 新增 `writing.continue.*`（示例配置目前没有整个 `writing` 段，需一并补齐示例） |
| `docs/` | 本文档 |
| `项目修改记录文档.md` | 登记「设计完成，待确认」 |

**不碰**：`media/`、`src/novel_drama/director/`、TTS/配音链路、场景切分逻辑。

### 8.2 新增字段 / schema 结论

**首版不需要改 schema**：

| 需求 | 现有承载 |
| --- | --- |
| 记录本次续写 | `chapter.continuations[]`（已存在，`mode=continue/outline_driven`） |
| 记录追加区间 | `continuation.produced.char_start/char_end`（已存在） |
| 记录上下文参数 | `continuation.context.include_recent_chars/budget_tokens/...`（已存在） |
| 记录用量/模型 | `continuation.usage/model`（已存在） |
| 大纲「已写到第几章」 | 由 `outline.acts[].chapters[].index` 最大值派生，无需落盘 |
| 大纲「上次续写时间」 | 现有 `settings_revision` / `updated_at` 足够；不新增 |
| 起始节拍 | 写入 `continuation.outline_ref`（已存在的自由字符串），如 `"beat:3"` |

**如果后续要做「续写进度面板/断点续传可视化」**，再考虑给 `setting-bible.outline` 增加 `meta`；因为 schema 是 `additionalProperties:false`，那会是一次显式 schema 变更，首版不做。

### 8.3 新增配置（建议值）

```yaml
writing:
  context_budget_tokens: 32768     # 现有键，续写继续使用
  continue:
    segment_words: 800             # 正文每轮目标字数
    max_rounds: 3                  # 正文单次最多轮数
    max_total_words: 3000          # 正文单次硬上限
    recent_chars: 2000             # 衔接锚 + 禁止重复参考的总字数
    previous_summaries: 5          # 注入前情摘要的章数
    overlap_chars: 400             # 去重比较窗口
    outline_chapters_per_turn: 3   # 大纲续写默认追加章数
```

---

## 9. 同类工具对标

| 工具 | 续写交互与上下文 | 对本设计的结论 | 来源 |
| --- | --- | --- | --- |
| Sudowrite（Write） | 文档内点 Write 继续写；官方说明正文至少约 20 个词时效果最好；Story Bible 注入风格/类型/角色/世界观/章节大纲；Chapter Continuity 最多读取链接的前 20,000 词、最多 25 个前序文档；生成结果先以紫色文本插入，用户可接受/丢弃；支持 Guided Mode 输入「接下来想发生什么」 | 采用「衔接锚 + 前情摘要 + 大纲」；采用「先预览后接受」；提供可选续写思路输入 | [Write 文档](https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/write/pvxUvbQqYybfEosqx1sXjY)、[Chapter Continuity](https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/chapter-continuity/4KL8gFeLZQ6GSBjDWtSbV6) |
| NovelAI | Memory 存长期事实，Lorebook 用激活关键词按需注入词条，Author's Note 固定文风；上下文上限按订阅档位（约 3K/6K/8K tokens，另有资料称高档位 28K，存在冲突） | 不把设定整篇塞入；用「全剧梗概 + 本章相关角色/世界」控制体积；预算必须硬上限 | [NovelAI 评测](https://shakespeareai.braintastic.ca/blog/novelai-review-2026)、[上下文档位](https://aitoolreviews.co/articles/novelai/) |
| Claude Projects | Project knowledge 注入设定文档，靠大窗口（200K/500K/1M）承载；输入超窗返回 400 “prompt is too long”；没有专门的「续写按钮」，需要用户自己组织上下文 | 大窗口不等于不用预算；发送前必须预检；本设计提供专用续写入口而不是让用户手工拼 prompt | [上下文窗口文档](https://platform.claude.com/docs/en/build-with-claude/context-windows?content=Oct2024AnalysisTool&medium=email&messageTypeId=140367)、[用量与长度限制](https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work) |
| 番茄小说（作家助手） | 选中文字或点「续写」，AI 紧接着上文生成；可先简述下文情节；另有「卡文锦囊」一次给 5 个后续情节选项 | 采用「可选续写思路」；「多候选」列为后续增强，不进首版 | [番茄 AI 写作功能](https://fanqienovel.com/writer/zone/article/7327136545129906238)、[编辑器说明](https://fanqienovel.com/writer/zone/article/7455639656860762137) |
| 彩云小梦 | 输入一段文字后点「AI 小梦来帮写」，一次给 3 条不同走向；选中一条后可改再继续；支持世界/人物词条 | 验证「候选走向」有价值，但会放大成本；首版先做单次可预览续写 | [Microsoft Store 介绍](https://apps.microsoft.com/detail/xpdb1hjg80k805?hl=en-US&gl=CN)、[功能介绍](https://www.aiww.com/aitool/caiyunxiaomeng/jianjie) |

**对标结论**：
1. 主流工具都做「**读最近原文 + 设定摘要 + 可选的用户意图**」，没有一家把整本正文塞进 prompt。
2. 「**生成 → 预览/可撤销 → 接受**」是通用交互；直接落盘是少数。
3. 「**多候选**」（彩云/番茄）体验好但成本×3，建议二期再评估。
4. 上下文上限必须由工具侧兜底，不能依赖模型报 400。

---

## 10. 待拍板清单（每项给推荐）

| # | 待拍板 | 推荐 | 备选 |
| --- | --- | --- | --- |
| 1 | 入口形态 | 新增 `outline continue` / `chapter continue`，不改现有 `generate` 语义 | 给 `generate` 加 `--continue` 开关 |
| 2 | 正文单次默认量 | 800 字 × 3 轮（上限 3000 字），可配置 | 直接按「本章剩余目标字数」一次算完 |
| 3 | 落盘方式 | 默认预览，用户点「应用」才落盘；CLI 需 `--apply` | 直接落盘 + 撤销 |
| 4 | 中断策略 | 已成功轮次逐轮落盘保留；失败轮整轮丢弃；记录 `failed/canceled` | 失败即整体回滚 |
| 5 | 章节状态 | 沿用现有约定：有 `scenes`/`script_path` 标 `stale` 并提示重新切分，否则 `written` | 永远保持原状态 |
| 6 | 节拍进度 | `--from-beat K`，缺省按字数比例估算并在返回中回显；写入 `continuation.outline_ref="beat:K"` | 首版不做节拍进度，固定从第 1 个节拍写 |
| 7 | 全剧梗概 | 从 `outline.acts` + 章节 `summary` 结构化拼装（≤600 字），不额外调模型 | 新增一次 LLM「故事到目前为止」摘要并持久化 |
| 8 | 大纲续写粒度 | `--chapters N` 为主，跨幕自动建幕，每幕一轮并逐幕落盘 | 只支持 `--acts N` |
| 9 | schema | 首版零改动，进度由 `index` 派生 | 给 `outline` 增加 `meta`（需要 schema 变更） |
| 10 | 多候选 | 首版不做 | 二期支持一次 2~3 条候选（成本×N） |
| 11 | 续写后摘要 | 不自动调用总结；在预览结果里提示「建议续写完成后重新生成章节摘要」 | 续写成功自动跑 `chapter summarize --apply`（会多一次模型调用并可能改标题/节拍） |

---

## 11. 验收口径（实现后按此验证）

1. **大纲续写**：已有 9 章，`outline continue --chapters 3` 后新增第 10~12 章，原 9 章标题/摘要/节拍逐字段不变；`outline apply` 只新建 3 个章节文件。
2. **正文续写**：已有 3400 字，`continue` 预览显示新增约 800 字、位置在末尾；`--apply` 后原 3400 字逐字不变，`word_count`/`revision` 正确，`continuations[-1].produced` 区间连续。
3. **防复述**：构造上一段末尾与模型输出开头重叠的用例，确认重叠被裁剪且不会二次追加。
4. **防跳跃**：`--from-beat 3` 时 prompt 只含第 3 个及之后的节拍（用离线适配器断言 metadata/消息）。
5. **超限降级**：把 `writing.context_budget_tokens` 调到很小，确认按 §3.3 顺序裁剪并返回 `context_warnings`；降到最低仍超时返回中文错误而不是裸 400。
6. **中断**：模拟第 2 轮抛错，确认第 1 轮文本已落盘、`continuations[-1].status="failed"`，再次续写从新末尾继续且不重复。
7. **下游保护**：已有 `scenes` 的章节续写后 `status="stale"` 并出现重新切分提示。
8. **不做**：不改 `media/`、不改场景切分、不整章重排、不自动生成候选。

---

## 12. 明确不做

- 不做整章/整本重写（仍走 `--regen` 或 `chapter edit`）。
- 不做向量检索与跨书设定召回。
- 不做多候选并排生成（二期）。
- 不做续写进度字段与断点文件（首版靠 `content` 末尾 + `index` 派生）。
- 不改场景切分、导演脚本、配音/媒体链路。
