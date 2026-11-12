# AI 续写机制横向调研（对照 R19 续写设计）

> 性质：**调研文档**。不改生产代码，不改 `spec-r19-outline-and-chapter-continuation.md`。
> 日期：2026-11-12
> 目的：用同类产品的可查证做法，校验 R19「大纲续写 + 正文续写」的入口、长度、上下文、落盘、中断与结构化续写方案。
> 证据标注：**【官方】**= 产品官方文档/官方公告；**【官方商店】**= 官方应用商店描述；**【二手】**= 第三方评测/下载站（仅作旁证）；**【不可查证】**= 未找到公开说明，不做推断。

---

## 0. 结论速览

1. **入口**：写作类产品（Sudowrite/NovelAI/Novelcrafter/番茄/彩云/蛙蛙/笔灵）都把续写放在编辑器内（光标处 / 选中文字 / `/` 菜单），侧栏只承担「给意图」和「看历史」；只有 Claude Projects 这类通用对话产品是纯聊天形态。
2. **长度**：在能查到的产品里，主流都给用户可调的字数/词数或预设，且有硬上限；没有一家把长度完全交给模型自由决定。
3. **连续性**：通行配方是「最近原文（有上限）+ 摘要/设定库 + 大纲或节拍 + 可选用户意图」，靠**预算裁剪**而不是「整本塞进去」。显式的「禁止重复前面情节」很少见，多数靠提示词 + 去重/裁剪兜底。
4. **落盘**：分两派——**候选区派**（Sudowrite 的 History 卡片、Novelcrafter 的 Apply/Retry/Discard/Section）与**就地插入派**（NovelAI、番茄、彩云小梦），后者靠 undo/分支/换一批兜底。在能查到的产品里，没有一家在生成失败时把半成品直接写进正文且不可撤销。
5. **中断**：各家都没有公开承诺「流式打断后保留多少」，普遍用「生成结果先不落到正文」或「可撤销分支」来规避。
6. **结构化续写**：能查到的做法是「按章/按卷/按幕追加 + 自动衔接上一章」，没有一家公开说明「增量合并冲突」的自动处理；冲突基本靠人工确认。
7. **额度**：续写成本与「读多少字 + 写多少字 + 用什么模型」正相关；上下文档位和单次输出上限经常直接绑定付费档。

---

## 1. 关键机制对照表

