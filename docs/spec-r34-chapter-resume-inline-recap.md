# spec-r34 章节断点续写 + 摘要并入正文末轮 + 超纲处理

> 后续口径变更（2026-11-15）：输出侧已取消一切人为预算，`budgets.py` 只保留输入侧
> 上下文预算；文中涉及「预算」的表述按现行口径理解为「请求 max_tokens 取上下文剩余空间」。
> 超纲分级为 1.5 倍提示、2 倍给压缩/重写动作。

## 1. 目标

在不牺牲成文质量的前提下降低单章生成成本，并让超长章节有可控收尾：

1. **章内断点续写**：一章中途取消 / 失败 / 服务重启时，已完成轮次不再作废，下次生成从断点接上，不重跑已写内容，最终正文与一次跑完逐字一致。
2. **摘要并入正文末轮**：正文最后一轮同时产出正文与本章摘要，省掉每章一次单独的摘要请求（实测中位约 53 秒）；只有解析失败才回退到单独调用，并留下可读说明。
3. **超纲处理**：生成不限制长度，写完后按目标字数判偏差；≥2 倍时提供「按目标字数压缩」「重写」两个动作，必须用户点击才执行，压缩不得靠删节拍过关。

不改变：分轮写作的节拍边界、提示词前缀、内容校验口径、滚动摘要落盘结构、设置页与预算解析逻辑。

## 2. 断点续写

### 2.1 状态存放位置

工程目录下：

```
<工程根>/.ndw/checkpoints/<章节ID>.chapter.json
```

- 只保存生成中间态，不写章节正文文件；正文与摘要都成功落盘后立即删除。
- 原子写入（先写 `.tmp` 再 `os.replace`），进程被杀不会留下半截 JSON。
- 预览（`dry_run`）既不读也不写断点；显式重新生成（`regen=True`）先删旧断点。

### 2.2 存档字段

| 字段 | 含义 |
| --- | --- |
| `state_version` | 存档结构版本，不兼容时按失效处理 |
| `chapter_id` | 章节 ID |
| `signature` | 上下文签名：`base_revision`、`chapter_words`、`total_beats`、`per_round`、`beats[{summary,target_words,character_ids}]` |
| `base_revision` | 开始生成时的章节版本指针 |
| `book_summary_before` | 生成开始时的全书滚动摘要（末轮摘要的固定输入） |
| `groups` | 轮次 → 节拍 ID 的分组，便于人工核对 |
| `content` | 已完成轮次的累计正文（未做最终排版归一化） |
| `rounds_done` | 已完成轮数（轮次指针） |
| `beats_done` | 已完成节拍数 |
| `provider` | 产出模型 |
| `status` | `running` / `canceled` / `failed` |
| `summary` | 末轮已解析出的摘要（若有） |
| `summary_error` | 末轮摘要解析失败原因（若有） |
| `created_at` / `updated_at` | 写入时间 |

写入时机：**每写完一轮**（`on_round` 回调）。轮内被取消时该轮不算完成，不落盘，续写会重跑该轮。

### 2.3 恢复条件

同时满足才复用断点：

1. 章节 ID 一致，且 `state_version` 兼容；
2. 章节 `revision` 与 `base_revision` 一致（版本指针未变）；
3. `chapter_words`、`total_beats`、`per_round` 与节拍内容（`summary` / `target_words` / `character_ids`）签名一致；
4. 断点 `content` 非空且 `rounds_done ≥ 1`；
5. 本次不是 `dry_run`，且不是 `regen`。

任一不满足：删除断点、从头生成，并在结果 `notices` 里写明原因（例如「章节版本已变化（断点基于 revision 1，当前 revision 2）」）。

恢复后的请求构造与一次跑完完全相同：`system + 首条 user + assistant(已写正文) + user(本轮节拍指令)`。因此前缀缓存可继续复用，正文语义一致。

### 2.4 返回结构

`generate_chapter` 结果新增：

```json
{
  "resume": {
    "resumed": true,
    "from_round": 3,
    "reused_rounds": 2,
    "status": "canceled",
    "updated_at": "2026-11-15T15:00:00+08:00",
    "checkpoint": "<工程根>/.ndw/checkpoints/<章节ID>.chapter.json"
  },
  "requests": {"body": 2, "summary_fallback": 0, "total": 2}
}
```

- 未续写时 `resume = {"resumed": false}`。
- 续写来源同时写入 `notices`，CLI 与页面都会显示「断点续写：沿用前 N 轮已写正文，从第 N+1 轮继续（断点文件 …）」。
- 取消时章节接口的 `canceled` 事件附带同一份 `resume`，文案改为「已完成轮次已存为断点，下次生成本章会自动从第 N 轮继续」。
- 若断点时所有轮次都已写完，则不再请求模型，只补落盘与摘要，并明确说明。

