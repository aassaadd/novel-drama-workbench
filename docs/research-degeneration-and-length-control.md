# 长文生成的"复读退化"与篇幅控制：业界做法、参数与阈值建议

- 调研日期：2026-11-15
- 适用场景：本地模型（OpenAI 兼容 HTTP 接口）按节拍分段生成中文小说正文
- 本文性质：调研文档，不含业务代码改动；所有"实测"均指引用来源方自己的实测，本次调研**未在本项目模型上跑过任何实验**
- 证据分级（全文统一标注）：
  - **【官方】** 推理框架/接口的官方文档或源码默认值
  - **【论文】** 有 arXiv/ACL 论文或其中数据支撑
  - **【生产】** 工程团队的线上生产报告（非同行评审）
  - **【社区】** 教程、厂商博客、社区经验，未做对照实验
  - **【推断】** 本文基于上述材料的推理，**没有直接实证**

---

## 0. 结论先行

1. 采样层的所有"惩罚类"参数都只能**降低**复读概率，不能**保证**退出循环；循环一旦形成，模型对重复内容的置信度会自我强化，越写越难退出（【论文】Holtzman 2020；"Wait, Wait, Wait… Why Do Reasoning Models Loop?" 2025）。因此**检测 + 截断/回退是必需品，不是可选项**。
2. 对中文短对白循环（「你有。」「我没有！」），**DRY 存在已被论文指出的盲区**：序列分隔符会把惩罚封顶，最长无分隔符片段 ≤7 token 的循环有相当比例能存活（【论文】"When Do Repetition Loops Lock?" 2026）。中文标点不在 DRY 默认 breaker 列表里，实际表现需要实测。
3. 本项目当前"输出侧不设人为上限、`max_tokens` 直接吃掉剩余上下文"的策略，会把一次退化放大成一次满上下文生成。建议把 `max_tokens` 改为**看门狗**（约 1.6–1.7 × 目标汉字数），真正的止损交给流式检测。
4. 字数控制上，**单点字数指令不可靠**：论文实测在严格长度基准上，naive 提示的合规率不足 30%，加"计数脚手架/剩余预算反馈"后可到 95% 以上（【论文】CAPEL 2025；动态反馈 2026）。落地优先级：外部计数 + 区间目标 + 每轮剩余预算，而不是继续调提示词措辞。
5. 本项目最划算的组合：**流式后缀/句级重复检测（客户端） + 局部回退续写 + 看门狗 max_tokens + 每轮剩余字数反馈 +（若服务端支持）温和的窗口化惩罚或 DRY**。不建议上 `no_repeat_ngram_size` 和高强度 `repetition_penalty`。

---

## 1. 采样层：反重复参数与默认值

### 1.1 参数总表

| 参数 | 提供方 | 语义 / 作用范围 | 默认值 | 写作常用区间 | 中文对话体的主要风险 |
|---|---|---|---|---|---|
| `repetition_penalty` | vLLM、SGLang、MLX-LM、llama.cpp、Ollama | 乘性惩罚：对出现过的 token 的 logit 除以系数（>1 抑制重复） | 1.0（MLX 文档写作 0.0=disabled） | 1.03–1.12 | 高频虚词（的/了/你/我/他）被反复压制，出现语病；vLLM 的实现**把 prompt 也算进"出现过"** |
| `frequency_penalty` | OpenAI、vLLM、SGLang、MLX-LM、llama.cpp、Ollama(API) | 加性惩罚，按 token 出现次数线性累加 | 0.0 | 0–0.15 | 长输出中虚词累计被罚到不可用；中文单字重复率天然高 |
| `presence_penalty` | 同上 | 加性惩罚，token 只要出现过就罚一次，与次数无关 | 0.0 | 0–0.3 | 能压制"复述整句"，但也会削弱有意重复（口头禅、排比、称呼） |
| `no_repeat_ngram_size` | HF `generate()`；部分自研服务 | 硬约束：同一 n-gram 只允许出现一次 | 关闭（设 >0 才启用） | 中文创作建议 0 | 极硬；中文 1 token≈0.6 字，n=3 相当于禁掉大量两字组合，正常行文会被破坏 |
| DRY（`dry_multiplier`/`dry_base`/`dry_allowed_length`/`dry_penalty_last_n`） | llama.cpp（ExLlamaV2、text-generation-webui 同源） | 序列级：候选 token 若会把当前后缀延长成上下文中已出现的片段，按指数惩罚 | multiplier 0（关）、base 1.75、allowed_length 2、penalty_last_n 64 | multiplier 0.8（0.5–1.2）、base 1.75、allowed 2、last_n -1 或 512–1024 | 短对白循环的惩罚会被 breaker 截断（见 1.3） |
| XTC（`xtc_probability`/`xtc_threshold`） | llama.cpp | 以一定概率排除最高概率 token，鼓励多样性 | prob 0（关）、threshold 0.10 | prob 0.3–0.5、threshold 0.1 | 创意写作向；可能牺牲连贯，属社区经验 |
| `min_p` | llama.cpp、SGLang、MLX-LM、vLLM | 相对最高概率截断（保留 p ≥ min_p × p_max 的 token） | llama.cpp 0.05；MLX/SGLang 0.0 | 0.02–0.1 | 本身不反重复，但允许在更高温度下保持连贯（【论文】min-p 2024） |
| `repeat_last_n` / `repetition_context_size` / `*_context_size` | llama.cpp、Ollama / MLX-LM | 惩罚回看窗口 | 64 / 64 / 20 | 256–1024（正文）、512（presence） | **默认窗口太短**：64 token≈40–60 汉字，抓不到跨段、跨回合的循环 |
| `min_tokens` / `min_new_tokens` | vLLM / SGLang | 强制最少生成 token 数（防早停，不防重复） | 0 | 目标字数对应 token 的 50%–70% | 与字数下限配合使用 |
| `max_tokens` / `num_predict` | 通用 | 硬上限 | vLLM=剩余上下文；Ollama -1（无限）；MLX-LM 512 | 1.6–1.7 × 目标汉字数（看门狗） | 只应作为止损，不应作为创作篇幅手段 |