| 产品 | 触发入口 | 单次生成长度控制 | 连续性上下文 | 落盘 / 重抽 | 中断保留 | 结构化（大纲）续写 | 付费 / 额度 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Sudowrite** | 光标放到文末 → 点 Write；另有 Guided（先给 3 条走向建议）与 Tone Shift 【官方】 | 每张卡片约 50–1,500 词，Max Words 最高约 2,000 词；卡片数量可设 【官方】 | 回读光标前最多 20,000 词；当前文档再取最多 20,000 词；Story Bible（Style/Genre/Characters/Worldbuilding/章节大纲）；Chapter Continuity 最多 25 个前序文档、累计最多 20,000 词 【官方】 | 生成结果进右侧 **History 卡片**，点 Insert 才入正文；插入后显示为紫色待编辑文本；重抽=再生成一张卡片 【官方】 | 【不可查证】 | 从 Outline 新建章节时**自动链接前一章**；Draft 按 Scenes 一次生成 3,000–5,000+ 词整章 【官方】 | 按 credits 计费，消耗取决于「读了多少词 + 写了多少词 + 模型」；档位约 22.5 万 / 100 万 / 200 万 credits/月 【官方】 |
| **NovelAI** | 编辑器内点 Send（⌘/Ctrl+Enter）从当前文末继续；也可在文中任意点 **Generate Inline** 【官方】 | Generation Options 里的 **Output Length**：官方写作「最少 4、最多 600 characters，上限随订阅档位」；单位口径存疑（疑为 token），但确实是可调上限 【官方】 | Memory（放在上下文最前）+ Lorebook（关键词激活）+ 正文 + Author's Note（靠近末尾）；每个条目的插入顺序、前后缀、裁剪方向、预留 token 都可配 【官方】 | **就地插入正文**；Undo/Redo 是分支时间线，Retry 重发上一次生成，History 可选从哪一版继续 【官方】 | 【不可查证】；但分支/撤销机制使「生成坏了」可回退 【官方】 | 无章节/大纲结构，属于「故事编辑器」，不是大纲驱动 【官方】 | 文本生成不限量；**上下文上限按档位**，Opus 为 28,672 tokens；Xialong 为 28,672 + 8,192 滚动窗口 【官方】 |
| **Novelcrafter** | 正文里输入 `/` 打开菜单：**Continue Writing**（预置「继续写」指令）或 **Scene Beat**（用户写接下来发生什么）【官方】 | 通用写作提示词内置长度预设，也可手填 【官方】 | Codex（角色/地点/物品等词条）+ 已有正文 + `storySoFar` 摘要上下文；可用 prompt 函数按 novel/act/chapter/scene 过滤，例如只取最近 3 条场景摘要 【官方】 | 生成后就地给 **Apply / Retry / Discard / Section**；Section 可把多版并排保留 【官方】 | 【不可查证】；未 Apply 的文本仍在编辑器里，不会自动进正文 【官方】 | 以 Act→Chapter→Scene 层级规划，用 scene beat 逐段推进；未见「一次追加 N 章」的公开说明 【不可查证】 | 订阅 + 自带 Key（BYOK）；「Generate from Scene Beats」从 Hobbyist（$8/月）档起可用 【官方】 |
| **Squibler** | 编辑器内 AI 写作、continue scenes 【官方营销页】 | **【不可查证】** | **【不可查证】**；官方只称能维护角色与场景一致性 | 可「generate another version」重新生成；是否先候选再接受 **【不可查证】** | 【不可查证】 | 支持先生成大纲，再「Generate Full-Length Book」生成整本；章节/场景可编辑 【官方营销页】 | 有免费入口；具体额度 **【不可查证】** |
| **Claude Projects** | **没有专用续写按钮**；官方文档只描述 Project knowledge + Project instructions，续写靠对话 【官方文档未见】 | 由用户指令控制；无产品级滑杆 【官方】 | Project knowledge（项目内所有对话共享）+ Project instructions + 当前对话；窗口 200K/1M tokens 【官方】 | 结果直接出现在对话里，复制到文档；无「应用/丢弃」概念 【官方】 | 长对话接近上限时自动摘要早前消息（claude.ai）【官方】 | 无大纲结构；结构化续写需用户自己维护文档 【官方】 | 按套餐用量限制；长上下文计入用量，超窗直接 400 `prompt is too long` 【官方】 |
| **番茄小说（作家助手）** | 选中部分文字，或直接点「续写」按钮 【官方】 | **【不可查证】**；官方未给字数上限，只说明可「简述下文故事情节」再生成 【官方】 | 官方只说明「紧接着上文」；具体上下文组成未公开 【官方】 | 生成后就地可改；不满意点「换一换」重生成 【官方】 | 【不可查证】 | 「开书灵感」一次给 3 个方向，「生成大纲」生成整本大纲；「卡文锦囊」一次给 5 条后续情节 【官方】 | 续写/扩写**仅对非保底作者开放**；未公布次数/字数额度 【官方】 |
| **阅文作家助手（妙笔）** | 写作页内的妙笔能力（设定灵感、章纲智能提取等）；「妙笔通鉴」支持挂载章节后问答 【官方版本说明】 | **【不可查证】** | **【不可查证】**；官方只说明可提取章纲、角色、设定 | **【不可查证】** | **【不可查证】** | 有总纲/卷纲/章纲模板与「章纲智能提取」，但是否能按章追加未公开 【官方版本说明】 | **【不可查证】** |
| **七猫（作家专区）** | **AI 小助理**：输入问题/需求后给回答，属聊天式助手 【官方】 | 不适用 | **【不可查证】** | 建议以对话形式返回，需作者自行取用 【官方】 | 不适用 | 未提供大纲续写；官方只列资料、起名、场景描写、答疑 【官方】 | **【不可查证】** |
| **彩云小梦** | 创作页输入一段文字 → 点「AI 小梦来帮写」【官方商店】 | 可自定义续写长度 **50–2000 字**，可控制结尾是否为完整短句 【二手】 | 官方商店只说明「接着上文」；世界设定/人物词条可建 【官方商店】 | 一次给 **3 条不同走向**，选一条继续；「换一批」重新给 3 条；可回溯任意节点改剧情、生成分支 【官方商店】【二手】 | 【不可查证】 | 无大纲结构，偏「分支故事」而非「章节大纲」 【官方商店】 | 年费权益写「每月 100,000 字长篇故事」【官方】 |
| **蛙蛙写作** | 正文编辑区上方点「续写正文」【官方】 | **【不可查证】**；工具页未公开字数上限 | 自动读取作品数据、目标读者、章纲、上下文；可**手动勾选跨章「参考剧情」和出场角色卡** 【官方】 | **【不可查证】**；官方只说明生成后作者修改使用 | 【不可查证】 | 总纲→卷纲→章纲（**1~20 章可配置**），可把**上一章结尾**填作衔接，再逐章续写正文 【官方】 | **【不可查证】** |
| **笔灵 AI 写作** | 小说编辑器内「每章节智能续写 / 自定义续写」【官方】 | **【不可查证】**；官方只宣传「无限续写」「10 分钟写全篇」 | **【不可查证】** | **【不可查证】** | 【不可查证】 | 有小说模板与全篇生成，未公开按章追加机制 【官方】 | **【不可查证】** |

---

## 2. 七个问题的横向回答

### 2.1 续写入口放在哪

- **编辑器内为主，且都支持「从文末继续」**：Sudowrite 把光标放文末点 Write；NovelAI 点 Send；Novelcrafter 用 `/` 菜单的 Continue Writing；番茄直接点「续写」或选中文字；彩云点「AI 小梦来帮写」；蛙蛙点编辑区上方「续写正文」。
- **「选中段落」是第二入口**：番茄明确支持选中文字后续写；NovelAI 支持在文中任意位置 Generate Inline；Sudowrite 的 Write 也是以光标位置为起点。
- **侧栏只做辅助**：Sudowrite 右侧栏是 History（存放卡片）；Novelcrafter 的 Chat/Workshop 是独立对话，不承担正文续写落盘；Claude Projects 本身就是对话，没有编辑器内按钮。
- **对我们的含义**：R19 把入口放章节页「续写正文」按钮是对齐主流；但**缺少「从选中段落/光标位置续写」**这一形态。R19 已明确首版只做「文末追加」，可以接受，但建议在 spec 里写清这是有意收窄，而不是遗漏。

### 2.2 单次生成长度怎么控制

