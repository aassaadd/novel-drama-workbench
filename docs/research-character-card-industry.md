# 同类产品如何产出与维护角色卡：行业调研

> 调研时间：2026-11-15。只做调研，不改代码。
> 证据分级：**[官方文档]** 产品帮助中心/官方说明；**[官方研究]** 官方研究博客或论文；**[产品宣称]** 官网/官方账号/发布会口径；**[论文]** 同行评审或预印本；**[第三方]** 第三方评测/教程；**[推断]** 依据公开信息推断、未见明确说明。
> 范围：写作类（Sudowrite、Novelcrafter Codex、Plottr、Campfire、Squibler、Scrivener 生态）、网文侧（番茄，附阅文）、有声侧（Audible BookLLM、喜马拉雅音剪 AI、万象有声）。

---

## 一、结论速览

| 问题 | 行业主流做法 | 代表产品 | 证据 |
| --- | --- | --- | --- |
| 1. 建卡时机 | 两种：①**计划期一次性建卡**（先 Synopsis 再一次生成全部角色）；②**人工随手建 Codex**，写作中靠提及高亮发现缺口。另有有声侧「画本阶段自动识别角色」、以及正文后的「逐章抽取事实」。**没有写作类产品把建卡放在"每章节拍生成后"这一粒度** | Sudowrite（一次性）、Novelcrafter/Plottr/Campfire/Scrivener（人工）、万象有声/喜马拉雅（有声画本阶段）、Novarrium（正文后逐章） | [官方文档] [官方研究] [产品宣称] |
| 2. 粒度与成本 | 建卡成本按「**一次全书/一次概要**」摊，而不是按章。逐章调模型的只有「正文后事实抽取」与学术管线；写作工具靠**字符串提及检测**代替逐章调用 | Audible BookLLM（整本一次）、Sudowrite（一次 Synopsis）、Novarrium（逐章抽取，宣称） | [官方研究] [官方文档] [产品宣称] |
| 3. 去重机制 | 主流是**人工维护别名 + 确定性匹配**（大小写、复数、排除词），模型只做候选识别；模型自动合并时会配**人工校验/合并入口** | Novelcrafter（别名+排除表+大小写）、Sudowrite（Other Names + 导入校验）、万象有声（自动识别 + 手动合并别名） | [官方文档] [产品宣称] |
| 4. 卡与大纲/场景绑定 | 写作工具绑到**场景（或场景节拍）**，通过"检测到才注入"控制上下文；有声工具绑到**剧本台词行** | Novelcrafter、Sudowrite、Plottr、万象有声 | [官方文档] [产品宣称] |
| 5. 状态回写 | 主流是**人工/场景锚定**：写作工具用「场景进度（Progression）」手动记录，AI 只从该场景起可见；自动回写只出现在宣称"自动抽取"的新产品与研究里 | Novelcrafter（人工 Progression）、Novarrium（自动，宣称）、CharacterGPT/Remember Me（论文） | [官方文档] [产品宣称] [论文] |
| 6. 遗漏兜底 | ①提及高亮/热力图找缺口；②导入前人工校验；③小角色直接回退旁白，不建卡；④正文后抽取+一致性检查 | Novelcrafter、Sudowrite、Audible、Novarrium | [官方文档] [官方研究] [产品宣称] |

**一句话结论**：行业普遍把「角色卡」当成**计划期的一次性资产 + 写作期的检索索引**；只有少数新产品/论文在正文后做逐章抽取。本项目「每章节拍后一次角色设定调用」比行业更前置、更细，但也更贵，属于行业里少见的第三条路径。

---

## 二、写作类产品

### 2.1 Sudowrite（Story Bible）

