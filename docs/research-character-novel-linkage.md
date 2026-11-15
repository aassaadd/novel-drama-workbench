# 角色与小说正文的关联：行业调研与本项目脱节点分析（只给方案，不改代码）

> 目标：解释「角色库」和「小说正文」为什么会像两套东西，并给出按投入分档、可落地的关联方案。
> 本文所有本项目判断均以当前代码为准，标注 `文件:行号`；行业判断标注证据等级：
> **[官方文档]** 产品官方帮助/文档；**[官方页面]** 产品官网功能页；**[第三方评测]** 媒体或评测文章；**[推断]** 基于以上材料的推理，未直接验证。

---

## 0. 结论摘要

1. 成熟产品的共同点不是「角色卡写得多细」，而是三件事：**角色是独立实体**、**场景/节拍显式挂角色与 POV**、**写作时按当前场景自动注入相关条目**。这三件事本项目已有约 70% 的数据结构，但只用了 30%。
2. 真正的分水岭是「**信息回流**」。少数产品（Novelcrafter）用「场景级角色增量」把正文里发生的变化挂回角色，只在该场景及之后进入上下文；多数产品仍靠作者手工改卡。
3. 本项目的核心脱节不是缺字段，而是：
   - **POV 只作为筛选条件存在**，从未以「本章视角人物：某某」进入提示词；
   - **节拍的 `character_ids` 只用于过滤角色卡**，模型看不到「第 2 个节拍谁出场」；
   - **`speech_style` 全项目没有任何写入方**，所以提示词里的「说话方式」永远为空；
   - **正文生成只回写章节摘要与全书摘要**，角色 `state.*`、节拍选角一概不动；
   - **角色页与画本/大纲只有单向跳转**（角色→章节有，章节→角色没有），且 POV、节拍选角在 UI 上不可编辑。
4. 建议按 P0（只改渲染与派生，1 天内）、P1（可编辑 + 回流预览，2–4 天）、P2（结构化关系/弧线/一致性校验）三档推进；P0 就能消掉大部分「两套东西」的体感。

---

## 1. 行业调研：角色条目如何与正文绑定

### 1.1 机制清单与普遍度

| 机制 | 具体形态 | 代表产品 | 普遍度 | 证据 |
|---|---|---|---|---|
| 独立角色实体 + 写作自动注入 | 角色卡/Codex 条目集中维护，生成时自动带入相关条目 | Sudowrite Story Bible、Novelcrafter Codex、Squibler Story Elements | **普遍** | [官方文档 1/2][官方页面 12][官方文档 4] |
| 场景/节拍显式挂角色 | 场景卡上打角色标签，可按角色筛选场景 | Plottr、Penpoint、Arc Studio | **普遍** | [官方页面 9/10][官方页面 13][第三方评测] |
| POV 绑定到具体角色 | POV = 人称 + 一个角色条目，可按全书或按场景设置 | Novelcrafter | **中等**（多数工具只有人称） | [官方指南 6] |
| 只注入「当前场景相关」条目 | 靠正文里提到名字/@提及/关系链拉取，而非全量塞入 | Novelcrafter Mentions/Relations、Sudowrite Story Bible | **普遍** | [官方文档 4][官方指南 8] |
| 角色条目字段分层 | 身份/别名/代词/分组/性格/背景/外貌/说话风格 | Sudowrite 默认卡字段 | **普遍** | [官方文档 1] |
| 关系图/关系条目 | 角色之间的关系单独建模、可视化 | Campfire Relationships、番茄作家助手人物关系 | **常见（偏设定展示）** | [官方页面 11][官方文档 14] |
| 角色弧线追踪 | 按章/场景记录角色的起点、转折、终点 | Campfire Arcs、Plottr plotlines | **个别** | [官方页面 11][官方页面 10] |
| 场景级角色「增量」回流 | 角色在某场景后发生变化，增量挂在该场景，之后才进上下文 | Novelcrafter Codex Progressions | **个别（但最值得借鉴）** | [官方文档 5] |
| 自动一致性校验/矛盾检测 | 生成后检查角色言行是否矛盾 | Squibler 宣称「contradictions are caught early」 | **个别 / 未证实** | [官方页面 12]（营销口径，未见文档细节） |
| 出场统计与按角色筛选 | 统计出场章节/场景，支持筛选 | Plottr Filter、本项目台词行统计 | **普遍** | [官方页面 10] |

### 1.2 角色条目通常包含哪些字段

**Sudowrite 角色卡**（官方默认字段，可直接对照）：
Pronouns（代词）、Groups（分组）、Other Names（别名）、Personality（性格）、Background（背景）、Physical Description（外貌）、Dialogue Style（说话风格）。角色卡还支持历史版本、角色定位下拉、图片画廊。[官方文档 1]