- **可调 + 硬上限是共识**：
  - Sudowrite：每张卡片 ~50–1,500 词，Max Words 最高 ~2,000 词；卡片数可设。
  - NovelAI：Output Length 滑杆，官方写 4–600（characters），上限随档位。
  - Novelcrafter：提示词内置长度预设，可手填。
  - 彩云小梦：50–2000 字可设，还能控制结尾是否完整句。
  - 番茄：官方未公布字数控制，但提供「简述下文情节」来约束内容走向。
- **没有一家让模型自行决定长度**；也没有一家暴露「轮数」这种内部编排参数给普通用户。
- **对我们的含义**：R19 的「800 字 × 3 轮，上限 3000 字」在实现上没问题，但**用户界面暴露「轮数」是内部概念**，与主流「按字数/预设选」不一致（见建议 2）。

### 2.3 连续性怎么保证

可查到的上下文配方，按出现频率排序：

| 素材 | 谁在用 | 关键细节 |
| --- | --- | --- |
| 最近原文（有字数上限） | 全部 | Sudowrite 当前文档最多 20,000 词、前序文档合计最多 20,000 词；R19 是末尾 2000 字 + 500 字禁重复参考 |
| 设定库/角色卡 | Sudowrite Story Bible、NovelAI Memory/Lorebook、Novelcrafter Codex、彩云世界词条、蛙蛙角色卡 | NovelAI 可按条目设插入顺序、裁剪方向、预留 token；Novelcrafter 可按 act/chapter/scene 过滤 |
| 摘要（story-so-far） | Novelcrafter `storySoFar`、Sudowrite 章节大纲摘要、R19 全剧梗概 | Novelcrafter 官方 prompt 示例用 `takeLast(chapter.summary(storySoFar), 3)` 只取最近 3 条场景摘要 |
| 大纲/节拍 | Sudowrite linked Outline、Novelcrafter scene beat、蛙蛙章纲、R19 剩余节拍 | 节拍是「接下来必须发生什么」的最强约束 |
| 用户意图 | Sudowrite Guided、番茄「简述下文情节」、Novelcrafter Scene Beat、蛙蛙「额外要求」 | 主流都提供「用一句话说接下来想发生什么」 |

**显式「不许重复前面情节 / 接着最后一个动作」**：
- 公开文档里**没有**看到逐字的产品级提示词；Sudowrite 反而给了操作技巧：「把光标停在句子中间，模型更容易接着写」「把大想法拆成小步骤」。这说明它靠**光标位置 + 前文**隐式约束，而不是一句「禁止复述」。
- R19 的「衔接锚点 + 禁止重复参考 + 代码级重叠裁剪」比公开文档里能看到的更严格，属于加分项，不是过度设计。

**上下文超限怎么办**：
- Sudowrite 明确给了**丢弃顺序**：Worldbuilding → Characters → Previous Chapter Text → Linked Outline Summary → Genre → Key Details → Tone → Style → Preceding Text → Highlighted Text；并说明「部分内容仍可能被部分传入」。
- NovelAI 是每个条目自己配「插入顺序 / 裁剪方向 / 最多裁多少 / 预留 token」。
- Claude 官方明确「上下文越长，准确率和召回会下降（context rot）」，且输入超窗直接 400。
- **对我们的含义**：R19 的固定降级顺序与 Sudowrite 同思路，可以保留；但**「全剧梗概」被列为第一个可删项**与主流「摘要优先保留」相反（见建议 5）。

### 2.4 新内容怎么落盘

两派做法：

| 派别 | 产品 | 具体行为 |
| --- | --- | --- |
| 候选区派 | Sudowrite | 结果进 History 卡片，点 Insert 才入正文；插入后紫色标记，提示「继续编辑才算你的」 |
| 候选区派 | Novelcrafter | 生成后就地给 Apply / Retry / Discard / Section；Section 可并排保留多版，便于对比 |
| 就地插入派 | NovelAI | 直接写进编辑器；靠 Undo/Redo 分支、Retry、History 选分支回退 |
| 就地插入派 | 番茄 | 直接插入，可改；「换一换」重新生成 |
| 就地插入派 | 彩云小梦 | 先给 3 条走向，选一条继续；可回溯节点、生成分支 |

**重抽 / 撤销 / 多候选**：
- 重抽：Sudowrite（再生成卡片）、Novelcrafter（Retry）、番茄（换一换）、彩云（换一批）全都有。
- 多候选对比：Sudowrite 一次生成多张卡片、彩云一次 3 条、Novelcrafter 用 Section 并排；**R19 首版不做多候选，只做单次预览**，属于功能收窄，但**连「重抽」都没有**是明显缺口。
- **对我们的含义**：R19「默认预览 + 用户点应用」是对齐候选区派的；但 **§7.1 的「逐轮落盘」把候选区又变回了直接写正文**，与主流风险控制方向相反（见建议 1）。

### 2.5 中断与失败怎么处理

- **没有一家公开说明「流式输出被打断后保留多少」**，全部【不可查证】。
- 可查到的替代机制：
  - Sudowrite：卡片在 History 里，未 Insert 就不影响正文；中断最多丢一张卡片。
  - Novelcrafter：未 Apply 的生成文本不算正文；Apply 才落盘。
  - NovelAI：任何生成都可 Undo/Retry，History 保留分支。
  - 番茄/彩云：就地插入 + 换一换/分支回溯。