## 3. 摘要并入正文末轮

### 3.1 输出约定

末轮（分轮模式为第 N 轮；单轮模式即那一次请求）在原有节拍指令后追加：

```
正文写完后，另起一行输出分隔标记 <<<CHAPTER_SUMMARY_JSON>>>
{"chapter_summary": "…150-300 字…", "book_summary": "…500-800 字，合并旧全书摘要…"}
正文中不得出现该标记；JSON 之后不要再写任何内容。
【已更新的全书摘要】<生成开始时的滚动摘要>
```

全书摘要输入固定为生成开始时的值（同时写入断点 `book_summary_before`），保证续写与一次跑完使用同一份输入。

### 3.2 结构化校验与回退

- `split_final_output` 按分隔标记拆分；模型漏标记时按 `"chapter_summary"` 键位切分，避免 JSON 混进正文。
- 正文部分照旧通过 `check_chapter_content` 校验（复述任务、罗列准则、列表占比过高等仍会触发严格重试）。
- 摘要 JSON 必须同时含非空 `chapter_summary` 与 `book_summary`，否则视为解析失败。
- 成功：`save_summary` 一次落盘（`chapter.synopsis` + `.ndw/rolling_summary.json`），**不再单独发摘要请求**。
- 失败：写明原因（如「本章摘要 JSON 解析失败：…」），回退 `update_rolling_summary` 单独请求；回退也会写入 `notices`，失败则 `summary_error` 明确回报。

### 3.3 `summary_mode` 四种取值

| 取值 | 含义 | 额外请求 |
| --- | --- | --- |
| `inline` | 末轮产出并通过校验，直接落盘 | 0 |
| `fallback` | 末轮摘要不可用，回退到单独摘要请求 | 1 |
| `failed` | 回退也失败 / 摘要落盘失败，`summary_error` 给出原因 | 0 或 1 |
| `preview` | `dry_run`，不产出摘要 | 0 |

### 3.4 输出上限口径

- 断点续写与内联摘要都**不依赖输出预算计算**，不新增 `max_tokens` 推导，也不做「正文预算 + 摘要预算」相加。
- 末轮与普通轮使用同一份请求上限口径；需要上限字段时由适配器按**上下文剩余可用空间**收敛（`_fit_output_budget`），不在业务层人为设限。
- 本特性未向 `budgets.py` 写入任何预算函数或常量。

## 4. 每章请求次数变化

设分轮数为 `N`（默认 4 拍自动分轮，`N=4`）：

| 场景 | 改动前 | 改动后 | 说明 |
| --- | --- | --- | --- |
| 分轮 + 内联成功 | `N + 1`（N 轮正文 + 1 次摘要） | `N` | 每章省 1 次摘要请求（中位约 53 秒） |
| 分轮 + 摘要回退 | `N + 1` | `N + 1` | 与改动前持平，仅多一次可读回退说明 |
| 单轮整章 | `2` | `1` | 正文与摘要共用一次请求 |
| 断点续写（已完成 `k` 轮） | 重新 `N + 1` | `N - k`（+ 0/1 摘要） | 已写轮次不再重跑 |
| 断点时轮次已全部写完 | — | `0`（仅补落盘/回退摘要） | 不再请求正文模型 |

内容校验触发严格重试时，会额外增加 1 次正文请求（原有行为，计入 `requests.body`）。

## 5. 依赖与边界

- 依赖：`writing/recap.py`（摘要约定、拆分、落盘）、`writing/budgets.py`（输入侧上下文预算）、`store`（工程文件）、`cancel.GenerationCanceled`（取消）。
- 不做：多章并行；不改变节拍划分与提示词前缀；不引入新的设置项；不改设置页与预算解析逻辑。
- 断点只对多轮模式生效；单轮模式取消时没有「已完成轮次」，不产生断点。

## 6. 验证

离线用例 `tests/test_chapter_resume.py`（5 项）：

1. 第 2 轮前取消 → 断点 `rounds_done=1`、`status=canceled`、正文未写入；再次生成只请求剩余 3 轮，`resume.from_round=2`，正文与 `dry_run` 一次跑完逐字一致，成功后断点删除。
2. 旧断点（`revision` 不匹配）→ 作废并从头跑满 4 轮，`notices` 含「断点已作废」。
3. 末轮返回可解析摘要 → `summary_mode=inline`、请求 4 次（无单独摘要）、`synopsis` 与滚动摘要写入，且末轮 `max_tokens` 与首轮一致（未额外抬高上限）。
4. 末轮摘要不可解析 → `summary_mode=fallback`、请求 5 次、正文正常、回退说明可读。
5. 离线多轮正文不带摘要块 → 同样回退，正文不受影响。

离线用例 `tests/test_chapter_overshoot.py`（4 项）：