**Novelcrafter Codex**：
条目有类型（角色/地点/物品/设定等），类型会影响系统能力（例如只有「角色」类型才能被设为 POV）；条目之间可建 Codex Relation，父条目被提及时会带出嵌套引用；另有 Global Entry（永远注入）。[官方文档 4]

**Campfire**：
Characters 模块之外，Relationships 负责关系映射，Arcs 负责角色随幕/章的变化轨迹。[官方页面 11]

**Plottr**：
角色可自定义字段（发色、能力等），并可把角色关联到时间线上的场景卡，从而按角色过滤场景。[官方页面 9/10]

**中文侧**：
番茄作家助手有「AI 人物设定」，生成人物卡片（可改、可换一换、可换图），并支持「新建人物关系」（输入人物姓名 + 选择关系，可自定义）。[官方文档 14] 但公开文档未说明人物卡会自动注入正文生成——**[推断]** 目前更偏设定展示与人工参考。
橙瓜码字的功能清单里没有角色卡，主要是取名、大纲助手、备份、敏感词等。[官方页面 15]
有声制作侧（魔音工坊、讯飞）以「音色/发音人」为单位，支持声音复刻与多发音人，但**不承载小说角色语义**；角色到音色的映射仍需人工维护。[官方页面 16/17]**[推断]**

### 1.3 三个值得单独记住的做法

**（1）POV 是「人称 + 角色」，不是只有人称**
Novelcrafter 的 POV 设置由「第几人称」+「一个 Codex 角色」组成（全知视角除外），可按全书或按场景设置，并进入后台提示词。[官方指南 6]
→ 对照本项目：`style.pov` 只有 `first/second/third_limited/third_omniscient`，`pov_character_id` 存在但不进提示词（见 §2.2、§3-A1）。

**（2）节拍要写清「谁在场」**
Novelcrafter 的 Beats 指南明确要求：点名角色（不要用代词，AI 会搞混）、写动作、写对话主题、写地点变化。[官方指南 7]
→ 对照本项目：节拍有 `character_ids`，但生成正文时既不给模型看名单，也不校验实际出场。

**（3）角色变化要「按场景增量」而不是「改总卡」**
Novelcrafter 的 Codex Progressions：如果角色中途被附身、脸上多了疤，不要直接改总卡（否则 AI 会在前文提前提到），而是把增量挂到当前场景，只有写该场景及之后的场景时才会看到；支持 replace / addition 两种模式。[官方文档 5]
→ 这是「写作 → 角色」回流最成熟的形态，也最适合本项目的章节/节拍结构。

### 1.4 行业普遍做法 vs 个别做法（给决策用）

- **普遍**：独立角色卡、写作自动注入、场景挂角色、按角色筛选/出场统计、只注入相关条目。
- **少数**：场景级角色增量回流、角色弧线、结构化关系图、自动矛盾检测。
- **结论**：本项目不需要发明新范式。**先把「普遍做法」补齐，再决定要不要做「个别做法」。**

---

## 2. 本项目现状：角色信息实际怎么流动

### 2.1 数据落点

| 信息 | 落点 | 关键字段 | 写入方 | 读取方（写作相关） |
|---|---|---|---|---|
| 角色静态档案 | `characters.json` | `name/aliases/role/gender/age_range/personality/background` | 人物生成、角色页 PATCH | `text/context.py` |
| 说话风格 | `characters.json` | `speech_style.tempo/pitch/accent/catchphrases` | **无**（仅默认空值） | `text/context.py`（只取 tempo/pitch） |
| 音色语言特征 | `characters.json` | `voice_description_zh/en` | 人物生成、AI 补全 | **仅 TTS**，写作不读 |
| 角色状态 | `characters.json` | `state.situation/knowledge/as_of_chapter_id` | **无**（CLI 仅支持 situation） | **写作不读** |
| 关系备注 | `characters.json` | `state.relationship_notes: {目标: 备注}` | 人物生成（键=姓名）、CLI（键=角色 ID） | `text/context.py`（原样渲染） |
| 计划选角 | `settings/bible.json` | `outline.acts[].chapters[].beats[].character_ids` | 大纲生成、`outline bind`、sanitize | 仅用于**过滤角色卡** |
| 章节 POV | bible + `chapters/<id>.json` | `pov_character_id` | bible：`outline bind`；chapter：**无写入方** | 仅用于过滤与 metadata |
| 本章世界 | bible | `world_ids` | 大纲生成、`outline bind` | 注入世界摘要 |
| 实际出场 | `chapters/<id>.json` | `scenes[].character_ids`、`lines[].speaker_id` | 切分/标注流水线 | 角色统计、配音、出图 |