- **对我们的含义**：R19 想「已写的不会白写」，动机合理，但**用「逐轮写正文」来实现，代价是失败/取消会在正文留下半成品**。主流做法是「半成品留在候选区，正文只在用户确认时变更」（见建议 1、6）。

### 2.6 大纲这类结构化续写

- **Sudowrite**：Outline 里建章节，从 Outline 新建章节时**自动链接前一章**（自动 Chapter Continuity）；再用 Draft 按 Scenes 一次生成 3,000–5,000+ 词整章。这是「按章追加 + 自动衔接」最完整的公开做法。
- **蛙蛙写作**：总纲 → 卷纲 → 章纲（1~20 章可配置），可把**上一章结尾**填作衔接，再逐章续写正文。中文网文工具里对「衔接」写得最具体。
- **番茄**：开书灵感一次给 3 个方向 → 生成整本大纲；写作中「卡文锦囊」一次给 5 条后续情节。偏「灵感/情节候选」，不是「按章追加」。
- **Novelcrafter**：Act→Chapter→Scene 三级规划 + 逐 scene beat 生成；官方文档没有「一次追加 N 章」的命令式入口。
- **NovelAI / Claude / 彩云 / 笔灵 /七猫**：没有可查证的大纲续写机制。
- **增量合并冲突**：**没有一家公开说明**。可查到的只有 Novelcrafter Codex Progressions：新增用 Additions、覆盖用 Replacements，且「AI 只从该时间点之后才看到变更」——这是**设定层**的版本化，不是大纲合并。R19 的「只 append、不重排、不重新编号」是更保守也更安全的做法。

### 2.7 付费/额度与续写长度的关系

- **Sudowrite**：credits 消耗 = 读的词 + 写的词 + 模型档次；卡片越长、上下文越长，越贵。三档约 22.5 万 / 100 万 / 200 万 credits/月。
- **NovelAI**：文本生成不限量，但**上下文窗口和单次输出上限按档位**；Opus 才有 28,672 tokens 上下文。
- **Novelcrafter**：订阅费 + 自带 Key，token 成本付给模型供应商；「Generate from Scene Beats」要 Hobbyist 及以上。
- **Claude**：按套餐限额；长上下文计入用量；超窗直接报错，不会自动降级。
- **彩云小梦**：年费权益写「每月 100,000 字长篇故事」，即**按生成字数**给额度。
- **番茄**：续写/扩写**只对非保底作者开放**，是权限而非字数额度。
- **对我们的含义**：R19 是本地/自建模型，没有 credits 概念，但「上下文长度 × 输出长度」直接决定耗时与显存占用；**默认 3000 字硬上限**合理，建议同时在 UI 上把「本次预计新增字数」前置展示（R19 已有预览，保留）。

---

## 3. 各产品细节与出处

### 3.1 Sudowrite

**Write（续写主入口）**【官方】
- 光标放到已有文字末尾，点 Write，AI 接着写。
- 回读光标前最多 **20,000 words**；Story Bible 开启时还会注入 Style、Genre、Characters、Worldbuilding、linked Chapter Outline。
- 官方建议**至少 20 个词**再续写，越往后效果越好。
- 输出 **50–2,000 words**（Write settings 可调）；单张卡片约 **50–1,500 words**，Max Words 最高约 **2,000 words**，卡片数量可设。
- 结果先出现在右侧 **History** 栏，点 **Insert** 才进正文；插入后为**紫色**，提示用户编辑后才算自己的文本。
- 三种模式：**Auto / Guided / Tone Shift**。Guided 可读取光标前 1,000 词，**给 3 条「接下来可能发生什么」的建议**，也可自己写一句；官方技巧包括「把大想法拆小」「把光标停在句子中间」。
- 出处：<https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/write/pvxUvbQqYybfEosqx1sXjY>

**Chapter Continuity（跨章连续性）**【官方】
- 在文档 `•••` 菜单里回答两个问题：是否属于 Outline、是否接续某文档。
- Write 会从链接文档回读**最多 20,000 words**；如果串联多个文档，**最多回看 25 个前序文档**，从最近往前，直到触顶。
- 当前文档也会从光标处往回读**最多 20,000 words**。
- 从 Outline 新建章节时**自动按序链接**；跳过中间章节时链接到最近的已连接章。
- 上下文放不下时按固定顺序丢弃：Worldbuilding → Characters → Previous Chapter Text → Linked Outline Summary → Genre → Key Details → Tone → Style → Preceding Text → Highlighted Text。
- 出处：<https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/chapter-continuity/4KL8gFeLZQ6GSBjDWtSbV6>

**Braindump / Draft（澄清两个容易混淆的功能）**【官方】
- **Braindump 不是续写入口**，它是 Story Bible 里手填的「原始想法」字段，会影响 Synopsis 的生成；Synopsis 为空时，下游字段回落到 Braindump。
- **Draft 是整章生成**：按已建立的 Scenes，一次生成 **3,000–5,000+ words** 的整章。
- 出处：<https://docs.sudowrite.com/using-sudowrite/1owq1kGqof9rtcyGnrWUBS/what-is-story-bible/jmWepHcQdJetNrE991fjJC>、<https://docs.sudowrite.com/getting-started/dQph1snuwbfMWG9wRjsNug/features/dq7YUMNy5ZMvKUJiRAisyT>