- **建卡时机**：Story Bible 的主链路是 Braindump → Synopsis → **Characters** → Outline → Scenes → Draft。角色由 **Synopsis 一次性生成**（Generate All Characters），也可以单张生成（Generate Character / Surprise Me）。Synopsis 里明确点到的人会自动纳入，点得少时 AI 会补配角。[官方文档]
- **粒度与成本**：一次概要一次调用，不按章建卡。另提供 **Smart Import**：从文本/文件导入，单次最多 60,000 词、最多 30 个角色，导入不消耗 credits；CSV 结构化导入可跳过 AI。[官方文档]
- **去重机制**：卡片带 **Other Names** 字段（昵称、别名、化名）供 AI 识别；Smart Import 在导入前有一步**人工校验**，可以拒绝把"化名/昵称"识别成独立角色——这是"模型识别 + 人工确认"的典型组合。[官方文档]
- **绑定方式**：Scene / Prose 生成**只读取被明确提及的角色**（"Scene and Prose Generation will only look at explicitly mentioned Characters and Worldbuilding elements"）。角色卡直接影响 Outline、Scenes、Draft。[官方文档]
- **回写**：卡片有历史版本可回看，也有 Chat 可以修改 Story Bible；但公开文档没有"正文后自动回写状态"的说明——**按公开信息判断是人工维护**。[官方文档] [推断]
- **遗漏兜底**：靠"只读被提及的角色"+ 卡片与 Scene 的显式提及控制，没有逐章捕获机制。[官方文档]

### 2.2 Novelcrafter（Codex）

- **建卡时机**：**人工建 Codex 条目**，写作/规划时自动做**提及检测**并在正文下划线、生成热力图。官方提示：写作时保持 tracking 开启，可以"spot missing Codex entries"——即用可视化发现漏建卡，而不是自动建卡。[官方文档] [推断：未文档化自动建条目]
- **粒度与成本**：不按章调模型建卡；成本主要来自"检测到才注入"的上下文 token。官方还专门给了描述长度建议：**次要角色 1–3 句**即可，降低 token 消耗、避免 AI 过度引用。[官方文档]
- **去重机制**：**别名 + 昵称 + 头衔**人工维护；默认大小写不敏感，可切大小写敏感；英文自动匹配规则复数，不规则复数需手加别名；支持**排除词表**（如角色名 Will 排除助动词 will）。[官方文档]
- **绑定方式**：Codex 条目可在正文、**场景节拍（scene beats）**、聊天中被检测；AI Context 四档：Always include / Include when detected（默认）/ Don't include when detected / Never include。Relations 支持条目分组与嵌套，提及一个条目会带出关联条目（如"五人议会"带出五张角色卡）。[官方文档]
- **回写**：**Codex Progressions**（Additions / Replacements）绑定到具体场景，**AI 只在该场景及之后才看到**；用于伤疤、婚变、职位变化等，不建议拿来替代剧情点。[官方文档]
- **遗漏兜底**：提及高亮 + 热力图 + 提及列表（正文/场景摘要/Codex/片段/聊天），用来发现"出现但没卡"。[官方文档]

### 2.3 Plottr / Campfire / Scrivener / Squibler

- **Plottr**：角色表单（多种模板、可按属性筛选），可把角色/地点**链接到时间线上的场景卡片**；从角色页能看到其出现的所有场景。公开资料以人工维护为主，没有可靠的"AI 自动建卡"说明。[官方页=产品宣称；场景联动为官方功能说明] [第三方]
- **Campfire**：模块化写作工具，Characters 模块支持 100+ 属性、Relationships 模块与关系/家谱图；角色成长/弧线可随时间线可视化。角色与时间线事件可双向关联（第三方教程描述）。人工维护。[官方页=产品宣称；第三方]
- **Scrivener**：Binder 里的 Characters 文件夹 + 可复用的角色卡模板（可自定义多文件模板），纯人工、无 AI 抽取。[官方博客]
- **Squibler**：提供 AI 角色生成器（按描述生成完整角色档案）与 AI 大纲；宣称"角色声音保持一致"。未见公开的 Codex/场景绑定与逐章建卡文档，属于**提示词驱动的单次生成**。[产品宣称]

> 小结：这一组产品把"角色卡"当**人工资产**，AI 只在生成时读取；没有产品把"建卡"做成逐章自动任务。

---

## 三、网文侧（番茄，附阅文）

- **番茄「AI 人物设定」**（官方公告，2024-05）：两种入口——
  1. **新建角色设定**：作者填性别、类别（主角/反派/主角阵营/中立）、200 字内人物描述 → AI 生成人物卡；可手动改、可"换一换"、可换图。
  2. **小说角色总结**：系统列出**作品出现次数前 30** 的人物名（或手输名字）→ AI 总结成人物卡。
  - 配额：**24 小时 15 次**；需作者等级 LV2+，仅作者后台（作家助手暂不支持）。
  - 人物关系另有「新建人物关系」入口；人物形象后续会用于站内分发。
  - **没有**"随大纲/逐章自动建卡"的说明；建卡是**作者主动触发**，一次一卡。[官方公告]