Schema 依据：
`character.schema.json` 角色字段与 `state`、`speech_style` 定义（L15–L115）；
`setting-bible.schema.json` 章 `pov_character_id`/`world_ids`（L63–L81）、节拍 `character_ids`（L98–L108）；
`chapter.schema.json` 章 `pov_character_id`（L17）；
`scene.schema.json` 场景 `character_ids`（L20–L24）。

### 2.2 写作时的上下文装配（单章生成）

调用链：`generate_chapter` → `build_chapter_context` → `ContextAssembler.build`。

1. **选角过滤**：只取「本章 POV + 所有节拍 `character_ids`」的并集作为 `speaker_ids`；找不到时保留全量角色。
   `writing/service.py:719-730`
2. **角色卡渲染**：只渲染 `name/role/gender/age_range/aliases/personality/background/speech(tempo+pitch)/relationship_notes`。
   `text/context.py:199-236`、`text/context.py:238-269`
3. **本章大纲渲染**：只渲染章标题、章摘要、节拍 summary、目标字数、转折标记；**不渲染 POV 角色、不渲染每个节拍的 `character_ids`**。
   `text/context.py:326-360`
4. **写作准则**：固定文案要求「人物言行必须符合其性格、背景、说话方式与关系备注」。
   `text/context.py:448-458`
5. **正文指令**：只给「视角：第三人称有限视角；基调：…」，没有 POV 角色名；节拍行只有 summary/目标字数/转折点。
   `writing/service.py:904-944`
6. **多轮写作**：system 与首条 user 全程不变，只追加 assistant 正文和本轮节拍范围说明；本轮说明里也没有角色。
   `writing/service.py:1146-1169`、`writing/service.py:1276-1290`

### 2.3 写作产物的回流

| 产物 | 是否回流 | 证据 |
|---|---|---|
| 章节摘要 | 回写 `chapter.synopsis` | `writing/recap.py:149-152` |
| 全书滚动摘要 | 回写 `.ndw/rolling_summary.json` | `writing/recap.py:154-167` |
| 节拍选角 | **不更新**；`_sync_beats` 明确「无法从正文可靠推断，沿用同位置旧值，新增节拍用 POV」 | `writing/summarize.py:301-336` |
| 角色状态 `state.*` | **不更新**；全项目无写入 `situation/knowledge/as_of_chapter_id` 的生成链路 | 全库检索仅 CLI `--situation` |
| 角色关系 | **不更新** | 同上 |
| 实际出场 | 由切分结果派生，不落盘；统计接口每次现算 | `web/app.py:4340-4374` |

补充：`docs/data-model.md:324` 写的是「`state`：角色当前处境/关系/已知信息，**章节完成后更新**，供续写与一致性检查」——**文档承诺了，代码没实现**。

### 2.4 角色页与章节/画本现有交互

| 方向 | 现状 | 证据 |
|---|---|---|
| 角色页 → 章节 | 有。「查看出场」弹窗按章列台词行数，点击 `goChapter` 跳画本 | `web-next/js/pages/characters.js:670-697` |
| 章节/画本 → 角色 | **无**。录音右栏「角色档案」是静态行，不可点；标题写「查看本章出场角色」，实际渲染的是全部角色 | `web-next/js/pages/studio-views.js:1032-1070` |
| 大纲章 → 角色 | 只有「N 人」徽章，不显示姓名，不可编辑 | `web-next/js/pages/novel.js:1028` |
| 大纲章 → POV | **无 UI**；后端有 `/api/ui/outline/bind` 但前端未使用 | `web/app.py:5185-5209` |
| 角色页编辑 | 姓名/别名/定位/性别/年龄段/性格/背景/说话风格（只读）/外貌/负向共 10 项；`situation`/`relationship_notes`/`knowledge` 无编辑入口（API 已支持 `situation` 与 `relationships`） | `characters.js:448-495`、`web/app.py:4253-4306` |
| 角色页「说话风格」 | **只读**，提示「由音色与行内参数决定」——但写作侧没有任何地方生成它 | `characters.js:201-208`、`characters.js:481-483` |

---

## 3. 脱节点清单

### A. 角色信息在写作时「用不上」