**额度**【官方】
- credits 消耗取决于任务复杂度，即「读了多少词、写了多少词、用什么模型」。
- 档位公开为 Hobby/Student 约 22.5 万、Professional 约 100 万、Max 约 200 万 credits/月。
- 出处：<https://docs.sudowrite.com/plans--account/wBnmhtSyMcWtk2BLzifGkz/what-are-credits/qzSpgs9UDPEtwYcdczkLd3>、<https://www.sudowrite.com/pricing>

### 3.2 NovelAI

**续写入口**【官方】
- 在 Editor 里写完一段，点 **Send**（⌘/Ctrl+Enter）从当前上下文末尾继续生成。
- 想从文中某点续写，点该位置后按 **Ctrl+Shift+Enter** 或用右键菜单的 **Generate Inline**。
- 每次生成都进 **Edit History 分支时间线**：Undo/Redo 逐步回退，**Retry**（⌥+R）重发上一次生成，**History** 按钮选择从哪一版继续。
- 出处：<https://docs.novelai.net/en/text/editor>

**长度控制**【官方】
- Generation Options 的 **Output Length** 控制单次输出上限，官方写「最少 4、最多 600 characters，取决于订阅档位」。
- 说明：该页把单位写作 characters，与模型 28,672 tokens 的上下文口径不一致，**疑为 token 口径或文档笔误，官方未给换算**，此处按原文引用，不作推断。
- 出处：<https://docs.novelai.net/en/text/editor/slidersettings>

**上下文组成**【官方】
- **Memory**：长期事实，默认放在 Story Context 最前面。
- **Lorebook**：关键词激活的词条，可配置插入位置/顺序。
- **Author's Note**：插在靠近上下文末尾的位置，对输出影响很强，用来定文风/当下重点。
- 每个条目（Memory / Author's Note / Lorebook / Story Context）都可配 **Prefix、Suffix、Insertion Order、Trim Direction、Maximum Trim Type、Reserved tokens**；可查看 Context Viewer 的 token 占比。
- 最大上下文取决于订阅档位；**Opus 为 28,672 tokens**；Xialong 为 28,672 + 8,192 滚动窗口（实际可用 28,672–36,864）。
- 出处：<https://docs.novelai.net/en/text/editor/storysettings>、<https://docs.novelai.net/en/text/editor/advancedsettings>、<https://docs.novelai.net/en/subscription>、<https://docs.novelai.net/en/text/models>

**结构化能力**
- 没有章节/大纲结构，本质是「长文故事编辑器 + 记忆/词条」；结构化续写要靠用户自己维护 Memory。
- 出处同上。

### 3.3 Novelcrafter

**两个正文生成入口**【官方】
- 在正文输入 `/`，菜单里有 **Continue Writing** 与 **Scene Beat**。
- 官方 FAQ：两者功能上唯一区别是 **Continue Writing 预置了一句「继续写」指令**，适合不想给额外指导的用户；Scene Beat 由用户写「接下来发生什么」。
- 出处：<https://www.novelcrafter.com/help/faq/write/scene-beat-vs-continue-writing>、<https://www.novelcrafter.com/courses/ultimate-beginners-guide/generating-prose-with-ai>

**上下文**【官方】
- 「AI 读取你的 beat、Codex 上下文和已有正文」。
- 通用写作提示词提供**长度预设**，也可点笔图标手填；可切换提示词/模型，或点 **Tweak and Generate** 调整全部输入。
- `storySoFar` / `storyToCome` 支持按 novel/act/chapter/scene 过滤，取交集；官方示例用 `takeLast(chapter.summary(storySoFar), 3)` 只取最近 3 条场景摘要，说明**默认上下文是摘要而不是全文**。
- 出处：<https://www.novelcrafter.com/help/reference/prompts/prompt-examples>、<https://www.novelcrafter.com/help/reference/prompts/prompt-functions>

**落盘与重抽**【官方】
- 生成后四个按钮：**Apply**（接受进正文）、**Retry**（同指令重生成，替换上一版；想保留就先 Apply 或 Section）、**Discard**（丢掉）、**Section**（接受并放入 Section，便于多版并排）。
- 出处：<https://www.novelcrafter.com/courses/ultimate-beginners-guide/generating-prose-with-ai>

**结构化与设定版本**【官方】
- Plan 以 Act→Chapter→Scene 组织；Codex 是设定中心，可自动索引提及、按场景摘要供 AI 使用。
- Codex **Progressions**：Additions（叠加新细节）/ Replacements（覆盖旧信息），AI **只从该时间点之后**看到变更；Series Codex 可跨书共享。
- 出处：<https://www.novelcrafter.com/use-cases/romance-nsfw>、<https://www.novelcrafter.com/courses/ultimate-beginners-guide/setting-up-the-codex>

**付费**【官方】
- Scribe $4、Hobbyist $8、Artisan $14、Specialist $20/月；AI 从 Hobbyist 起（BYOK），「Generate from Scene Beats」Hobbyist 起可用。
- 出处：<https://www.novelcrafter.com/pricing>

### 3.4 Squibler

**能查到的**【官方营销页】
- 编辑器内可用 AI「generate outlines, continue scenes, build characters, rewrite sections」。
- 可先生成大纲，再点 **Generate Full-Length Book** 生成整本；可「generate another version」重生成；有字数目标追踪。
- 出处：<https://www.squibler.io/novel-writing-software/>、<https://www.squibler.io/ai-book-writer/>

**查不到的**
- 单次续写长度控制、上下文组成、是否候选区落盘、中断策略、额度与续写长度的关系，官方页面均未说明。标注 **【不可查证】**，不做推断。