- **阅文作家助手**：模板中心提供大纲、角色、设定模板，一键应用；未见逐章自动建卡的公开说明。[产品宣称] [推断]

> 网文侧的关键差异：**配额制 + 人工触发 + 按"出现次数"排序候选**。它在"发现漏角色"上用的是**全书词频统计**，而不是逐章模型调用。

---

## 四、有声侧

### 4.1 Audible / BookLLM（Amazon Science，2026-10）

这是目前公开信息里工程化程度最高的"角色卡"管线，但服务于配音而非写作：

1. **整本读一遍**建立 cast list：识别唯一角色、**按重要性排序**、为每人导出声音描述；
2. 从受控音色库里给每人分配**不同 profile**，优先拉开主要角色对比度；
3. **次要/一次性角色回退到旁白**，避免"一堆只用一次的声音"；
4. 再逐段标注"谁在说"（旁白/具体角色），并给每句对白写表演说明（如"紧张地低声说"）；
5. **角色声音 profile 在书级只固定一次**，保证从头到尾一致。
6. 评测：25 本英/西语小说、两名人工标注，说话人归属准确率 **97%（英）/ 94%（西）**。[官方研究]

> 对我们的直接启发：**"整本一次建卡 + 角色重要性分级 + 小角色不建卡"** 是被验证过的成本/质量折中；"每章一次"不是行业默认。

### 4.2 喜马拉雅「音剪 AI」

- 官方产品页列出「AI 小说」：**智能拆章、精准角色识别、多款 AI 音色**；「有声制作」支持多人有声剧。
- 官方账号/媒体报道：升级版音剪 AI 会**自动梳理角色性格特征与叙事身份，为旁白和每个角色推荐合适音色**；累计上线超 1 万种角色音；流程为**智能拆章 → 角色分析 → 演播**，并支持外部 Agent 调用的 AudiobookLM Skill。[产品宣称]

### 4.3 万象有声（Audimind）

- **智能画本**：上传文稿后自动识别角色、分离旁白与对白、用不同颜色区分；角色侧边栏可**查看/编辑角色档案、合并别名、指定播音**，也可让 AI 按"角色性格 + 声音特征"批量推荐播音。[官方文档]
- **全自动 AI 多播**：智能拆章 → **角色分析** → 画本生成 → AI 多角色演播 → 后期合成，无人值守，宣称约 **7–8 元/万字**；生成后仍可局部换音色、调语速、改读音。[官方文档=产品宣称]
- **去重**：自动识别 + **人工合并别名**；角色档案是画本里的"角色"实体，逐条台词绑定说话人。[官方文档]

> 有声侧的共性：角色卡天然是**"台本实体"**——按台词行绑定、按角色分配音色；去重靠"自动识别 + 人工合并别名"；成本按字数/分钟摊，而不是按章调用。

---

## 五、研究与开源管线（用于对照"自动去重"能做到什么）

- **BookNLP**：分词/NER → **角色名聚类** → 代词共指 → 引语归属；输出角色级 mention 链。说明"同名/别名/代词"可以靠**聚类 + 共指**做确定性合并，而不必每章问模型。[论文/开源]
- **ACL 2023 短论文（引语归属）**：文档级**命名实体聚类**作为别名合并，比局部 span 聚类更稳；"两个命名实体被标为同一别名时几乎总是对的"。这支持"别名合并用文档级聚类"。[论文]
- **COLING 2025（角色网络构建）**：管线为 NER → 共指（可选）→ **character unification** → 共现网络。把"统一/合并"作为独立阶段，是行业可借鉴的分层。[论文]
- **CharacterGPT（arXiv 2405.19778）**：按章节摘要**逐轮更新角色 persona**（CPT），属于"逐章回写"的研究路线。[论文]
- **Remember Me（ICCC 2026）**：planner/writer/grapher 三 agent，**逐章生成并维护演化中的知识图谱**，写作时同时条件于图谱与已生成正文。[论文]
- **Ex³（ACL 2024）**：长篇写作中抽取并追踪实体信息，再扩展成文。[论文]

---

## 六、与本项目现状对照

### 6.1 当前做法（作为对照基线）