| # | 脱节点 | 具体表现 | 证据 |
|---|---|---|---|
| A1 | POV 角色身份不进提示词 | `pov_character_id` 只参与过滤和 `metadata`；指令里只有人称「第三人称有限视角」 | `writing/service.py:719-730`、`writing/service.py:919` |
| A2 | 节拍选角不进提示词 | 节拍 `character_ids` 只用于收集 `speaker_ids`；节拍行只有 summary/字数/转折 | `writing/service.py:904-918`、`text/context.py:343-357` |
| A3 | 多轮写作全程共享同一角色集合 | system 固定，后段节拍的角色可能在第 1 轮就登场 | `writing/service.py:1276-1290` |
| A4 | `speech_style` 永远是空 | 全库只有默认值 `{null,null,null,[]}` 与读取方，没有任何写入方（人物生成、AI 补全都不产出）；提示词里的「说话方式」因此从不出现 | `models/entities.py:87`、`character_plan.py:444-475`、`text/context.py:216-218` |
| A5 | 音色语言特征只服务配音 | `voice_description_zh/en` 含语速、语气、年龄感、口音，但写作上下文不读，只有在 voice design / casting 里用 | `text/context.py:199-236` |
| A6 | 角色状态字段全空 | `state.situation/knowledge/as_of_chapter_id` 既不注入也不写入 | `text/context.py:220`、`web/app.py:4287` |
| A7 | 关系备注渲染原始键 | `_render_cards` 直接 `f"{key}：{value}"`；人物生成时键是**姓名**，CLI 编辑时键是**角色 ID**，混用且可能把 `chr_xxx` 写进提示词（违反项目「角色 ID 不得直接展示」约定） | `text/context.py:266-267`、`cli_character.py:304-315`、`service.py:3338-3362` |
| A8 | 大纲选角没有人物依据 | 大纲生成只把 `id/name/role/aliases` 给模型，性格、关系、背景都不参与选角；`auto` 绑定按章轮转 POV、按章轮转世界 | `service.py:158-171`、`service.py:3574-3616` |

### B. 写作过程中产生的信息「不回流」

| # | 脱节点 | 具体表现 | 证据 |
|---|---|---|---|
| B1 | 角色状态不更新 | 生成完成后只写章节摘要与全书摘要；`state.*` 永远停在初始值 | `recap.py:149-167` |
| B2 | 计划选角不校正 | 正文实际出场与节拍选角无关；重新总结章节时也刻意保留旧 `character_ids` | `summarize.py:301-336` |
| B3 | 关系变化不记录 | 正文里结盟/反目不会写回 `relationship_notes` | 无写入链路 |
| B4 | 新角色/新称呼不捕获 | 切分后落入 `chr_unknown` 或需要手工建卡；没有「从正文提取未建卡角色」的入口 | `pipeline.py:129`、`cli_character.py:374-404` |
| B5 | 出场统计维度单一 | 只按台词行统计；角色只在旁白里被提到、或只在场景 `character_ids` 里出现时，会显示「还没有出场记录」/统计偏低 | `web/app.py:4340-4374`、`characters.js:404-407` |

### C. 必然出现不一致的地方

| # | 问题 | 机制 | 证据 |
|---|---|---|---|
| C1 | POV 双源，其中一份是死数据 | bible 章有 `pov_character_id`，chapter 文件也有；`new_chapter` 永远写 `None`，页面无写入方；运行期优先读 bible。画本侧因此看不到 POV | `setting-bible.schema.json:73`、`chapter.schema.json:17`、`models/entities.py:233`、`service.py:769` |
| C2 | 删除角色留下悬空引用 | 删除前只检查台词行引用（`character_refs`），不检查大纲节拍/POV/关系备注；删完后 `sanitize_outline_character_ids` 会静默丢弃，或 `focus` 为空回落到全量角色 | `cli_character.py:340-404`、`service.py:3619-3659` |
| C3 | 页面生成不做绑定校验 | `validate_chapter_binding` 只在 CLI `writing` 调用；页面生成没有这一步，缺 POV/缺角色也能生成 | `cli_writing.py:134`、`service.py:3707-3762`（无 web 调用点） |
| C4 | 计划与实际无对账 | 大纲徽章「N 人」与正文实际出场无任何校验，长期漂移无人发现 | `novel.js:1028`、`service.py:3619-3686` |
| C5 | 已绑定音色显示未绑定 | 角色页头部 `voiceLine` 只认 `voice_id/ref_audio`，不认 v2 的 `voice_library_id` | `characters.js:211-216` |
| C6 | 「本章出场角色」名不副实 | 录音右栏渲染 `state.characters` 全量列表，却标注「查看本章出场角色」 | `studio-views.js:1032-1070` |

### D. 交互断点