1. 阈值：1.5 倍 `none`、1.7 倍 `notice` 且无动作、2.0 倍 `action` 且带两个动作与阈值文案。
2. 压缩：逐节拍保留、落盘版本 +1、分段标记不进入正文、说明含前后字数。
3. 压缩缺节拍：拒绝写入、正文与版本不变、错误文案含「不能靠删节拍压缩」。
4. 重写：结果含「重写前 / 重写后」字数与原因，正文正常覆盖。

另：`node --check` 通过；全量 `pytest tests` 中预算模块（`test_generation_budget.py`）与离线重复句守卫相关用例由对应改动路径收尾，本特性的 9 项用例全绿。

## 7. 回滚

- 删除 `writing/chapter_checkpoint.py` 与 `generate_chapter` 中断点相关分支即可恢复「取消即整章作废」。
- 摘要回退路径保留：去掉末轮附加的摘要约定即恢复「每章单独摘要请求」。
- 断点文件为纯中间产物，可直接删除，不影响已有章节。
- 超纲动作不影响正常生成：传 `mode="generate"` 时行为与改动前一致。

## 8. 超纲处理（压缩 / 重写）

生成过程不限制长度，写完后按目标字数判偏差，**不自动重跑**。

### 8.1 阈值（全工程唯一出处）

`service.py`：

```python
OVERSHOOT_NOTICE_RATIO = 1.5   # ≤1.5 倍：只在结果里标字数
OVERSHOOT_ACTION_RATIO = 2.0   # ≥2 倍：给出压缩/重写动作
```

不新增设置项。结果里的 `word_deviation` 会带回 `notice_ratio` / `action_ratio`，
页面直接展示「当前阈值 1.5 倍仅提示、2.0 倍及以上可压缩或重写」，前后端口径不会漂移。

| 实际 / 目标 | `level` | 动作 |
| --- | --- | --- |
| ≤ 1.5 | `none` | 无（只标实际与目标字数） |
| > 1.5 且 < 2.0 | `notice` | 无（提示已超纲，可自行精简） |
| ≥ 2.0 | `action` | `compress` + `rewrite`，需用户点击 |

### 8.2 动作执行

页面在超纲结果里展示两个按钮；点击后才以 `mode=compress|rewrite` 调用
`POST /api/ui/chapter/generate/stream`，不点不会执行。

- **按目标字数压缩**（`compress_chapter`）：
  - 输入现有正文 + 本章全部节拍，要求按 `【节拍 1】…【节拍 N】` 逐段输出；
  - 结构化校验：段数必须覆盖 1..N、顺序一致、每段非空，缺段或多段直接拒绝写入
    （错误文案含「不能靠删节拍压缩」）；不合格时追加约束重试 1 次；
  - 通过后去掉分段标记、按既有对白规范归一化、写入新版本（`revision +1`），
    保留原摘要不重新生成；
  - 结果带 `overshoot_note`：`已按「按目标字数压缩」处理：原 X 字（目标 Y 字，N 倍…）→ 压缩后 Z 字；本章 K 个节拍全部保留`。
- **重写**（`mode=rewrite`）：
  - 等同 `regen=True` 的整章重生成（断点清空、摘要按内联/回退正常处理）；
  - 结果带 `overshoot_note`：`已按「重写」处理：原 X 字（目标 Y 字，N 倍…）→ 重写后 Z 字`。

两个动作的结果都含 `mode`、`before_words`（压缩）、`word_deviation` 与前后字数说明，
页面与日志可直接读到「为什么执行、前后差多少」。

### 8.3 边界

- 压缩不做二次自动压缩：压缩后仍超纲时只在 `word_deviation` 里提示，是否重试由用户决定。
- 压缩不重新生成摘要：情节未变，沿用原 `synopsis` 与滚动摘要。
- 压缩失败不写章节、不改版本；原文可继续阅读，也可改用「重写」。

---

## 9. 退化熔断（2026-11-15 追加）

线上现象：正文流式输出反复循环同一段对白（「你有。」「我没有！」…），已接收 18121 字
仍显示 0%，只能等上下文被填满才停。本节是**退化保护**，不是长度上限。

### 9.1 检测

全部阈值集中在 `src/novel_drama/writing/degeneration.py`，出处见
《research-degeneration-and-length-control.md》第 2.2 / 6.3 节：

| 信号 | 口径 | 常量 |
| --- | --- | --- |
| S1 后缀周期重复 | 长度 6–120 字的片段连续出现 3 次 | `SUFFIX_MIN_PERIOD` / `SUFFIX_MAX_PERIOD` / `SUFFIX_REPEATS` |
| S2 n-gram 覆盖率 | 12 字 shingle 出现 ≥3 次，覆盖字符占比 ≥30% **且最长连续重复 ≥60 字** | `SHINGLE_SIZE` / `SHINGLE_MIN_COUNT` / `SHINGLE_COVERAGE_STOP` / `SHINGLE_MIN_SPAN` |
| S3 句级重复 | 去标点后同一句（≥6 字）在相邻几句里连续出现 ≥3 次（两次之间最多隔 2 句） | `SENTENCE_MIN_CHARS` / `SENTENCE_STOP_COUNT` / `SENTENCE_MAX_GAP` |