### 3.5 Claude Projects

**机制**【官方】
- Project 有 **project knowledge**（项目内所有对话共享的知识库）与 **project instructions**（项目级指令）。
- 没有「续写按钮」；续写靠用户在对话里贴原文 + 指令。
- 上下文窗口 200K / 1M tokens；官方明确「上下文越长，准确率与召回会下降（context rot）」。
- 输入超窗：API 直接返回 **400 `invalid_request_error`（"prompt is too long"）**；Claude 4.5+ 在输入 + max_tokens 超窗时接受请求，生成到上限后以 `model_context_window_exceeded` 停止。
- 官方提供**自动上下文管理**：长对话接近上限时会摘要早前消息（该能力与具体功能开关/套餐相关）。
- 出处：<https://support.claude.com/en/articles/9517075-what-are-projects>、<https://platform.claude.com/docs/en/build-with-claude/context-windows>、<https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work>

### 3.6 番茄小说（作家助手）

**续写与改写**【官方】
- 「续写」：**选中部分文字或直接点「续写」按钮**，AI 紧接着上文生成下文，作者可修改使用。
- 可「简述下文的故事情节」再生成（编辑器课程页）。
- 不满意点**「换一换」**重新生成。
- 「卡文锦囊」：选小说类型 + 输入卡点，**一次生成 5 个后续情节**。
- 「扩写」限选中 5–300 字；「改写」限 5–600 字，方向含轻松明快/严肃沉稳/添加修辞。
- 续写与扩写**仅限非保底作者**。
- 出处：<https://fanqienovel.com/writer/zone/article/7327136545129906238>、<https://fanqienovel.com/writer/zone/article/7455639656860762137>

**结构化**
- 「开书灵感」一次给 3 个灵感，选中后可「生成大纲」；无「按章追加」的公开说明。
- 出处同上。

### 3.7 阅文作家助手（妙笔）

**能查到的**【官方】
- 版本说明里明确有【妙笔】设定灵感（含宝物道具）、**章纲智能提取**（一键提取章节概要）。
- 「妙笔通鉴」支持挂载章节后输入指令问答，示例指令包括「提取大纲」「角色关系」。
- 官方广告页把「妙笔 AI 写作助手」列为卖点，并提供大纲/角色/设定模板。
- 出处：<https://write.qq.com/portal/version>、<https://write.qq.com/portal/community/topic/detail/89608351800941501>、<https://write.qq.com/portal/advertising>

**查不到的**
- 续写入口、单次长度、上下文组成、落盘方式、额度。标注 **【不可查证】**。
- 时间线旁证（二手）：阅文 2023 年 7 月发布「妙笔」大模型及作家助手妙笔版。
- 出处：<https://36kr.com/p/3501232140474501>

### 3.8 七猫

**能查到的**【官方】
- 2023-07-31 上线「AI 小助理」：在作家专区点图标进入，输入问题/需求后给回答与建议；官方列举能力为历史文化资料、角色起名、场景描写、创作答疑。
- 出处：<https://www.qimao.com/gonggao/64c711e2727af1330b0af2e6/>

**结论**
- 未提供「续写正文」机制文档；属于聊天式辅助，不是编辑器内续写。不做进一步推断。

### 3.9 彩云小梦

**能查到的**
- 创作页输入一段文字，点「AI 小梦来帮写」，**立刻给三条不同故事走向**；点继续可再次续写；点「换一批」重新给三条。【官方商店】
- 可建普通词条与人物词条，增强一致性。【官方商店】
- 可自定义续写长度 **50–2000 字**，可控制结尾是否为完整短句；支持「回溯任意续写节点、修改剧情并生成分支」。【二手】
- 年费权益：「AI 智能续写，每月享受 100,000 字长篇故事」。【官方】
- 出处：<https://m.yyb.qq.com/agreement/app-desc/?appid=54148715>、<https://m.duote.com/android/1162933.html>、<https://www.xiaomengai.com/h5/annual-fee>

**注意**
- 「三条走向是否可并排对比」官方未说明；从描述看是「选一条继续」，不是并排 diff。标 **【不可查证】**。

### 3.10 蛙蛙写作

**能查到的**【官方】
- 正文编辑区上方点「续写正文」呼出工具。
- 直连后台作品数据，自动读取**目标读者、章纲、上下文语境**。
- 可手动勾选**跨章节的「参考剧情」**（例如第 50 章要用第 10 章出场的配角）与**出场角色**，让 AI 调取对应章节细节。
- 结构化链路：长篇总纲 / 卷纲 / 章纲；章纲**一次可配 1~20 章**，可填入**上一章结尾**作为衔接，并可在补充要求里指定爽点节奏。
- 出处：<https://wawawriter.com/notes/article/6ae831e076ceb17210b90a80>、<https://wawawriter.com/notes/article/6ae82904034e9a8221b909f6>

**查不到的**
- 单次续写字数上限、落盘是否候选、中断策略、额度。标注 **【不可查证】**。

### 3.11 笔灵 AI 写作

**能查到的**【官方】
- 小说编辑器宣传「热门模板 + 无限 AI 续写 + 10 分钟写全篇」「每一章节都可以智能续写，也可以自定义续写」。
- 独立「续写」工具支持上传 word/txt/markdown 后按文本类型续写。
- 出处：<https://ibiling.cn/novel-editor>、<https://ibiling.cn/scene/cont-w>