| # | 断点 | 影响 |
|---|---|---|
| D1 | 章节/画本无法一键跳到角色档案 | 写作时想核对人设要手动切页 + 搜索 |
| D2 | 大纲无法编辑 POV 和节拍选角 | 选角只能靠自动绑定或 CLI，作者无法在大纲阶段做叙事决策 |
| D3 | 角色页无「当前处境」「关系」编辑 | 即使后端支持，作者也维护不了续写要用的状态 |
| D4 | 角色页无法从「章节大纲」深链 | 只能跳画本，不能跳「第 3 章大纲」 |
| D5 | 角色页说话风格只读且无生成入口 | 最关键的口吻约束字段长期为空 |

---

## 4. 最小可行方案

### 4.1 设计原则（先定边界）

1. **单一事实源**：角色静态档案只在 `characters.json`；计划选角只在 `bible.outline`；实际出场只在 `chapter.scenes/lines`。不把角色信息复制进 bible。
2. **派生优先**：出场统计、计划/实际对账都做成「读时派生」，不新增落盘字段，避免数据过期。
3. **写作前注入、写作后对账、变化需确认**：不自动改大纲、不自动改正文、不静默改人设。
4. **提示词里永不出现 `chr_`**：所有 ID 渲染前必须解析成姓名，解析失败显示「已删除角色」。
5. **不为未上线状态写兼容分支**：可直接统一关系键为 ID、可直接给 schema 加字段。

### 4.2 数据怎么挂

| 信息 | 方案 | 字段变化 |
|---|---|---|
| 角色静态档案 | 保持 `characters.json` 唯一源 | 无 |
| 计划出场 | 保持 `bible.outline...beats[].character_ids`，新增 UI 可编辑 | 无 |
| 本章 POV | `bible` 为主源；修改时**写通**到 `chapter.pov_character_id`（含 revision+1），运行期仍优先读 bible | 无（消灭双源分叉） |
| 实际出场 | 由 `scenes[].character_ids` + `lines[].speaker_id` 派生，不落盘 | 无 |
| 角色状态 | 写入既有 `state.situation` / `state.knowledge` / `state.relationship_notes` / `state.as_of_chapter_id`，确认后应用 | 无 |
| 说话风格 | 让 `speech_style` 成为**文字口径**的口吻字段（语速/音高/口音/口头禅/称呼习惯） | 无 |
| 结构化关系（P2） | 新增 `state.relations: [{target_id, label, sentiment, since_chapter_id, note}]` | 新增 |
| 角色弧线（P2） | 新增 `bible.outline...character_arcs: [{character_id, from_state, to_state, note}]`（挂章或节拍） | 新增 |
| 对账结果 | 接口实时计算，不落盘 | 无 |

**说话风格与音色描述的分工**（避免重复造两份）：

- `speech_style`：给文字看。例：`tempo=偏慢`、`pitch=低沉`、`accent=北方口音`、`catchphrases=["有意思","别急"]`。
- `voice_description_zh/en`：给 TTS 看。音质、气声、年龄感、参考音风格。
- 两者都允许为空；为空时提示词不输出该行。

### 4.3 角色页 ↔ 章节/画本互跳

**路由与接口（尽量复用现有）**

| 跳转 | 实现 |
|---|---|
| 角色 → 画本章节 | 现有「查看出场」弹窗保留，点击 `goChapter(path, chapterId)` |
| 角色 → 大纲章 | 新增 `#/p/{path}/novel?chapter={chapterId}`；`main.js` 解析 query，`novel.js` 初始化 `ui.sel` |
| 画本 → 角色 | 新增顶部绑定条：`POV：沈砚` + `本章出场：沈砚、林墨、+2`；每个 chip 链到 `#/p/{path}/characters?character={id}`（现有路由已支持） |
| 录音右栏 → 角色 | 角色行改为可点击链接，直达角色档案 |
| 大纲节拍 → 角色 | 节拍角色 chip 可点；旁边「+ 添加」就地选人 |
| 角色页 → 计划出场 | 出场弹窗分「计划出场（大纲节拍）」「实际出场（场景/台词）」两组 |

**新增读接口**（一个就够，避免前端拼三个接口）：

```
GET /api/ui/chapters/binding?path=...&chapter=...
→ {
    pov: {id, name} | null,
    beats: [{beat_id, index, summary, characters: [{id, name, role}]}],
    characters: [{id, name, role, planned: true, lines: 12, scenes: 3}],
    audit: {
      planned_not_used: [{id, name}],
      unplanned_appeared: [{id, name, source: "line"|"scene"}],
      pov_not_in_prose: true,
      missing_refs: [{kind: "beat"|"pov"|"relationship", id}]
    }
}
```

**新增写接口**：