- 判定窗口：`max(1.5 × 本轮目标字数, 400)`，上限 2000（`resolve_window`）。
- 检查点固定在 120 字的整数倍上，只对文本前缀判定：同一文本换分片、重复跑结论一致。
- 压缩动作只启用 S1：原文本身可能带重复结构，S2/S3 会误杀正常压缩结果。

### 9.2 恢复阶梯（不整轮丢弃）

1. 命中后截到「循环起点前一句」：S1 会继续往回走，定位整段循环真正的起点，
   窗口外的重复也一起截掉。
2. 注入「推进指令 + 剩余字数」做局部续写，最多 `MAX_LOCAL_CONTINUATIONS = 2` 次。
3. 仍不收敛 → 追加硬约束整节重写 1 次。
4. 再失败 → 抛 `AdapterError`（`detail.code=degeneration`），本轮正文不落盘、断点不留。

每一步都通过 `on_notice` 留可读说明，例如：
`第 1 轮正文检测到重复退化（同一段连续重复 3 次（「…」））：已截回到循环起点前一句（保留 0 字），改为局部续写（第 1/2 次，剩余约 600 字）`。

## 10. 看门狗（止损线）

集中在本项目 `src/novel_drama/writing/watchdog.py`：

- `max_tokens = 本轮目标字数 × 1.65 × 0.886 token/汉字`（0.886 为 42 条真实请求实测口径）。
- 末轮同时产出摘要时加 `SUMMARY_EXTRA_CHARS = 600` 字对应额度。
- 与「上下文窗口 − 提示词占用量」取小；被压住时日志与说明里写明「被上下文压住」。
- 每轮目标字数与提示词同源（章节目标字数按节拍占比分配），不另造算法。
- 命中 `finish_reason=length`：保留已写正文、按句末收尾、局部续写；续写用尽才结束本轮
  （`finish_reason=watchdog`），正文不丢。
- 日志与界面统一说明「这是止损线，不是创作限制」。

## 11. 长度区间 / 补写 / 采样参数

### 11.1 长度写法

`src/novel_drama/writing/length_plan.py` 按目标字数推导档位与区间，提示词写成
「约 3000–3500 字（中长篇），分 4–5 段，每段约 600–800 字」，并明确禁止复述同一句对白、
同一句式；每轮另带「本章目标 X–Y 字；当前已写 A 字，剩余约 B 字；接近目标且到句末即可收尾」。
档位与区间集中一处，`generate_chapter` 日志记录用的是哪一档。

### 11.2 补写（`mode=supplement`）

- 触发：正文低于目标 `UNDERFILL_ACTION_RATIO = 0.7` 倍时，`word_deviation.actions` 给
  `{"id": "supplement", "label": "补写"}`；必须用户点击后执行。
- 未覆盖要点：节拍摘要关键词（中文 2 字滑窗）命中率低于 `SUPPLEMENT_COVERAGE_RATIO = 0.34`
  视为没写到。
- 结构：要求按 `【补写节拍 N】` 逐段输出，缺段/多段拒绝写入并追加约束重试 1 次。
- 合并：只追加不改写，裁掉与已有结尾重叠的部分；结果带 `before_words`、`added_words`、
  `gap_words`、`supplemented_beats`、`overshoot_note`（写清补了多少字、补的是哪几个要点）。
- 压缩 / 补写 / 重写的倍数阈值同处 `service.py`，页面按后端返回值显示，前端不写死。

### 11.3 采样参数

- 先探测服务端 `/openapi.json` 支持的字段（按 endpoint 缓存），只对 `chapter` /
  `chapter_compress` / `chapter_supplement` 下发。
- 温和档：`repetition_penalty` 默认 1.05（vLLM 会连提示词一起罚，强度必须 ≤1.05）；
  llama.cpp 系加 DRY 温和档；不开 `no_repeat_ngram_size`。
- 实际下发参数与来源写日志；`omlx.repetition_penalty` 可覆盖，超过 1.05 会打警告。

## 12. 用例与结论

- `tests/test_degeneration.py`：窗口推导、三类信号、正常文本不误杀、局部续写收敛、
  重写后仍重复则失败且不落盘、看门狗数值/摘要额度/上下文取小/命中不丢正文。
- `tests/test_chapter_supplement.py`：不足阈值给动作、缺要点拒绝、只追加不改写、
  达到阈值拒绝执行。
- `.venv/bin/python -m pytest -q tests/`：116 项通过。