**查不到的**
- 上下文组成、单次长度、落盘方式、中断策略、额度。标注 **【不可查证】**。

---

## 4. 对 spec-r19 的具体建议（按重要度，最多 6 条）

> 只提改动点，不改生产代码；每条给出理由与依据。涉及 spec 的章节号均指 `docs/spec-r19-outline-and-chapter-continuation.md`。

**建议 1（最高）：把 §7.1「逐轮落盘」改为「候选区累积 + 一次性应用」，或在逐轮落盘前保存 `content` 快照并支持一键回滚。**
- 理由：当前设计在 `--apply` 模式下按轮写正文，失败/取消时会把半成品留在正文，且没有撤销入口；主流产品都用候选区或可撤销分支规避这一点（Sudowrite 卡片不 Insert 不进正文、Novelcrafter 不 Apply 不算正文、NovelAI 有分支时间线）。
- 依据：Sudowrite Write/History、Novelcrafter Apply/Retry/Discard、NovelAI Undo/Retry。

**建议 2：把 §4 的长度控制从「轮数 × 每轮字数」改为「单次目标字数预设/滑杆（如 400/800/1200/2000）+ 硬上限」，轮次只作为内部实现，不暴露给用户。**
- 理由：能查到的竞品基本按「用户能理解的字数/预设」控制（Sudowrite 50–1,500/2,000、彩云 50–2000、Novelcrafter 预设、NovelAI Output Length）；「轮数」是编排细节，用户无法据此预期产出长度。
- 依据：Sudowrite Write 文档、彩云应用商店描述、Novelcrafter 课程页、NovelAI Slider Settings。

**建议 3：在 §2.2 / §6.2 增加「重抽」按钮（同参数重生成并替换当前候选，不落盘）；多候选仍留二期。**
- 理由：R19 目前只有「生成 → 预览 → 应用」，没有「不满意再换一版」的低成本路径；而重抽是竞品标配（番茄「换一换」、彩云「换一批」、Novelcrafter「Retry」、Sudowrite 多卡片）。
- 依据：番茄官方公告、彩云应用商店描述、Novelcrafter 课程页、Sudowrite Write 文档。

**建议 4：在 §2.1 的大纲续写里补两项——(a) 除现有「最近 10 章标题/摘要」外，再把「上一章结尾原文」作为显式衔接输入；(b) `start_index` 发现大纲与章节文件序号不一致时，改为「返回冲突并要求确认」，而不是静默按最大值续写。**
- 理由：蛙蛙明确支持把上一章结尾填作衔接、Sudowrite 自动链接前章；静默 `max+1` 会把序号空洞掩盖成正常续写，属于数据一致性问题。
- 依据：蛙蛙官方文章、Sudowrite Chapter Continuity、R19 §2.1 现有 `start_index` 规则。

**建议 5：把 §3.3 降级顺序中的「全剧梗概」从「第一个删」改为「固定保留（可压缩到 300 字）」，并把「禁止重复参考」从「末尾 500 字」升级为「最近 1~2 段 + 已写节拍摘要」。**
- 理由：主流把 story-so-far 摘要视为防跑偏的核心素材（Novelcrafter `storySoFar`、Sudowrite linked Outline summary），且 Claude 官方明确长上下文召回会下降；删摘要保世界细节，方向相反。仅给末尾 500 字，模型仍可能复述更早的情节。
- 依据：Novelcrafter Prompt Examples/storySoFar、Sudowrite Chapter Continuity 丢弃顺序、Claude context windows 文档。

**建议 6：把 §7.1 的「失败轮整轮丢弃」改为「半成品保留为未应用草稿（status=failed），用户可继续编辑/应用/丢弃」；只有用户主动取消才允许直接丢弃。**
- 理由：竞品都不会让一次长生成白等（Sudowrite 卡片留在 History、Novelcrafter 未 Apply 文本仍在编辑器、NovelAI 可 History 选分支）；「整轮丢弃」会让用户在长上下文、慢模型下损失最大。
- 依据：Sudowrite History、Novelcrafter 生成后四按钮、NovelAI Edit History。

---

## 5. 对 spec §9 现有对标表述的更正

| spec §9 现有表述 | 核查结果 | 建议 |
| --- | --- | --- |
| 「NovelAI 上下文上限按订阅档位（约 3K/6K/8K tokens，另有资料称高档位 28K，存在冲突）」 | 官方已明确：Opus 28,672 tokens；Xialong 28,672 + 8,192 滚动窗口；低档更小 | 改为「Opus 28,672；其余档位更低，官方未在订阅页给具体数字」 |
| 「主流工具都做『生成 → 预览/可撤销 → 接受』，直接落盘是少数」 | 只对一半：Sudowrite/Novelcrafter 是候选区；NovelAI/番茄/彩云是**就地插入 + 撤销/分支/换一批** | 改为「候选区派 vs 就地插入派；共同点是都可撤销，没有不可撤销的直接落盘」 |
| 「没有一家把整本正文塞进 prompt」 | 成立，但要补上限：Sudowrite 前序文档 20,000 词 + 当前文档 20,000 词，约等于我们的 32K token 预算量级 | 补一句「主流是 2 万词量级的上限，不是无限」 |
| 「多候选体验好但成本×3，建议二期」 | 成立；但**重抽**是标配，成本仅 ×1 | 区分「重抽（一期）」与「多候选并排（二期）」 |