```
POST /api/ui/outline/beat/characters
{path, chapter, beat, character_ids: ["chr_..."]}
→ 校验 ID 均存在且非旁白；去重；保存；返回最新绑定
```

POV 编辑直接复用现有 `POST /api/ui/outline/bind`，并让它同时写通 `chapter.pov_character_id`。

### 4.4 写作时怎么注入与校验

#### （1）注入模板（改渲染，不改数据）

角色卡（每张）：

```
- 沈砚（主角，男，28）
  别名：三郎
  性格：克制、记仇
  背景：……
  当前处境：被逐出师门，暂居城南
  说话方式：语速偏慢，音高偏低；北方口音；口头禅「有意思」
  已知信息：师父已死；不知道弟弟还活着
  关系：林墨（旧友）——曾并肩守城；赵铎（仇敌）——杀父之仇
```

本章大纲：

```
【本章大纲与节拍】
章节：第 3 章《夜雨》
视角人物：沈砚（第三人称有限视角）
章节摘要：……
节拍：
1. 沈砚在雨夜发现密信（目标 800 字，出场：沈砚）
2. 与林墨对峙，试探其立场（目标 900 字，转折点，出场：沈砚、林墨）
```

强制规则（写进渲染函数，可单测）：

- 任何 `chr_` 前缀字符串不得进入 system/user；`relationship_notes` 的键必须先解析成姓名，失败显示「已删除角色」。
- POV 卡永不裁剪；其他角色按「计划出场次数」降序排列。
- 预算裁剪新增一档：先删 `knowledge`，再删背景，再删性格/口吻；`situation` 与 POV 卡最后删。

#### （2）校验时机与错误码

| 时机 | 校验 | 行为 |
|---|---|---|
| 生成前 | 本章绑定完整性 | `BINDING_CHARACTER_MISSING`（error，阻断，带「去人物页」链接）；`BINDING_POV_MISSING`（warn，不阻断，提示将默认跟随主角）；`BINDING_BEAT_EMPTY`（warn）；`BINDING_WORLD_MISSING`（保持现有阻断） |
| 落盘前 | 现有 `content_guard` + 引号规范化 | 不变 |
| 落盘后 | 计划 vs 实际对账 | `PLAN_UNPLANNED_CHARACTER`（warn，给「加入本章计划」按钮）；`PLAN_UNUSED_CHARACTER`（info）；`POV_NOT_IN_PROSE`（warn）；`CHARACTER_STATE_STALE`（info） |

页面表现：画本顶部一条「绑定检查」条，正常为绿色摘要，异常展开成可点击列表。**所有校验只提示/阻断生成，不自动改数据。**

#### （3）写作后回流（P1）

在 `update_rolling_summary` 之后追加一次结构化调用：

```
输入：本章摘要 + 本章正文（截断）+ 本章计划角色 + 各角色当前 state
输出 JSON：
{
  "updates": [
    {
      "character_id": "chr_...",
      "situation": "被逐出师门，暂居城南",
      "knowledge_add": ["师父已死"],
      "relationships": [{"target_id": "chr_...", "note": "结盟", "mode": "set"}]
    }
  ]
}
```

行为：

1. 预览存 `.ndw/character_updates/<chapter>.json`，刷新页面不丢。
2. 角色页/章节页显示「本章角色变化 3 条」，逐条勾选后应用。
3. 应用只写 `state.situation/knowledge/relationship_notes/as_of_chapter_id`，不碰正文、不碰大纲。
4. 应用后把 `state.as_of_chapter_id` 设为本章 ID，后续生成可提示「角色状态停留在第 N 章」。

### 4.5 分档落地路线

#### P0（低投入，优先做；只改渲染、派生与入口跳转）

| 项 | 改动点 | 验收（可直接测） |
|---|---|---|
| P0-1 POV 具名 + 节拍出场 | `text/context.py` 的 `chapter_outline`、`_character_models`、`_render_cards`；`writing/service.py` 的 instruction | 生成请求中：出现「视角人物：X」；每个节拍含「出场：A、B」；全文无 `chr_` |
| P0-2 关系键解析姓名 | `_render_cards` 增加 ID→姓名映射；未知键显示「已删除角色」 | 手工把 `relationship_notes` 的键改成 `chr_xxx`，生成请求里显示姓名而非 ID |
| P0-3 卡片补字段 + 裁剪顺序 | `_character_models` 增加 `catchphrases/accent/situation/knowledge`；`build` 的裁剪循环增加 knowledge 档 | 卡片出现「当前处境」「口头禅」；超预算时先消失的是「已知信息」 |
| P0-4 让口吻有数据 | `character_plan.fill_character_descriptions` 与人物生成 schema 增加 `speech_style`；角色页该字段改为可编辑（或「AI 补全」一并生成） | 新建角色后 `speech_style` 非空；生成请求出现「说话方式：…」 |
| P0-5 出场统计扩展 | `GET /api/ui/characters/stats` 增加 `scenes` 与 `planned`；角色页弹窗分组 | 只在旁白出现/只在场景 `character_ids` 出现的角色不再显示「还没有出场记录」 |
| P0-6 双向跳转 | 画本顶部绑定条；录音右栏角色行可点；角色页「计划出场」跳 `novel?chapter=` | 画本点 POV 打开对应角色档案；角色页点计划条目打开对应章大纲 |
| P0-7 生成前校验 | 页面生成入口调用 `validate_chapter_binding`；缺 POV 仅告警 | 删掉某章全部节拍角色后点生成：弹出中文阻断提示，而不是照常生成 |