- 分层大纲阶段三：**每章节拍生成完成后**调用一次 `character_sync`，把本章标题/摘要/全部节拍 + 现有角色卡交给模型；同名/同别名复用并补别名，没有就新建卡；**每个节拍绑定 character_ids**；空节拍按「本章 POV → 主角 → 第一个角色」兜底；`dry_run` 只预览不落盘；续跑补做。
- 扩充/续写（`expand_outline`）与 CLI 续写（`continue_outline`）同粒度接入。
- 正文后：角色状态**提案**落盘、作者确认后才写卡；一致性检查、未建卡捕获作为补充。
- 数据：档案只在 `characters.json`；大纲节拍存引用；实际出场从 `scenes/lines` 派生；POV 以 bible 为主源并写章副本。

### 6.2 与行业一致的地方

| 点 | 行业做法 | 评价 |
| --- | --- | --- |
| 角色卡作为唯一事实源，名字/别名检索 | Sudowrite、Novelcrafter | 一致；我们的"只存 characters.json + 引用 ID"更严格 |
| 建卡前置到正文之前 | Sudowrite（Synopsis 后一次）、万象有声（画本阶段） | 方向一致；我们更细（章/节拍级） |
| 别名/昵称参与匹配 | Novelcrafter、Sudowrite | 一致；我们还让模型"补别名" |
| "检测到才进上下文"的注入策略 | Novelcrafter AI Context | 一致；我们按 POV + 节拍出场 + 说话方式注入 |
| 状态回写需人工确认 | Novelcrafter Progressions（人工） | 一致；我们是"提案 + 确认"，没有静默改卡 |
| 小角色/漏角色兜底 | Audible 回退旁白；Novelcrafter 提及高亮 | 部分一致；我们的兜底是"POV/主角"而非"旁白/龙套" |

### 6.3 与行业不同的地方

1. **粒度**：写作类产品没有"每章节拍后建卡"这一档。它们是"计划期一次 + 写作期字符串检索"；我们是"计划期逐章各一次模型调用"。**更前置、更防漏，但调用次数随章数线性增长**。
2. **去重主体**：行业是"人工别名表 + 确定性匹配 + 人工合并"；我们是"模型判重 + 自动补别名"，缺人工合并/校验入口，也缺排除词与"代号不建卡"规则。
3. **绑定位置**：Novelcrafter 绑"场景/场景节拍"，万象有声绑"台词行"；我们绑"大纲节拍（计划层）+ 正文派生实际出场"——比写作工具更硬，但需要防止计划与实际漂移。
4. **空节拍兜底**：Audible 对次要角色**回退旁白**（避免音色泛滥）；我们强制绑 POV/主角，可能把龙套写成主角的戏，污染后续正文。
5. **回写形态**：Novelcrafter 的 Progression 是**场景锚定**的（该场景之后才可见，天然支持倒叙/闪回）；我们的状态是"当前值"提案，缺少时间锚点。

### 6.4 值得改的地方（按优先级）

**A. 成本：先做"确定性预扫描 + 条件调用"，再考虑合并调用**

- 行业用"一次建卡 + 提及检测"把成本压到 O(1)；我们是 O(章数)。以 100 章、平均 30 张卡估算，每章 cast 调用约 1–2k 输入 token + 数百输出 token，累计约 10–20 万输入 token；更重要的是**多出 100 次串行调用**，直接拉长生成时间。
- 建议按顺序做三件事：
  1. **预扫描**：用与 cast_sync 相同的名字/别名规范化规则扫本章节拍，若"没有未识别候选名 + 所有节拍已有绑定"，**跳过模型调用**（预期大量章节可跳过）；
  2. **只把未决候选送给模型**：已能确定性匹配的角色不再进 prompt，prompt 里只放"本章新增候选 + 精简角色表（id/姓名/别名/定位/speech 一行）"，不要把完整背景塞进去；
  3. **可选合并调用**：把 `cast` 作为 `emit_beats` 的附加字段一次产出（省一次调用），但需评估质量；更稳的是**每幕一次 cast 审计**兜底，章级只做预扫描 + 增量。
- 参考行业成本口径：Audible 整本一次；番茄 15 次/24h 配额；万象有声 7–8 元/万字；Novelcrafter 明确建议次要角色卡写短以省 token。

**B. 去重：从"模型全权判重"改为"三层去重 + 人工合并"**