---

## 6. 不可查证清单（明确不编造）

| 项 | 状态 |
| --- | --- |
| Squibler 的单次长度控制、上下文组成、落盘方式、中断策略、额度 | 官方仅有营销页，**不可查证** |
| 番茄「续写」的单次字数上限、每日次数/额度 | 官方未公布，**不可查证** |
| 阅文作家助手「妙笔」的续写入口、长度、上下文、落盘 | 官方仅有版本说明与功能公告，**不可查证** |
| 七猫是否存在编辑器内续写 | 官方仅见 AI 小助理，**未查到续写机制** |
| 笔灵的上下文组成、长度控制、落盘 | 官方仅有功能宣传，**不可查证** |
| 各家「流式输出被打断后保留多少」 | 全部**不可查证** |
| 彩云小梦三条走向是否支持并排对比 | 官方未说明，**不可查证** |
| 各家「大纲增量合并冲突」的自动处理 | 未查到任何产品有公开机制，**不可查证** |
| NovelAI Output Length 的 4–600 单位（characters vs tokens） | 官方原文写 characters，与 28,672 token 上下文口径不一致，**官方未解释，不作推断** |

---

## 7. 出处清单

**Sudowrite**
- Write：<https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/write/pvxUvbQqYybfEosqx1sXjY>
- Chapter Continuity：<https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/chapter-continuity/4KL8gFeLZQ6GSBjDWtSbV6>
- Story Bible / Braindump：<https://docs.sudowrite.com/using-sudowrite/1owq1kGqof9rtcyGnrWUBS/what-is-story-bible/jmWepHcQdJetNrE991fjJC>
- Features（Draft 整章 3,000–5,000+ 词）：<https://docs.sudowrite.com/getting-started/dQph1snuwbfMWG9wRjsNug/features/dq7YUMNy5ZMvKUJiRAisyT>
- Credits：<https://docs.sudowrite.com/plans--account/wBnmhtSyMcWtk2BLzifGkz/what-are-credits/qzSpgs9UDPEtwYcdczkLd3>
- Pricing：<https://www.sudowrite.com/pricing>

**NovelAI**
- Models（上下文尺寸）：<https://docs.novelai.net/en/text/models>
- Editor（Send / Generate Inline / Undo / Retry / History）：<https://docs.novelai.net/en/text/editor>
- Story Settings（Memory / Author's Note / Lorebook）：<https://docs.novelai.net/en/text/editor/storysettings>
- Advanced Settings（上下文顺序/裁剪/预留）：<https://docs.novelai.net/en/text/editor/advancedsettings>
- Slider Settings（Output Length）：<https://docs.novelai.net/en/text/editor/slidersettings>
- Subscription（档位与 Opus 28,672）：<https://docs.novelai.net/en/subscription>

**Novelcrafter**
- Generating Prose with AI：<https://www.novelcrafter.com/courses/ultimate-beginners-guide/generating-prose-with-ai>
- Scene Beat vs Continue Writing：<https://www.novelcrafter.com/help/faq/write/scene-beat-vs-continue-writing>
- Prompt Examples（storySoFar/摘要）：<https://www.novelcrafter.com/help/reference/prompts/prompt-examples>
- Prompt Functions：<https://www.novelcrafter.com/help/reference/prompts/prompt-functions>
- Codex / Progressions：<https://www.novelcrafter.com/use-cases/romance-nsfw>
- Pricing：<https://www.novelcrafter.com/pricing>

**Squibler**
- Novel Writing Software：<https://www.squibler.io/novel-writing-software/>
- AI Book Writer：<https://www.squibler.io/ai-book-writer/>

**Claude**
- What are projects：<https://support.claude.com/en/articles/9517075-what-are-projects>
- Context windows（200K/1M、context rot、400 超窗）：<https://platform.claude.com/docs/en/build-with-claude/context-windows>
- Usage and length limits（自动摘要）：<https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work>

**番茄小说**
- AI 写作工具功能上线通知（2024-05-08）：<https://fanqienovel.com/writer/zone/article/7327136545129906238>
- 新手勇闯番茄：解锁三种写作编辑器（2025-07-17）：<https://fanqienovel.com/writer/zone/article/7455639656860762137>

**阅文作家助手**
- 版本说明（妙笔章纲智能提取）：<https://write.qq.com/portal/version>
- 挂载章节问答：<https://write.qq.com/portal/community/topic/detail/89608351800941501>
- 官方介绍页：<https://write.qq.com/portal/advertising>
- 时间线旁证（36kr）：<https://36kr.com/p/3501232140474501>

**七猫**
- AI 小助理公告（2023-07-31）：<https://www.qimao.com/gonggao/64c711e2727af1330b0af2e6/>

**彩云小梦**
- 应用商店功能描述：<https://m.yyb.qq.com/agreement/app-desc/?appid=54148715>
- 年费权益（10 万字/月）：<https://www.xiaomengai.com/h5/annual-fee>
- 长度/分支（二手）：<https://m.duote.com/android/1162933.html>

**蛙蛙写作**
- 官方工具升级说明：<https://wawawriter.com/notes/article/6ae831e076ceb17210b90a80>
- 续写正文介绍：<https://wawawriter.com/notes/article/6ae82904034e9a8221b909f6>

**笔灵**
- 小说编辑器：<https://ibiling.cn/novel-editor>
- 续写工具：<https://ibiling.cn/scene/cont-w>