来源：vLLM [SamplingParams](https://docs.vllm.ai/en/stable/api/vllm/sampling_params/)、[OpenAI 兼容服务](https://docs.vllm.ai/en/stable/serving/openai_compatible_server/)，SGLang [Sampling Parameters](https://docs.sglang.io/docs/basic_usage/sampling_params)，llama.cpp [llama-server(1)](https://manpages.debian.org/experimental/llama.cpp-tools/llama-server.1)，Ollama [Modelfile](https://docs.ollama.com/modelfile)，MLX-LM [SERVER.md](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md)，HF [Generation](https://huggingface.co/docs/transformers/main/en/main_classes/text_generation)。

### 1.2 三个必须知道的语义差异【官方】

1. **`repetition_penalty` 的作用范围不同**
   - vLLM 文档原文：惩罚"whether they appear in the prompt and the generated text so far"——**提示词里的字也算**。长上下文小说生成时，等于把前文用过的字全部轻罚一遍，强度一大就伤正常行文。
   - llama.cpp / Ollama 是**窗口内**（`repeat_last_n`，默认 64）回看，只影响最近的 token。
   - MLX-LM 有独立 `repetition_context_size`（默认 20），窗口极短，且文档写"默认 0.0（disabled）"，与 vLLM/Ollama 用 1.0 表示关闭**不一致**，字段含义必须先探测再填。
2. **频率/存在惩罚的累加方式不同**
   - OpenAI 语义（vLLM/SGLang/MLX 沿用）：`logit[j] -= c[j] * α_frequency + 1(c[j]>0) * α_presence`，其中 c[j] 是此前采样次数。OpenAI 接口限定范围 -2..2，默认 0。
   - vLLM 的这两个惩罚作用于**全部已生成 token**，没有窗口参数；输出越长，常见字被罚得越狠。
3. **`no_repeat_ngram_size` 不是"惩罚"，是硬禁止**
   - HF 文档原文："all ngrams of that size can only occur once"。一旦触发，模型只能绕开，容易出现语义断裂；对中文创作不建议开启。

### 1.3 DRY 的实测效果与盲区

- 【论文】DRY 原文（"Don't Repeat Yourself: Stopping Verbatim Loops at Sampling Time", 2026）：suffix-extension rate 降低 47%，AWQ 量化的 70B/120B 上循环率约减半，同时 MT-Bench/MMLU/GSM8K 不掉点；并明确指出 repetition/presence/frequency penalty 与 n-gram blocking "只在会破坏格式或流畅度的强度下才抑制循环"。
- 【论文】"When Do Repetition Loops Lock?"（2026）指出 DRY 的盲区：breaker（默认 `\n`、`:`、`"`、`*`）会把惩罚封顶为 `λ·b^(g-1-a)`（g 为最长无 breaker 片段长度）。实验里 **g=6 的锁定单元有 61% 在 DRY 下仍然锁死，g=8 时降到 0.2%**；自由生成中所有 199 个存活循环的 g ≤ 7。
- **对本项目的直接含义【推断】**：中文短对白循环往往就是 5–15 个汉字一轮（g 很小），正好落在 DRY 的盲区里；中文标点（。！？""」）不在默认 breaker 里，实际 g 会更长、惩罚更强，但也会误伤正常排比。结论：DRY 可以用，但**不能拿它替代检测与截断**。

### 1.4 中文对话体的实际效果与副作用

- 【社区】重复惩罚的通用区间：1.05–1.15 为"温和"，1.2–1.3 为"激进"，1.5+ 质量明显下降（Aspose 参数文档）；另一份参数教程给出"2.5 时连 a/is/the 这类功能词都被重罚，句子无法成立"的失败模式（AI Parameter Playbook）。中文没有对应公开对照实验，但机制相同：**惩罚不知道哪些重复是修辞、哪些是退化**。
- 【社区】本地写作常见组合：温度 0.8–1.1、top_p 0.9–0.95、repeat_penalty 1.1 左右；也有教程建议大上下文场景 repeat_penalty 1.15、`num_ctx` 32768、`min_p` 0.05（KDnuggets）。另有排障文章建议 `repeat_penalty` 1.0–1.05、不要超过 1.2、`repeat_last_n` 64（runaihome）。
- 【推断】中文正文的推荐做法：**宁可把惩罚调轻、把窗口拉长**，让"整句复述"由检测层处理；不要靠 1.2+ 的 `repetition_penalty` 硬压，否则最先坏掉的是"的、了、着、他、她、你、我"这类无法回避的字。
- 【推断】温度过低（<0.5）时开放式续写更容易进入"最高概率就是上一句"的定势；本项目正文 0.8 属于合理区间，不建议为了反重复把温度降到 0.5 以下。

---

## 2. 检测层：线上如何发现退化并止损

### 2.1 服务端内置能力

| 框架 | 内置重复检测 | 结论 |
|---|---|---|
| vLLM | **有**：`repetition_detection: RepetitionDetectionParams` | 官方：`max_pattern_size`（>0 启用）、`min_pattern_size`（默认 0，生效时按 1 处理）、`min_count`（≥2）。命中后"generation will be ended early"。默认全关。可通过 OpenAI 兼容接口的 extra 字段传入 |
| SGLang | 未在采样参数文档中提供 | 需客户端检测 |
| llama.cpp / Ollama | 未提供（只有采样惩罚） | 需客户端检测 |
| MLX-LM（oMLX 大概率同源） | 未提供 | 需客户端检测 |

- 【官方】vLLM 的能力边界：文档只说明"提前结束生成"，**未说明 `finish_reason` 的取值**；本次调研未查到，落地前需用一次请求确认（否则客户端无法区分"正常结束"与"被检测掐断"）。
- 【生产】Reducto 在文档解析 VLM 上的生产数据（2026-09）：用 vLLM 原生检测器，保守参数取得 **100% precision / 98.0% recall**，命中位次中位数是 **第 1813 个 token**；相对 32,768 token 上限，平均每次失败省下约 **30,955 个 token**。他们同时报告两类漏检：**长周期重复**（936 token 的表格段重复 4 次）和**带规律变化的重复**（2074-2075-2076…、A→Z→AA 计数）。这两类在中国网文里对应"整段重抄"和"换名字重演同一场戏"。
- 【生产】检测之后他们没有"一律重试"，而是按失败类型走不同兜底（换管线，或换采样策略重试，例如 olmOCR 2 的 dynamic temperature scaling）。这与本项目"局部回退"方向一致。

### 2.2 客户端流式检测：4 个信号与阈值

> 说明：vLLM 以外的服务端拿不到 token 级信息，但有 SSE 文本增量，足够做字符级检测。以下阈值是**工程建议值**，除注明外均为【推断】，需按本项目 20–50 条正常样本 + 10 条已知循环样本标定。

设目标字数为 T（汉字），检测窗口 `W = clamp(1.5 × T, 400, 2000)` 字。

| 信号 | 判定 | 阈值 | 级别 | 依据 |
|---|---|---|---|---|
| S1 后缀周期重复 | 末尾存在长度 p ∈ [6, 120] 的片段连续出现 3 次（字符级精确匹配） | `min_count=3` | 硬止损 | vLLM 的 `min_count≥2` 机制【官方】；社区检测器用 3 次【社区】；文档解析域生产实践命中位次中位数 1.8k token【生产，非中文小说域】 |
| S2 n-gram 覆盖率 | 窗口内 12 字 shingle 出现 ≥3 次，其覆盖字符占比 | ≥30% 硬止损；20%–30% 软告警 | 硬/软 | n-gram 重复率是数据清洗与退化检测的通用指标【论文/官方：阿里云 LLM-N-Gram Repetition Filter】；阈值为【推断】 |
| S3 句级重复 | 去标点归一化后，同一句（≥6 字）在窗口内出现次数 | ≥3 硬；=2 软 | 硬/软 | 对话体最直观的信号；阈值为【推断】 |
| S4 压缩率 | `len(gzip(窗口)) / len(窗口)` 相对正常样本中位数的比值 | <0.55× 软；<0.40× 硬 | 软/硬 | 压缩率检测有论文用法（zlib 比率 <0.30 判定退化），但那是英文 GSM8K 语料【论文】；中文小说域未标定，只能做辅助信号 |
| S5 语义重复 | 相邻段落 embedding 相似度 / 小模型判定 | >0.9 | 软 | Antislop 明确指出采样器只能防"逐字重复"，防不了"同义反复"【论文】；成本高，建议离线抽检 |

补充：
- 【社区】开源检测器的参数可作参照：`dsh-loop-detector` 扫描 8–256 字符的周期、64 字符块在窗口内出现 ≥3 次即判定、并用 512 字符尾块做"整段重演"锚点；`pi-no-spin` 用 `period × threshold` 的周期一致性判定。二者思路与上表 S1/S2 一致。
- 【推断】检测窗口不要只看"最近 200 字"：整段重抄的周期可能超过 500 字（Reducto 的漏检案例是 936 token），所以 S1 的 p 上限给到 120 字、S2 窗口给到 1.5T 是必要的。

### 2.3 检测到之后：截断、回退还是重试

| 动作 | 做法 | 成本 | 风险 | 建议 |
|---|---|---|---|---|
| 硬截断 | 截到循环起点前最后一个句末标点，保留前文 | 极低 | 可能丢一点有效内容 | 默认第一步 |
| 局部回退续写 | 保留前缀 → 注入"推进指令 + 剩余字数" → 续写一次 | 低（有 prefix cache 时只重算尾部）【推断】 | 可能再次进入同一循环 | 默认第二步 |
| 换采样重试 | 温度 +0.05~0.1、presence +0.1、开 DRY / 换 seed | 中 | 风格漂移 | 同一节最多 1 次 |
| 整节重写 | 丢弃本节，重写并加强结构约束 | 高（prompt 重新 prefill） | 时间成本最高 | 连续 2 次失败后 |
| 直接报错 | 保留原文、标记失败、交人工 | 低 | 需要产品兜底 | 不建议静默吞掉 |

关键取舍【推断】：整节重写会把这节的 prompt（往往几千 token）重新 prefill 一遍；局部回退只多花"循环段之后的几十到几百 token"。生产数据也显示，检测的价值主要来自"早停"，而不是"重试"（Reducto 省的是 3 万 token 的无效生成）。

---

## 3. 结构层：分段、上下文与提示词

### 3.1 分段/分场景

- 【推断】这是本项目已经在做的方向，业界也普遍这么做，但**没有查到"分段 vs 整章一次生成"的对照实验**。可引用的间接证据：
  - 生成长度越长，退化概率越高；循环形成后模型置信度会自我强化（【论文】Holtzman 2020；"Why Do Reasoning Models Loop?" 2025）。
  - 训练数据中的重复会传导为生成退化（【论文】"Repetition In Repetition Out" 2021）。
- 【推断】建议单次输出目标控制在 **600–1200 汉字**（约 400–800 token）；超过 1500 字再拆一拍。这个区间既能摊薄循环损失，也不至于让节拍过碎、上下文反复重述。

### 3.2 上下文长度与内容

- 【推断】不要在上下文里大段重复"已写正文"：模型会把已出现的句子当成高概率续写目标，重复上下文本身就是循环的温床（机制同上：重复 → 置信度上升 → 更难退出）。
- 【推断】建议上下文只保留：最近 1–2 拍原文（每拍 ≤800 字） + 前情摘要 + 本节必须发生的事件清单；更早的正文用摘要替代。
- 【官方/推断】`max_tokens` 不要等于"剩余上下文"：上下文越满，循环造成的损失越大；看门狗上限见 7.4。

### 3.3 提示词里的"禁重复"约束

- 【推断】"不要重复""禁止复读"这类**纯负面指令效果很弱**，且会占用注意力；未查到量化研究。
- 更有效的替代【推断，基于长度控制论文的同类结论】：
  1. **正向推进约束**："本轮必须发生一件上文没有的事：___；必须有一个角色改变立场/离场/拿出新物件"。
  2. **说话人轮换约束**："每个说话人每轮只能推进一个新信息，不得重复上一轮台词"。
  3. **收束指令**："写到 850 字进入收束，最后一句留钩子"——把"停"变成结构化任务，而不是靠模型自觉。
- 提示词中给出**具体反例**（"不要出现『你有。』『我没有！』这类来回"）属于 few-shot 纠偏，【社区】常用，但可能被模型照抄，需谨慎。

---

## 4. 字数控制：产品怎么做、提示词怎么写、事后怎么修

### 4.1 写作类产品的实际做法

| 类型 | 做法 | 来源 |
|---|---|---|
| 通用 API | 只给 `max_tokens`/`max_completion_tokens` 硬上限 + 提示词里的字数要求；超长直接截断 | 【官方】OpenAI/vLLM 文档 |
| 网文 AI 工作流 | 单章 2000–3000 字；拆成 2–3 个小节，每节明确"约 1800 字"，再脚本拼接；单章生成设上限保护，避免失控爆字 | 【社区】51CTO 教程、番茄写作指南、博客园长篇小说教程 |
| 结构化提示 | "本章 2000–3000 字、对话占比 40%+、章末留钩子"——把字数与结构、节奏一起写 | 【社区】番茄/网文提示词实践 |
| 研究原型 | 计数脚手架（CAPEL）：让模型边写边输出倒计时标记，严格合规率从 <30% 提升到 >95%（MT-Bench-LI，gpt-4.1）；随机长度 1–1000 上接近 99% | 【论文】CAPEL 2025 |
| 研究原型 | Plan-and-Write：先列结构再写，MAPD（平均绝对百分比偏差）最多改善 37.6% | 【论文】2025（Amazon Science） |
| 研究原型 | 动态长度反馈：生成中把"已写/剩余"回灌给模型，MAE 与精度都显著改善（Qwen3-8B 等） | 【论文】2026 |
| 研究原型 | DcLM：把"目标长度/已写/剩余预算"编码成动态长度标记 | 【论文】ACL 2026 Findings |

结论【推断】：产品侧真正稳定的做法是"**外部计数 + 分段 + 剩余预算反馈**"；纯提示词单点字数（"写 800 字"）在严格基准上合规率很低（论文实测 <30%），只能当软目标。

### 4.2 提示词写法（由弱到强）

1. 弱（不推荐单独用）：`请写约 800 字。`
2. 中（推荐作为基线）：
   ```
   本节目标 800 字（按汉字计，不含标点），允许 700–900 字。
   写到 850 字时必须进入收束：完成当前动作、给出一句钩子，不再开启新对话回合。
   ```
3. 强（长度敏感场景）：在 2 的基础上，每轮把实际进度写回去：
   ```
   当前已写 420 字，剩余约 380 字。
   剩余必须覆盖：① 主角发现信物 ② 反派出现。请按此顺序收束。
   ```
4. 计数脚手架（CAPEL 思路的中文改造，【推断】）：
   - 原文是"每个词/字配一个倒计时标记"，中文按字做标记不现实（2500 字=2500 个标记）。
   - 可行改造：**每 100 字**在段末插一个 `〔剩余 500〕` 标记，并要求标记数递减；或每段末写 `[420/800]`。这保留了"把计数外化成可见 scratch pad"的机制，密度降低一个量级。**该改造未经实证**。

### 4.3 回合与事后处理阈值

设目标 T，实际 L，偏差 `d = (L - T) / T`。

| 区间 | 处理 | 说明 |
|---|---|---|
| `-15% ≤ d ≤ +15%` | 接受 | 与项目现有"1.1 倍以内"的要求基本一致 |
| `-30% < d < -15%` | 补写一轮（指明补什么） | 不要泛泛"再写长点" |
| `d ≤ -30%` 且节拍未完成 | 继续写 | 属于正常未写完 |
| `d ≤ -30%` 且节拍已完成 | 重写（信息密度不足） | 说明模型在敷衍 |
| `+15% < d ≤ +30%` | 删减式压缩（保情节、删描写/重复修饰） | 一次改写调用 |
| `+30% < d ≤ +50%` | 整节重写（加强结构约束） | 压缩容易伤逻辑 |
| `d > +50%` 或触发重复检测 | 判失控：截断 + 重生成 + 记录样本 | 不再做"压缩补救" |

- 轮次上限【推断】：单节最多 3 次模型调用（1 主 + 2 修）；累计 token 不超过 `2.5 × T` 对应的 token；超过则接受现状或报错，避免"越修越长"。
- 停止条件【推断】：达到 `0.85T` 且停在句末即停；不要为了凑满 T 再开一轮，否则必然超量。

### 4.4 解码层可行性（OpenAI 兼容接口）

| 方案 | 通用 HTTP 接口可行性 | 说明 |
|---|---|---|
| `max_tokens` 硬上限 | 可行 | 所有服务端都支持；只能截断，不能"恰好写满" |
| `min_tokens` / `min_new_tokens` | 取决于服务端 | vLLM / SGLang 支持；可防止模型过早 EOS，但不能保证内容质量 |
| `ignore_eos` | vLLM 支持 | 强制写满上限，创作场景不建议 |
| 长度受控解码（自定义 logits processor / 训练/RL） | **不可行** | 需要进程内访问模型；vLLM 的 custom logits processor 只能 Python 进程内用，且与 MTP 不兼容（【生产】Reducto 明确说明"推理引擎开箱不支持自定义检测逻辑"） |
| 结构化输出/grammar | 可行但无用 | 只能约束格式，不能约束长度 |
| 投机解码/动态温度 | 取决于服务端 | olmOCR 2 用 dynamic temperature scaling 做重试兜底（【生产】），属"换采样重试"的一种 |

### 4.5 汉字 ↔ token 换算

- 【官方】Qwen 文档：英文约 3–4 字符/token，中文约 1.5–1.8 汉字/token。
- **以本项目实测为准（口径已更正）**：本项目适配器对同一提示词服务做了 42 条真实请求校验，
  实测 **0.886 token/汉字**（即约 1.13 汉字/token），与厂商文档的 1.5–1.8 汉字/token 不一致
  （厂商口径偏乐观，实际分词更细）。换算统一用：`tokens ≈ 汉字数 × 0.886`，
  即 800 汉字 ≈ 709 token。
- 看门狗：`max_tokens ≈ 1.65 × T_汉字 × 0.886`（系数与换算集中在本项目
  `src/novel_drama/writing/watchdog.py`）；开启 thinking 时要单独加预算，
  本项目写作默认关闭 thinking，按上式即可。
- 若换模型/换量化，先用一次 `usage.completion_tokens` 与输出汉字数反推实际比率，
  再改常量（【推断】）。

---

## 5. 总对照表

| 方法 | 解决什么 | 成本与风险 | 是否适合本项目 |
|---|---|---|---|
| `repetition_penalty` 1.05–1.1 | 抑制 token 级复读 | 低；伤高频虚词；vLLM 会连 prompt 一起罚 | 有条件适合（窗口化、轻量） |
| `frequency_penalty` 0.05–0.15 | 抑制同一 token 反复出现 | 低；长文累计副作用大 | 谨慎，短节拍可用低值 |
| `presence_penalty` 0.1–0.2 | 抑制整句复述 | 低；削弱有意重复 | 适合（本项目当前未启用） |
| `no_repeat_ngram_size` | 硬禁重复片段 | 语义断裂风险高；中文 token 粒度不匹配 | 不适合 |
| DRY 0.8/1.75/2 | 序列级循环 | 中；短对白循环存在论文指出的盲区；需 llama.cpp 系服务端 | 有条件适合（服务端支持时） |
| XTC | 提升多样性 | 低；可能不连贯 | 可选，非必需 |
| `min_p` 0.05 | 高温度下保持连贯 | 低 | 适合（可替代 top_p） |
| 长窗口 `repeat_last_n` 256–1024 | 覆盖跨段循环 | 低；窗口越长误伤越多 | 适合（配轻惩罚） |
| vLLM `repetition_detection` | 服务端早停 | 零客户端成本；需 vLLM；`finish_reason` 未确认 | 视服务端 |
| 客户端后缀/句级检测 | 任何服务端都能早停 | 低；需调阈值 | **强烈适合** |
| 局部回退续写 | 用最低成本跳出循环 | 低；可能再次入循环 | **强烈适合** |
| 整节重写 | 兜底 | 高（重新 prefill） | 适合作为最后一步 |
| 分段/分节拍 | 摊薄循环损失 | 低；节拍过碎会重复铺垫 | 适合（已在做） |
| 上下文去重（摘要替代旧正文） | 降低循环诱发 | 中；丢失细节 | 适合（需摘要质量） |
| 提示词"禁止重复" | 弱约束 | 几乎零成本，效果弱 | 只作辅助 |
| 正向推进/说话人轮换约束 | 从结构上避免原地打转 | 低 | **强烈适合** |
| 区间字数 + 剩余预算反馈 | 提高篇幅命中率 | 低；需每轮计数 | **强烈适合** |
| 计数脚手架（CAPEL） | 严格长度 | 中；标记污染文本、需后处理 | 可选（长度敏感场景） |
| 事后压缩/补写 | 修正偏差 | 中；压缩伤逻辑 | 适合（按 4.3 阈值） |
| 长度受控解码/RL | 精确长度 | 高；需权重与训练 | 不适合 |

---

## 6. 推荐组合与具体默认值

### 6.1 基线（不改服务端能力，先做这四件事）

1. **看门狗 `max_tokens`**：`ceil(1.65 × T_汉字)`，T 为当前节拍目标字数；不再传"剩余上下文"。
2. **流式检测**：S1（3 次同段）+ S2（12 字 shingle 覆盖率 30%）+ S3（同句 ≥3 次），窗口 `max(1.5T, 400)`。
3. **回退策略**：命中 → 截到循环起点前一句 → 注入推进指令与剩余字数 → 续写；同一节最多回退 2 次，再失败整节重写 1 次，仍失败报错。
4. **字数反馈**：每轮提示词写"目标 800 字（允许 700–900）；已写 X 字，剩余约 Y 字"；到 0.85T 且句末即停。

### 6.2 采样参数（按服务端分档）

| 服务端 | temperature | top_p / min_p | 反重复 | 备注 |
|---|---|---|---|---|
| MLX-LM / oMLX（需先探测字段） | 正文 0.85；润色 0.6 | `min_p 0.05`（若支持），top_p 保持 1.0 | `repetition_penalty 1.05` + `repetition_context_size 512`；`presence_penalty 0.1` + `presence_context_size 512`；`frequency_penalty 0` | MLX 文档默认 `repetition_penalty 0.0` 表示关闭，**字段语义与 vLLM 不同，必须实测** |
| llama.cpp / llama-server | 0.9 | `min_p 0.05`，top_p 0.95 | DRY：`dry_multiplier 0.8`、`dry_base 1.75`、`dry_allowed_length 2`、`dry_penalty_last_n 512`；`repeat_penalty 1.05`、`repeat_last_n 256`；`xtc_probability 0`（可选 0.3/0.1） | DRY 建议排在 temperature 之后（社区经验）；中文可追加 breaker：`。！？""」` |
| vLLM / SGLang | 0.85 | `min_p 0.05` 或 top_p 0.95 | `repetition_penalty 1.0–1.03`（会罚 prompt，务必轻）；`presence_penalty 0.1`；`frequency_penalty 0–0.05`；vLLM 可开 `repetition_detection{min_pattern_size:8, max_pattern_size:128, min_count:3}` | 长上下文正文不建议靠 vLLM 的 `repetition_penalty` |
| Ollama | 0.85 | `min_p 0.05` | `repeat_penalty 1.05`、`repeat_last_n 256`；`presence_penalty 0.1`（API 支持，Modelfile 文档未列） | 版本差异大，先 `ollama show` 确认 |

- 【推断】以上"正文 0.85 / min_p 0.05"来自 min-p 论文与社区写作参数的折中，本项目原值 0.8 也可保留，先只加 `min_p` 和检测，避免一次改太多变量。
- 【官方】vLLM 的 `repetition_detection` 三个参数默认全 0（关闭），`min_count` 必须 ≥2；启用后"生成会提前结束"。

### 6.3 检测参数（默认值）

```yaml
window_chars: max(1.5 * target_chars, 400)   # 上限 2000
suffix_period: {min: 6, max: 120, repeats: 3}   # S1 硬
shingle: {n: 12, min_count: 3, coverage_warn: 0.20, coverage_stop: 0.30}  # S2
sentence: {min_len: 6, warn_count: 2, stop_count: 3}  # S3
compression: {enabled: false, warn_ratio: 0.55, stop_ratio: 0.40}  # S4，需先标定
```

- 触发后的动作与上限见 2.3。
- 误报保护【推断】：如果命中位置在全文 <20% 处且前文没有重复，先按"软告警"处理（只加约束、不截断），避免把正常的排比/口头禅误杀。

### 6.4 字数参数（默认值）

| 项 | 值 |
|---|---|
| 目标写法 | `T` 的 0.875–1.125 倍作为提示词区间（如 800 → 700–900） |
| 停止 | `L ≥ 0.85T` 且句末即停 |
| 续写轮次 | ≤2；累计 ≤1.3T |
| 压缩阈值 | `L > 1.15T` 触发；`>1.3T` 改为整节重写 |
| 补写阈值 | `L < 0.7T` 且节拍已完成 |
| 单次输出目标 | 600–1200 汉字；超过 1500 字拆拍 |
| 看门狗 | `max_tokens ≈ 1.65 × T_汉字` |

### 6.5 灰度与验证（建议顺序）

1. **先探测服务端字段**：用同一 prompt、`temperature=0`，分别发 `repetition_penalty: 1.0` 与 `2.0`（或 `presence_penalty: 0` 与 `1.0`），输出若完全一致说明字段被忽略；再用 `/v1/models` 与服务端日志确认。**不同服务端的 0/1 语义不同，不能照抄。**
2. **只开检测，不改采样**：跑 30 节拍，统计命中率、误报率、平均节省 token、回退成功率。
3. **再加轻量采样**：`min_p 0.05` +（若支持）窗口化 `repetition_penalty 1.05`，对比"对话回合数/千字"与"重复片段占比"。
4. **最后试 DRY**：仅 llama.cpp 系；重点看中文短对白是否仍存活（对应论文 g≤7 盲区）。
5. 指标建议【推断】：每千字重复片段数、最长连续重复长度、检测命中率、单节平均调用次数、字数偏差分布（P50/P90）、人工可读性抽检 20 条。

---

## 7. 未查到实证、属于推测的结论（务必按此口径使用）

1. **中文对话体的具体检测阈值**（12 字 shingle、30% 覆盖率、同句 3 次）没有公开对照实验，是从通用 n-gram 指标和生产检测器的参数迁移来的，**必须本地标定**。
2. **"分段生成能降低复读"** 没有查到直接对照实验；本文的依据是"长度越长退化越多""重复上下文会强化循环"这类间接机制。
3. **提示词"禁止重复"的有效性**没有量化研究；本文建议用"正向推进/说话人轮换"替代，属于机制推理。
4. **事后压缩/补写阈值（15%/30%/50%）** 是工程经验，不是论文结论。
5. **gzip 压缩率阈值在中文小说域未标定**；论文里的 0.30 是英文 GSM8K 响应的退化判定线，不能直接搬。
6. **oMLX 是否支持 `repetition_penalty` / `presence_penalty` / `min_p` 等字段未实测**；它大概率基于 MLX-LM 服务端【推断】，但本项目适配器还会下发 `cache_prompt`、`prompt_cache_key` 等非 MLX 原生字段（见 [writing.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L1079-L1082)，服务端不支持时通常忽略），说明实际服务端可能是魔改版或并非 MLX-LM，必须逐字段探测。
7. **DRY 对中文短对白的效果**是英文模型实验（g=6 存活 61%）的外推；中文标点与分词不同，实际可能更好也可能更差。
8. **"温度 0.85 + min_p 0.05"** 是写作社区与研究论文的折中值，本项目未做 A/B。
9. **CAPEL 的"每 100 字一个剩余标记"改造**没有实证，只是把"计数外化"机制降频后的合理猜测。
10. **vLLM `repetition_detection` 命中后的 `finish_reason` 取值**未查到，落地前需实测，否则无法区分正常结束。

---

## 8. 关键来源

**官方文档 / 源码默认值**

- vLLM SamplingParams（`repetition_penalty` 1.0、`frequency/presence` 0.0、`min_tokens`、`RepetitionDetectionParams`）：https://docs.vllm.ai/en/stable/api/vllm/sampling_params/
- vLLM OpenAI 兼容服务（`repetition_detection` 会提前结束生成）：https://docs.vllm.ai/en/stable/serving/openai_compatible_server/
- SGLang Sampling Parameters（惩罚项与 `min_new_tokens`）：https://docs.sglang.io/docs/basic_usage/sampling_params
- llama-server 参数与默认值（`repeat_last_n 64`、`repeat_penalty 1.00`、`min_p 0.05`、`dry_multiplier 0`、`dry_base 1.75`、`dry_allowed_length 2`、`xtc 0/0.10`）：https://manpages.debian.org/experimental/llama.cpp-tools/llama-server.1
- llama.cpp CLI README（DRY 默认值 1.75/2/64）：https://github.com/ggml-org/llama.cpp/blob/master/tools/cli/README.md
- Ollama Modelfile（`repeat_penalty` 默认 1.0、`repeat_last_n 64`、`min_p 0.0`、`num_predict -1`）：https://docs.ollama.com/modelfile
- Ollama 上下文默认值（4k/32k/256k 按显存）：https://docs.ollama.com/context-length
- MLX-LM Server 请求字段（`repetition_penalty` 默认 0.0、`repetition_context_size 20`、`presence/frequency` 默认 0、`max_tokens 512`）：https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md
- HF Generation（`no_repeat_ngram_size` 语义）：https://huggingface.co/docs/transformers/main/en/main_classes/text_generation
- OpenAI Chat Completions API（`frequency_penalty`/`presence_penalty` 范围 -2..2）：https://platform.openai.com/docs/api-reference/chat/create
- Qwen 核心概念（中文 1.5–1.8 汉字/token）：https://qwen.readthedocs.io/zh-cn/stable/getting_started/concepts.html

**论文**

- Holtzman et al., The Curious Case of Neural Text Degeneration（nucleus sampling）：https://arxiv.org/abs/1904.09751
- Welleck et al., Neural Text Degeneration with Unlikelihood Training：https://arxiv.org/abs/1908.04319
- Fu et al., Repetition In Repetition Out（训练数据重复导致退化）：https://arxiv.org/abs/2310.10226
- Weidmann et al., Don't Repeat Yourself: Stopping Verbatim Loops at Sampling Time（DRY；suffix-extension -47%）：https://arxiv.org/abs/2608.22761
- Chen & Wang, When Do Repetition Loops Lock?（DRY 盲区；g=6 存活 61%）：https://arxiv.org/abs/2610.24715
- Paech et al., Antislop（backtracking 采样器，8000+ 模式）：https://arxiv.org/abs/2510.15061
- Nguyen et al., Turning Up the Heat: Min-p Sampling：https://arxiv.org/abs/2407.01082
- Keskar et al., CTRL（重复惩罚的提出）：https://arxiv.org/abs/1909.05858
- 2025, Wait, Wait, Wait… Why Do Reasoning Models Loop?（循环中置信度自我强化）：https://arxiv.org/html/2512.12895v1
- 2025, Prompt-Based One-Shot Exact Length-Controlled Generation（CAPEL，合规率 <30% → >95%）：https://arxiv.org/html/2508.13805
- 2025, Plan-and-Write（MAPD 最多改善 37.6%）：https://arxiv.org/pdf/2511.01807v1
- 2026, Can LLMs Track Their Output Length?（动态长度反馈）：https://arxiv.org/html/2601.01768
- 2026, DcLM: Output Length Control via Dynamic Length Markers：https://aclanthology.org/2026.findings-acl.1158.pdf

**生产实践 / 工程博客**

- Reducto, Reliable repetition detection for small VLMs（vLLM 检测器生产数据；长周期与规律变化漏检）：https://reducto.ai/engineering/posts/reliable-repetition-detection-for-small-vlms/
- llama.cpp DRY 参数实践指南（0.8 起步、sampler 顺序、局限）：https://llama-dry-docs.site/view/docs
- Aspose.LLM RepetitionPenalty 参数区间（1.05–1.15 / 1.2–1.3 / 1.5+）：https://docs.aspose.com/llm/net/developer-reference/parameters/sampler/repetition-penalty/
- AI Parameter Playbook：惩罚过强的失败模式：https://nitinkc.github.io/AI-parameter-playbook/learning/05-repetition/
- 本地模型复读排障（repeat_penalty 1.0–1.05、不超过 1.2）：https://runaihome.com/blog/local-llm-repeating-gibberish-fix-2026/
- KDnuggets：大上下文场景 repeat_penalty 1.15、min_p 0.05：https://www.kdnuggets.com/tweaking-local-language-model-settings-with-ollama
- 网文提示词实践（2000–3000 字/章、对话占比）：https://tataxiezuo.com/tomato-novel-ai-writing/
- 长篇小说 AI 工作流（单章字数上限保护、稳定字数优先）：https://www.cnblogs.com/xiezuo/p/23358193
- 分段生成 + 拼接扩篇幅（每节约 1800 字）：https://edu.51cto.com/article/note/59861.html
- 社区循环检测器参数参照：https://www.npmjs.com/package/dsh-loop-detector 、https://www.npmjs.com/package/pi-no-spin

---

## 9. 与本项目现状的衔接（只做说明，不改代码）

- 当前请求体只带 `temperature`（正文 0.8）、`stop`、`max_tokens`（等于剩余上下文）与 thinking 开关；`top_p`、`min_p`、各类 penalty 均未下发。见 [writing.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/adapters/writing.py#L1037-L1086)。
- 已有 `metadata.extra_body` 合并入口，可在不改业务代码的前提下做参数灰度（同上，L1083–L1085）。
- 输出预算目前明确"不设人为上限"，见 [budgets.py](file:///Users/zhaohaochen/git/novel-drama-workbench/src/novel_drama/writing/budgets.py#L1-L10)；本文建议区分"看门狗上限"与"创作目标字数"。
- 本文所有建议参数均未在本项目模型上验证；落地前先按 6.5 做字段探测与小样本灰度。

---

## 7. 阈值实测标定（2026-11-15，tt1 真实正文）

样本：tt1 工程 25 章中已写出正文的 10 章（约 5.2 万字）+ `.ndw/versions/` 18 份历史草稿，
共 28 份、19.2 万字。判定窗口按本项目口径（`max(1.5 × 目标字数, 400)`，上限 2000），
检查点固定为 120 字的整数倍，逐前缀扫描。

| 指标 | 正常正文（25 份） | 确认退化的草稿（3 份） |
| --- | --- | --- |
| S1 后缀周期重复 | 0 | 1 |
| S2 覆盖率最大值 | 4.2% | 36%–59% |
| S2 最长连续重复 | 28 字 | 64–290 字 |
| S3 同句最大出现次数 | 3（但间隔远，**紧凑连串恒为 1**） | 5–7 |

据此调整（原则：宁可漏判，也不误杀正常正文）：

- **S2**：保留 30% 覆盖率，新增「最长连续重复 ≥60 字」。tt1 正常正文连续重复最长 28 字，
  退化草稿最短 36 字；取 60 有约 2 倍余量。只看覆盖率会被模板化文本推到 30% 以上。
- **S3**：新增「紧凑」约束——同一句连续 3 次出现之间最多隔 2 句。tt1 正常正文里同一句
  会隔很远再现（如「九斤看周也。」在 2 万字里出现 3 次），加约束后误报归零。
- **S1**：不变（6–120 字片段连续 3 次），tt1 上零误报，且能抓住最短的对白来回。

结果：改前 11/28 报警（7 份正常正文被误杀），改后 3/28 全部为真退化（0 误杀）。
代价：一份覆盖率 29.7%、连续重复 64 字的退化草稿现在漏判（低于 30%），由 S1/S3/看门狗兜底。

稳定性：此前覆盖率在「32% / 29%」之间摆动，根因不是集合或浮点，而是**检查点跟着上游
分片长度走**（`_checked = len(text)`），窗口末端随分块边界漂移。改为固定网格后，
同一文本任意分片、任意重复次数结果一致。