- **第一层（确定性）**：姓名/别名精确匹配（去空白、全半角、引号）；维护**排除词表**：代词与叙述代称（他/她/它/他们、女人/男人/老人/少年/少女/对方/来人/声音…）**一律不建卡**，最多作为别名候选；像 Novelcrafter 一样支持"同名常用词"的大小写/排除处理。
- **第二层（模型）**：只处理未命中的候选；prompt 中列出"现有卡姓名+别名"，要求二选一（`existing_id` 或新建），且**每个角色必须带节拍证据**（哪条节拍、哪句话）；没有证据的条目丢弃。
- **第三层（人工）**：本章新建卡在界面上以"本章新建"分组展示，提供**合并到已有角色**（复用现有引用迁移能力，把大纲节拍/关系里的旧 ID 一并换掉）与**删除**入口；新建卡记录来源章节，便于回溯。
- **代号策略**：像「女人」「它」这类，行业（BookNLP/引语归属）靠共指聚类处理，不是建卡；建议我们只在**明确指向已有角色**时写成别名，不新建；无名龙套用 `minor` 且允许旁白/不绑定，而不是强绑主角。
- **重复新建的"复发阈值"**：第一次出现只记候选（不建卡），**第二次出现（或跨 2 章/2 节拍）再建卡**；这是 Audible"小角色不建声音"的写作侧等价做法，可显著减少卡片噪音。

**C. 绑定与兜底：允许"旁白/龙套"，不做主角兜底**

- 把"空节拍 → POV/主角"改成"空节拍 → 本章 POV；若节拍无具名人物 → 标记 `cast_unresolved` 并允许 `narrator`"，在生成正文前给一次提示而不是硬绑主角。行业（Audible）明确选择"minor fallback to narrator"。
- 角色卡加 `importance/龙套` 维度，与 Audible 的"按重要性排序"对齐；正文上下文注入时按重要性裁剪，避免卡片越多越挤占预算。

**D. 状态回写：加时间锚点，而不是只存"当前值"**

- Novelcrafter 的 Progression 模式值得借鉴：状态变化挂到**章节/场景**，生成某章时只注入"该章及之前"的变化；这样倒叙、插叙不会用到"未来状态"。
- 我们的章副本已有 POV 双写基础，可把 `state` 提案同样带 `anchor_chapter`，确认后按锚点生效；这比"覆盖当前值"更接近行业成熟做法，也避免一致性检查只能事后发现。

**E. 遗漏兜底：把"正文后捕获"前移为"正文前闸门"**

- 行业用提及高亮发现缺口。我们已有节拍绑定，建议在**章生成前**做一次轻量检查：本章节拍里出现的人名/别名若不在角色表且未标 `cast_unresolved`，直接拦住并提示补卡/标龙套；正文后的捕获与一致性检查保留为补充，不作为主路径。

---

## 七、来源清单

### 写作类