#### P1（中投入，形成闭环；2–4 天）

| 项 | 改动点 | 验收 |
|---|---|---|
| P1-1 大纲可编辑 POV / 节拍角色 | `outline/text` 支持节拍 `character_ids`；`outline/bind` 写通 chapter；novel 章详情加两个控件 | 在大纲页改 POV 后，画本顶部同步变化；节拍角色可增删 |
| P1-2 角色页补「当前处境」「关系」编辑 | 复用 `PATCH /api/ui/characters` 的 `situation` / `relationships` / `removed_relationships` | 编辑后生成请求出现对应内容；删除关系行后服务端不残留 |
| P1-3 写作后角色状态回流（预览+确认） | 新增 `propose_character_updates` + `.ndw/character_updates/` + 应用接口/面板 | 生成完一章出现「本章角色变化」，勾选应用后 `state.as_of_chapter_id` 更新 |
| P1-4 计划 vs 实际对账 | `GET /api/ui/chapters/binding` 的 `audit`；画本提示条；「加入本章计划」 | 写一章里多出一个计划外角色，提示可一键写回节拍选角 |
| P1-5 删除角色联动检查 | `character_refs` 扩展到大纲 POV/节拍/关系备注；删除前展示全部引用 | 角色仍被大纲引用时拒绝删除并列出章节 |

#### P2（高投入，按需）

| 项 | 说明 |
|---|---|
| P2-1 结构化关系 + 关系图 | `state.relations`；角色页关系图；按章节回放关系变化 |
| P2-2 角色弧线 | 大纲生成阶段三产出 `character_arcs`；角色页时间线；写作时注入「本章目标状态」 |
| P2-3 生成后 LLM 一致性检查 | 输出结构化 issues（口吻偏离/已知信息越界/关系矛盾/计划外出场），分级提示，不自动改写 |
| P2-4 未建卡角色捕获 | 切分后识别未建卡姓名，一键从上下文生成角色卡并补进本章计划 |

### 4.6 优先级理由

- **P0 全是「把已有数据接上」**：不新增字段、不改 schema、不动生成主流程，但能直接消掉 A1/A2/A4/A7、B5、D1/D5 这些最影响体感的断点。
- **P1 才引入新行为**（可编辑、回流、对账），每一项都能单独验收、单独回滚。
- **P2 是增强**：关系图和弧线有价值，但不是「脱节」的主因；先别做，避免过度设计。

---

## 5. 明确不做的事

1. 不把角色档案复制进 `bible.json`，避免第三份事实源。
2. 不为出场统计新增落盘字段；统计永远从 `scenes/lines` 现算。
3. 不在写作后自动改节拍选角、自动改正文、自动改人设；一律预览 + 确认。
4. 不为了「一致性」引入第二套 LLM 改写链路；P2 的检查只输出问题。
5. 不给旧工程写迁移脚本（项目未上线，按新代码唯一实现）。

## 6. 需要确认的决策

1. **POV 默认值**：POV 未设置时，是「阻断生成」还是「警告并默认跟随主角」？（建议后者）
2. **关系键统一**：是否直接把 `relationship_notes` 的键统一为角色 ID，并把现有姓名键一次性改掉？（建议是，未上线）
3. **说话风格来源**：`speech_style` 由人物生成阶段一次产出，还是只由「AI 补全」按需生成？（建议前者，和现有「人物生成一次产全」的约束一致）
4. **状态回流时机**：生成后立即预览、作者确认；还是只在作者点「更新角色状态」时才算？（建议前者，但必须确认才写）
5. **P2 是否现在排期**：如果当前目标是「先让角色和正文看起来是一套东西」，P2 可以整体延后。

---

## 附录：证据索引（本项目）

| 主题 | 位置 |
|---|---|
| 角色字段与 state / speech_style | `schema/character.schema.json:15-115` |
| 章 POV / world_ids / 节拍 character_ids | `schema/setting-bible.schema.json:63-108` |
| 章 POV 副本 | `schema/chapter.schema.json:17` |
| 场景 character_ids | `schema/scene.schema.json:20-24` |
| 角色卡渲染与裁剪 | `src/novel_drama/text/context.py:199-269`、`:326-360`、`:416-458`、`:481-527` |
| 上下文构建与选角过滤 | `src/novel_drama/writing/service.py:685-790` |
| 正文指令与多轮 | `src/novel_drama/writing/service.py:904-958`、`:1146-1169`、`:1243-1314` |
| auto 绑定（按章轮转） | `src/novel_drama/writing/service.py:3574-3616` |
| 非法引用清洗 | `src/novel_drama/writing/service.py:3619-3659` |
| 绑定校验（仅 CLI 调用） | `src/novel_drama/writing/service.py:3707-3762`、`cli_writing.py:134` |
| 节拍选角不回写 | `src/novel_drama/writing/summarize.py:301-336` |
| 只回写摘要 | `src/novel_drama/writing/recap.py:149-167` |
| 正文有效性校验 | `src/novel_drama/writing/content_guard.py` |
| 人物补全字段 | `src/novel_drama/writing/character_plan.py:444-475` |
| 关系/处境 CLI | `src/novel_drama/cli_character.py:298-327` |
| 删除角色只查台词 | `src/novel_drama/cli_character.py:340-404` |
| 角色 PATCH 接口 | `src/novel_drama/web/app.py:4253-4306` |
| 角色引用接口 | `src/novel_drama/web/app.py:4308-4321` |
| 角色出场统计 | `src/novel_drama/web/app.py:4340-4374` |
| 章节目录角色维度 | `src/novel_drama/web/app.py:1950-2050` |
| POV/世界绑定接口 | `src/novel_drama/web/app.py:5185-5209` |
| 角色页表单与出场弹窗 | `web-next/js/pages/characters.js:201-216`、`:391-497`、`:670-697` |
| 录音右栏角色列表 | `web-next/js/pages/studio-views.js:1026-1070` |
| 大纲章详情与保存 | `web-next/js/pages/novel.js:1012-1075`、`:1696-1728` |
| 前端路由 | `web-next/js/main.js:30-57` |
| 文档承诺「章节完成后更新 state」 | `docs/data-model.md:324` |

## 行业来源

1. [Sudowrite Characters（官方文档）](https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/characters/a7tdE1ZB8KvAwMD3Mopwpd)
2. [Sudowrite What is Story Bible（官方文档）](https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/what-is-story-bible/jmWepHcQdJetNrE991fjJC)
3. [Sudowrite 完整指南（第三方评测）](https://www.clarigital.com/ai-atlas/specialist-tools/writing-content/sudowrite/)
4. [Novelcrafter Glossary / Codex（官方文档）](https://www.novelcrafter.com/help/reference/terms/novelcrafter)
5. [Novelcrafter Progressions/Additions（官方文档）](https://www.novelcrafter.com/help/docs/codex/progressions-additions)
6. [Novelcrafter POV and Prologues（官方指南）](https://www.novelcrafter.com/courses/ultimate-beginners-guide/prologue-and-pov)
7. [Novelcrafter What are beats（官方指南）](https://www.novelcrafter.com/courses/beats-cookbook/what-are-beats)
8. [Novelcrafter 提升场景节拍输出（官方指南）](https://www.novelcrafter.com/courses/ultimate-beginners-guide/improving-ai-output-for-scene-beats)
9. [Plottr 2.0 Characters（官方页面）](https://plottr.com/news/introducing-plottr-2)
10. [Plottr Timeline / 按角色筛选（官方页面）](https://plottr.com/timeline-creation/)
11. [Campfire Character Builder / Arcs / Relationships（官方页面）](https://www.campfirewriting.com/character-builder)
12. [Squibler Novel Writing Software（官方页面）](https://www.squibler.io/novel-writing-software/)
13. [Penpoint Assigning Things to Scenes（官方文档）](https://penpoint.app/help/assigning-things-to-scenes)
14. [番茄小说 AI 人物设定功能使用说明（官方文档）](https://fanqienovel.com/writer/zone/article/7361736120746393662)
15. [橙瓜码字 关于我们（官方页面）](https://chenggua.com/about-us/index/index.html)
16. [讯飞 超拟人语音合成（官方页面）](https://www.xfyun.cn/services/smart-tts)
17. [魔音工坊（官方页面）](https://www.moyin.com/)