- [Sudowrite — What is Story Bible](https://docs.sudowrite.com/using-sudowrite/1owq1kGqof9rtcyGnrWUBS/what-is-story-bible/jmWepHcQdJetNrE991fjJC)［官方文档］
- [Sudowrite — Characters](https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/characters/a7tdE1ZB8KvAwMD3Mopwpd)［官方文档］
- [Sudowrite — Chat（可改 Story Bible）](https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/chat/5vbuELXf6LZQnGfVzsEXCV)［官方文档］
- [Novelcrafter — Anatomy of a Codex Entry](https://www.novelcrafter.com/help/docs/codex/anatomy-codex-entry)［官方文档］
- [Novelcrafter — Codex Tracking（AI Context 四档）](https://www.novelcrafter.com/help/docs/codex/codex-tracking)［官方文档］
- [Novelcrafter — The Mentions Tab](https://www.novelcrafter.com/help/docs/codex/codex-mentions)［官方文档］
- [Novelcrafter — Progressions/Additions](https://www.novelcrafter.com/help/docs/codex/progressions-additions)［官方文档］
- [Novelcrafter — Description Guidelines（次要角色短卡省 token）](https://www.novelcrafter.com/help/docs/codex/guidelines-for-descriptions)［官方文档］
- [Novelcrafter — Codex 功能页](https://www.novelcrafter.com/features/codex)［产品宣称］
- [Plottr — Features](https://plottr.com/features)［产品宣称］
- [ScribeCount — Plottr 评测（角色与场景联动）](https://scribecount.com/author-resource/writing-tools-for-authors/plotting-app)［第三方］
- [Campfire — Character Builder](https://campfirewriting.com/character-builder)［产品宣称］
- [ScribeCount — Campfire 评测（时间线与角色出场）](https://scribecount.com/author-resource/writing-tools-for-authors/campfire-write-for-indie-authors)［第三方］
- [Literature & Latte — Scrivener 角色卡模板](https://www.literatureandlatte.com/blog/using-advanced-character-sketch-templates-in-scrivener)［官方博客］
- [Squibler — AI Character Generator](https://www.squibler.io/ai-character-generator/)［产品宣称］
- [Squibler — AI 小说写作（大纲）](https://www.squibler.io/)［产品宣称］

### 网文侧

- [番茄小说 — AI 人物设定功能使用说明](https://fanqienovel.com/writer/zone/article/7361736120746393662)［官方公告］
- [番茄小说 — AI 写作工具上线通知（AI 起名等）](https://fanqienovel.com/writer/zone/article/7327136545129906238)［官方公告］
- [阅文作家助手（模板中心/大纲角色模板）](https://write.qq.com/portal/advertising)［产品宣称］

### 有声侧

- [Amazon Science — Better speech synthesis for audiobooks（BookLLM）](https://www.amazon.science/blog/better-speech-synthesis-for-audiobooks)［官方研究］
- [喜马拉雅音剪（一站式音频创作平台）](https://audioeditor.ximalaya.com/)［产品宣称］
- [喜马拉雅官方微博：音剪 AI 升级（音色分配/角色梳理）](https://m.weibo.cn/detail/5316247341239941)［产品宣称］
- [央广网：喜马拉雅 WAIC2026（角色分析→推荐音色）](http://www.cnr.cn/mspd/jrhm/20260721/t20260721_527721985.shtml)［媒体报道］
- [万象有声 — 10 分钟跑通第一个作品（智能画本/角色/合并别名）](https://www.audimind.com/docs/first.html)［官方文档］
- [万象有声 — 创作全流程演示（全自动多播/成本口径）](https://www.audimind.com/docs/workflow.html)［官方文档］
- [万象有声官网（AI 音色推荐）](https://tweb.audimind.com/)［产品宣称］

### 研究/开源

- [BookNLP（GitHub）](https://github.com/booknlp/booknlp)［开源/论文］
- [Improving Quotation Attribution with Fictional Character Embeddings（EMNLP 2024 Findings）](https://hal.science/hal-04869619v1/file/2024.findings-emnlp.744.pdf)［论文］
- [Improving Automatic Quotation Attribution in Literary Novels（ACL 2023 Short）](https://aclanthology.org/anthology-files/anthology-files/anthology-files/pdf/acl/2023.acl-short.64.pdf)［论文］
- [The Role of NLP Tasks in Automatic Literary Character Network Construction（COLING 2025）](https://aclanthology.org/2025.coling-main.566.pdf)［论文］
- [CharacterGPT: A Persona Reconstruction Framework（arXiv 2405.19778）](https://arxiv.org/pdf/2405.19778v5)［论文］
- [Remember Me: Enhancing Long-term Character Persistence using Knowledge Graphs（ICCC 2026）](https://computationalcreativity.net/iccc26/papers/ICCC26_paper_39.pdf)［论文］
- [Ex³: Automatic Novel Writing by Extracting, Excelsior and Expanding（ACL 2024）](https://aclanthology.org/anthology-files/anthology-files/anthology-files/pdf/acl/2024.acl-long.494.pdf)［论文］
- [Novarrium — 逐章自动抽取角色事实（产品博客）](https://novarrium.com/blog/ai-character-consistency)［产品宣称］

---

## 八、调研边界

- 各家帮助中心不会公开"是否逐章调模型"的内部实现；本报告对写作工具的判断基于**公开文档描述的功能形态**，未公开处标为**[推断]**。
- Plottr / Campfire / Squibler 的"AI 能力"多来自官网营销页，缺少可核对的行为文档；因此只作方向性参考。
- 有声侧的三家都有"自动识别角色"，但**识别是确定性 NER/规则还是模型调用**未公开；按证据只能确认产品行为，不能确认调用粒度。
- 本报告没有做产品实测，所有行为均来自公开资料；若需要，可对 2–3 家做一次真实稿件对比测试（输出角色数、别名合并数、人工修正耗时）。
