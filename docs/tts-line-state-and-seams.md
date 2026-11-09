# 台词行状态、变更说明与接缝裁切 设计

> 状态：设计定稿（未实现）
> 关联：`docs/design-v2.md` §5（TTS）、`docs/data-model.md` §7（台词行）
> 目的：把「行状态 / 过期判定 / 变更说明 / 撤回 / 生成范围 / 原文对照 / 接缝裁切 / 虚拟分组」这一束绑在一起的设计一次定清，避免拼接核心改一遍、行状态再改一遍。

---

## 0. 为什么这两件事必须一起定

实测前提（2026-11，本机）：**24 字短切分单独使用，成品时长 +29%**；加上静音裁切后回落到接近单段水平。这说明：

1. 切分粒度不是独立参数，它的代价由「接缝静音」承担，必须同表定义；
2. 裁切参数一旦进入合成链路，就必须参与缓存键与过期判定，否则改了参数不会重跑；
3. 过期判定一旦从 `revision` 改成「参数指纹」，行状态、变更说明、撤回三者就共用同一份快照。

所以本文件把「快照（sidecar v2）」作为共同地基，其余七项都从它派生。

---

## 1. 结论摘要（改动边界）

| 项 | 是否动 `schema_version` | 新增持久化 | 说明 |
| --- | --- | --- | --- |
| 行七态 | **不动** | 无 | 五态落盘，两态派生 |
| 「改 N」变更说明 | **不动** | 无 | 复用侧车（sidecar）快照 |
| 行级撤回 | **不动** | 无 | 写回快照参数，指纹命中即 ready |
| 范围选择器 | **不动** | 无 | 纯 CLI 参数 |
| 原文 / 当前对照 | **不动** | `.ndw/imports/` 快照文件 | 不是 schema 数据 |
| 接缝静音裁切 | **不动** | 无 | 配置项 + 指纹参与 |
| 虚拟分组 | **不动** | `.ndw/groups/<chapter>.json` | 刻意不放章节 JSON |
| CLI 接口 | **不动** | 无 | 全部是命令层 |

**唯一的数据格式变更**：音频侧车 `.wav.json` 从隐式 v1 升到显式 `payload_version: 2`，新增 `params` 与 `fingerprint` 两段。侧车不是作品数据，不参与 `schema_version` 校验（校验入口见 `docs/data-model.md` §1340）。

> 把指纹镜像进 `tts.fingerprint`（需要 schema 1.1）在 §9 列为**备选，不建议现在做**。

---

## 2. 行状态机（七态）

### 2.1 现状

落盘字段只有 `tts.status`，初值 `pending`，另有 `asset_id` / `duration_ms` / `adapter` / `voice_id` / `synthesized_revision` / `error`（[`models/entities.py:124-141`](../src/novel_drama/models/entities.py)）。过期判定是纯版本比对（[`orchestration/pipeline.py:101-119`](../src/novel_drama/orchestration/pipeline.py)）：

```python
if tts.get("status") != "ready": return True
if tts.get("synthesized_revision") != line.get("revision"): return True
```

### 2.2 七态定义

| 态 | 来源 | 含义 | 是否落盘 |
| --- | --- | --- | --- |
| `pending` | 落盘 | 从未生成，或产物缺失需重做 | 是 |
| `queued` | 落盘 | 已入队未开始 | 是 |
| `running` | 落盘 | 正在推理 | 是 |
| `ready` | 落盘 | 产物存在且指纹命中 | 是 |
| `failed` | 落盘 | 上次失败，`tts.error` 有值 | 是 |
| `stale` | **派生** | 产物存在，但当前参数指纹 ≠ 快照指纹 | **否** |
| `skipped` | **派生** | 不参与配音：`text.strip()==""`，或 `kind ∈ tts.skip_kinds`（默认空） | 否 |

落盘只允许这五个值，`stale` / `skipped` 写入即视为非法（写入口做断言）。

### 2.3 派生规则（读取时计算，一次性完成）

```
1) asked = tts.status
2) 若 skipped 条件成立                     → skipped（覆盖一切）
3) 若 asked ∈ {queued, running}
   且不存在覆盖该行的活跃批次              → pending（僵尸态降级，不写盘）
4) 若 asked == ready
   4a) asset 文件不存在                    → pending
   4b) 侧车存在且 fingerprint 不等         → stale
   4c) 侧车不存在（旧数据）                → synthesized_revision != revision ? stale : ready
   4d) 角色绑定 voice_id 与 tts.voice_id 不等 → stale
   4e) 否则                                 → ready
5) 其余原样返回
```

规则 3 的「活跃批次」判定复用 `.ndw/tts_batches/*.json` 里的 `status=running` 且未超时（默认 30 分钟无心跳视为死亡）。

### 2.4 为什么 `stale` 不落盘

落盘就必须维护「什么时候把它改回去」，而真相是「产物 + 快照」。workbench 的教训正是这点：它每次打开台本都重算状态，不信存下来的 `status`（`_heal_status`）。我们采用同样的原则，但**只在读取路径计算**，不产生写放大——一本书 500 行，`line list` 只读一次侧车目录。

### 2.5 需要的读取接口（内部）

```python
def resolve_line_state(line, characters, snapshot_index, active_batches) -> dict
    # 返回 {"state": "stale", "persisted": "ready", "reason": "param_changed", "changes": [...]}
```

`changes` 直接复用 §3 的 diff 结果，避免 UI 再算一遍。

---

## 3. 「改 N」变更说明

### 3.1 快照存在哪

侧车文件 `out.wav.json`，与音频同目录（现在由 [`tts/service.py:338-350`](../src/novel_drama/tts/service.py) 写出）。v2 结构：

```json
{
  "payload_version": 2,
  "text": "……",
  "provider_id": "cosyvoice3_mlx",
  "model": "Fun-CosyVoice3-0.5B-2512-8bit",
  "model_version": "…",
  "variant_id": "vav_xxx",
  "fingerprint": "sha256:9f2c…",
  "params": {
    "emotion": "sad",
    "emotion_intensity": 0.5,
    "speed": 1.0,
    "delivery_note": null,
    "voice_id": "voi_xxx",
    "instruct": null,
    "chunk_limit": 24,
    "chunk_silence_ms": 120,
    "edge_trim_ms": 100,
    "hard_split_ratio": 0.5
  },
  "dimensions": {"sample_rate": 48000, "duration_ms": 3210},
  "chunks": 3,
  "routing": {"chosen": "cosyvoice3_mlx", "degradations": []},
  "created_at": "2026-11-10T…"
}
```

### 3.2 比哪些维度

按用户能理解的粒度分组，不逐字段罗列：

| 组 | 字段 | 展示 |
| --- | --- | --- |
| 文本 | `text` | `文本 +12/−3 字`（不做行内高亮，UI 用逐字 diff） |
| 情绪 | `emotion`, `emotion_intensity` | `情绪 neutral → sad`、`强度 0.5 → 0.8` |
| 音色 | `voice_id`, `variant_id` | `音色 旁白A → 旁白B` |
| 演绎 | `speed`, `delivery_note` | `语速 1.0 → 0.95`、`交付说明已改` |
| 引擎 | `provider_id`, `model_version` | `引擎 mlx → http（模型更新）` |
| 切分 | `chunk_limit`, `chunk_silence_ms`, `edge_trim_ms` | `切分 24→18 字`、`接缝 120→80ms` |

CLI 输出最多 5 条，超出折叠成「等 N 项」。**不比较** `routing` 的候选顺序与 `latency_ms`，它们不是用户意图。

### 3.3 指纹怎么算

```python
FP_FIELDS = ("text","emotion","emotion_intensity","speed","delivery_note",
             "voice_id","variant_id","instruct","chunk_limit","chunk_silence_ms",
             "edge_trim_ms","hard_split_ratio","provider_id","model_version")

def line_fingerprint(line, binding, cfg) -> str:
    payload = canonical({f: resolve(f) for f in FP_FIELDS})
    return "sha256:" + sha256(payload).hexdigest()
```

两条硬规则：

1. **`revision` 不进指纹**。否则撤回后再也回不到 ready。
2. **浮点归一化到 3 位**（`round(x, 3)`）后再哈希。否则 `0.9500000001` 会造成整篇误判——workbench 为此专门加了「两边都不同才算过期」的二次判定。

---

## 4. 行级撤回

### 4.1 语义

`line revert` = **把参数类字段写回快照，文本不动**。

| 对象 | 是否回滚 | 理由 |
| --- | --- | --- |
| `emotion` / `emotion_intensity` / `speed` / `pause_before_ms` / `pause_after_ms` / `delivery_note` | 回滚 | 试错成本低，回滚是常态 |
| `voice_id`（若在行上覆盖） | 回滚 | 同上 |
| `text` | **不回滚** | 文本是用户的创作成果，回滚文本会静默吞掉编辑 |
| `kind` / `speaker_id` | 不回滚 | 属于结构化判定，改动应走审核流程 |

与 workbench 的差异要写清楚：它的 `revertLine` 是「整行参数恢复到生成时」（`web/longform.js`），我们的行文本编辑更频繁，所以只回参数。

### 4.2 撤回后的状态

- 若 `text` 与快照一致：写回参数 → `revision += 1`（沿用现有乐观锁）→ 重新算指纹 → **命中** → 状态 `ready`，**不触发重跑**，音频继续使用。
- 若 `text` 已改过：只回参数 → 指纹不命中 → 状态 `stale`，命令输出明确提示「文本已变更，仍需重生成」。
- 若侧车缺失：拒绝撤回，提示「无快照，无法撤回」（不给基于猜测的回滚）。

### 4.3 与 revision 机制怎么配合

`revision` 保留原职责：**并发写冲突检测与增量重算的粗粒度标记**（`build_tts_jobs` 的幂等键仍带 revision，[`pipeline.py:140`](../src/novel_drama/orchestration/pipeline.py)）。

`stale` 判定则**改由指纹负责**，两者分工：

| 机制 | 回答的问题 | 频率 |
| --- | --- | --- |
| `revision` | 「这份数据被别人改过吗？」 | 每次写 |
| `fingerprint` | 「这条产物还对得上当前参数吗？」 | 每次读/每次规划 |

副作用（必须接受）：`synthesized_revision < revision` 会成为常态，`is_line_stale` 不能再把它当唯一依据。

---

## 5. 生成范围选择器

### 5.1 四个范围与现有 CLI 的对应

| UI 范围 | 语义 | 现有 CLI | 现状 |
| --- | --- | --- | --- |
| 选中 | 指定 `line_id` 列表 | —— | **缺**，`tts run` 只有 `--chapter/--wait` |
| 待处理 | `pending + stale + failed`（含僵尸 queued/running） | `tts batch` 隐式全量跑 stale | **缺显式开关** |
| 场景 | 单个 `scene_id` | `tts batch --scene` | 已有 |
| 全章 | 整章 | `tts batch`（无 `--scene`） | 已有 |

### 5.2 需要的参数

```
ndw tts plan  --chapter CH [--scope selected|pending|scene|chapter]
                             [--line ID ...] [--scene ID] [--status pending,stale,failed]
ndw tts run   --chapter CH [--scope ...] [--line ID ...] [--scene ID] [--status ...]
                             [--dry-run] [--resume]
```

**并发约束（已拍板，不是可选项）**：并发固定为 1，**不提供 `--concurrency` 参数**。理由：单机单卡；CosyVoice3 0.5B 8bit 常驻内存高；`gpu_queue` 与 ComfyUI 共用 `/Volumes/other/gpu.lock`（`config.yaml` 的 `gpu.concurrency: 1`）。任何「提速」需求只能在块级缓存、批内复用、接缝裁切上做，不允许开并行推理。若将来要放开，必须先证明 MLX 下两实例显存与锁语义都成立，属于新设计，不在本文件范围。

- `--scope selected` 必须配 `--line`，否则报错（不猜）。
- `--status` 与 `--scope pending` 二者互斥，后者是前者的预设值。
- `tts plan` 输出四分类计数 + 预估耗时（用最近 20 条同 provider 的 `latency_ms` 中位数），供 UI 直接渲染成「0 句 / 37 句 / 2 组 / 全章 412 句」。

### 5.3 现在就能用的部分

`--scene`、整章、`--dry-run` 已可用；其余三项需要新增参数。**不需要改数据模型**，只是查询条件。

---

## 6. 原文 / 当前文本对照

### 6.1 数据来源

- **当前文本**：`chapter.content`（整体）。
- **原文快照**：`.ndw/imports/<sha256>.txt`（内容寻址，同一段原文跨章节共享一份）+ `.ndw/imports/index.json`：

```json
{
  "chp_001": {
    "snapshot_id": "e4f6c78c01fb89558bed8f20d0af7ff0",
    "file": ".ndw/imports/e4f6c78c…eb5d.txt",
    "sha256": "sha256:e4f6c78c…eb5d",
    "chars": 8421,
    "imported_at": "2026-11-08T…",
    "source_path": "/Users/…/novel/第1章.txt",
    "tool": "novel import"
  }
}
```

章节 JSON 只留 `source`（引用 + 哈希 + 字数 + 来源），正文一律不进章节 JSON。
文件名用内容哈希而不是 `<chapter_id>`：重复导入同一文件（或同一段原文落到两章）时**不重复落盘**，
`save()` 返回 `deduplicated: true`，`imported_at` 保持首次值。

### 6.2 为什么必须存快照

`chapter.content` 会被改写（续写、润色、人工修订），`vault/raw/` 也可能被替换。没有导入时刻的副本，就永远回答不了「我是不是漏了一段」——这是长篇改编唯一的完整性检查手段。

### 6.3 为什么不复用 block 实体

`docs/data-model.md` 里的 `block`（正文块，带 `char_start/char_end/text_hash`）目前**代码中尚未实现**（`models/entities.py` 无对应构造器）。设计不能建立在文档假设上，所以先用扁平快照，等 block 落地后再迁移为「快照 → block 映射」。

### 6.4 对照算法

```
原文段落 = split_paragraphs(import 快照)      # 按空行切，保留 char_start/char_end
当前段落 = split_paragraphs(chapter.content)
用 difflib.SequenceMatcher 对「去空白后的段文本」求 opcodes
输出：missing（原文有、当前无）/ added（当前有、原文无）/ modified / matched
```

对齐单位是**段落**而不是句子：`tts` 目录里的行已经过 24 字切分，与原文句子不是一对一，
按句子对齐会把整章判成「全都不匹配」；按段落对齐后，切分/合并行完全不影响对照结果。
段落 key 去掉所有空白再比对，因此缩进、空行、换行风格的变化不会误报 modified。

### 6.5 CLI

```
ndw chapter source --chapter CH                      # 快照元信息 + 段落数
ndw chapter source --chapter CH --diff               # 段落级 missing/added/modified + 偏移
ndw chapter source --chapter CH --paragraph 3        # 这段原文去哪了
ndw chapter source --chapter CH --at 1420            # 原文第 1420 字落在当前哪一段
```

- 无快照时返回 `{"available": false, "hint": …}`，退出码 0（查询无结果不是错误）。
- `--diff` 的条目带原文与当前的 `char_start/char_end`，UI 可直接跳到对应位置。
- `--paragraph` / `--at` 对不上时返回 `status: "missing"`，**不会**静默当成「作者已删除」；
  序号/偏移越界属于参数错误，退出码 2。

---

## 7. 接缝静音裁切

### 7.1 现状

每块之间无条件插入固定静音（[`tts/service.py:243-244`](../src/novel_drama/tts/service.py)），参数来自 `tts.segmentation.chunk_silence_ms`，默认 120ms（[`config.py:45`](../src/novel_drama/config.py)）。块自身的头尾空白照单全收，于是「设 120ms 实际听到 400ms+」，短切分把这条误差乘以块数——这就是 +29% 的来源。

### 7.2 新增配置（全部在 `tts.segmentation` 下）

```yaml
tts:
  segmentation:
    max_chars: 24
    chunk_silence_ms: 180       # 目标净间隔（不是插入量）
    edge_trim_ms: 100           # 每块头/尾各裁的目标上限
    edge_trim_max_ms: 400       # 硬上限，防吃掉弱起音
    silence_threshold_dbfs: -45 # 低于此视为静音
    keep_min_ms: 30             # 两端各保留，防切掉气口
    hard_split_ratio: 0.5       # 硬切处静音折扣
    trim_single_chunk: false    # 单块不裁，默认关
```

### 7.3 算法

```
对每块 audio:
  head = 从 0 开始连续低于阈值的时长
  tail = 从末尾向前连续低于阈值的时长
  cut_head = min(head, edge_trim_max_ms) - keep_min_ms   # 不小于 0
  cut_tail = min(tail, edge_trim_max_ms) - keep_min_ms
  实际裁剪 cut_head / cut_tail

块间插入:
  kept   = (tail(前块) - cut_tail) + (head(后块) - cut_head)  # 两侧实际保留的气口
  insert = max(0, chunk_silence_ms - kept)                     # 净间隔精确等于目标值
  若该处是硬切（chunk 末尾无句末标点）:
      insert = int(insert * hard_split_ratio)                  # 默认 0.5
```

> 2026-11-10 修订：原式 `max(chunk_silence_ms, 2*edge_trim_ms - trimmed)` 在 `trimmed=0`（块边缘本来没静音）时会插入 200ms，与目标值不符；改为按「实际保留的气口」扣减后，`trimmed=0 → insert=目标值`、`trimmed 很大 → insert=0`。

判定「硬切」的标记必须由 `split_text` 一并返回，现在只返回字符串列表，需要改成 `[(text, is_hard_split)]`——这是**内部返回值变更，不入盘**。

### 7.4 默认值与可验证目标

| 场景 | 目标 | 实测（2026-11-10，合成器头尾静音 250/350ms） |
| --- | --- | --- |
| 单块（≤24 字） | 与改造前完全一致（`trim_single_chunk=false`） | 逐样本完全一致 |
| 5 块、正常句末 | 成品时长相对改造前明显下降，净间隔等于 `chunk_silence_ms` | 21480ms → 18779ms；净间隔 180ms |
| 同一文本「切分 vs 不切分」 | 时长差 **≤3%**（改造前 +15.5%） | **+1.0%** |
| 硬切处 | 听感不断句：间隔为普通接缝的一半 | 插入 60ms（180×0.5） |

### 7.5 与缓存/状态的关系

`edge_trim_ms` / `edge_trim_max_ms` / `keep_min_ms` / `silence_threshold_dbfs` / `hard_split_ratio` / `chunk_silence_ms` 全部进指纹（§3.3），并同时进缓存键（否则改配置会命中旧产物）。改参数 → 全章 stale 是**预期行为**，不是 bug；文档和 CLI 提示都要写明。

### 7.6 不做的事

- 不做响度对齐（已有 −16 LUFS 两遍规整，在混音阶段）
- 不做交叉淡化（会改变时长，与时间轴锚点冲突）
- 不引入 ffmpeg `silenceremove`（numpy 逐块处理足够，且避免重采样）

---

## 8. 虚拟分组

### 8.1 存哪

`.ndw/groups/<chapter_id>.json`：

```json
{
  "version": 1,
  "groups": [
    {
      "id": "grp_a1b2",
      "name": "第一场 · 压抑对白",
      "query": {
        "speaker_ids": ["chr_li"],
        "kinds": ["dialogue"],
        "scene_ids": ["scn_003"],
        "line_ids": []
      },
      "overrides": {
        "emotion": "sad",
        "emotion_intensity": 0.7,
        "speed": 0.95,
        "pause_after_ms": 200
      },
      "updated_at": "2026-11-10T…"
    }
  ]
}
```

### 8.2 为什么放 `.ndw` 而不是章节 JSON

章节写入前有 schema 校验（`docs/data-model.md` §1340「失败拒绝落盘」）。加顶层字段等于动 schema 版本，而分组是**编辑期辅助数据**，不是作品内容，放在 `.ndw` 下可以随时删除重建，不污染作品文件。

### 8.3 与参数继承顺序怎么合并：物化，不运行时继承

原设想是「行 > 分组 > 角色 > 全局」运行时合并。**这条路走不通**，原因很具体：行对象的每个字段都有默认值（`emotion="neutral"`、`speed=1.0`、`emotion_intensity=0.5`，见 [`models/entities.py:124-141`](../src/novel_drama/models/entities.py)），运行时无法区分「用户显式设成 neutral」和「没设过所以是 neutral」。任何「分组只在字段未显式设置时生效」的规则都会误判。

因此改为**物化**：

```
ndw group diff  --chapter CH --group grp_a1b2   # 列出将影响的行与每个字段的 旧值→新值
ndw group apply --chapter CH --group grp_a1b2   # 实际写入行，revision += 1
```

- `apply` 之后分组与行不再有运行时耦合，参数继承顺序回到现有的「行 > 角色 > 全局」，无歧义。
- 分组改完再 `apply` 一次即可重新覆盖，`diff` 负责让用户先看清影响范围。
- 一个行**允许被多个分组影响**，按 `apply` 的先后形成覆盖链；`group diff` 必须显示「该行已被 grp_x 覆盖过」。

### 8.4 与「改 N」的互动

`apply` 会改参数 → 指纹变化 → 相应行变 `stale`。这和手动逐行改的语义完全一致，不需要额外设计。

三个实现细节：

- `apply` **只改行参数并 `revision += 1`，不写 `tts.status`**；状态一律由读取时按指纹派生。
- `pause_after_ms` 不在 `FP_FIELDS` 里（见 §4.3）。只改它的分组 `apply` 后行**不会**变 `stale`——
  时间轴参数不影响配音，这是有意为之；`group diff` 会在这种情形下显式提示，避免误以为没生效。
- 每次 `apply` 会给分组记 `applied: {at, lines}`，`list` 据此显示「已覆盖 N 行」，
  `diff` 据此显示「该行已被 grp_x 覆盖过」。

`group list` 额外给出 `ungrouped`（没有任何分组命中的行）与 `duplicated`（被 ≥2 个分组命中的行），
这是它相对「按角色设默认值」更有用的地方：后者只能表达「命中」，无法表达「漏掉」与「冲突」。

---

## 9. Schema 影响与备选

### 9.1 明确不动 schema

七项均在既有字段与侧车文件上完成：`tts.status` 五态 + 派生两态、侧车 `payload_version: 2`、`.ndw/imports/`、`.ndw/groups/`、config 新增键、CLI 参数。

### 9.2 唯一备选（不建议现在做）

在 `tts` 对象里加 `fingerprint` 字段，好处是 `line list` 不必读 500 个侧车；代价是：

- 必须升 `schema_version`；
- 侧车与字段两处真相，必然出现不一致；
- 旧数据迁移要重算全部指纹。

结论：先不做。若实测 `line list` 超过 300ms 再考虑，并把指纹**只作为缓存**（侧车仍是唯一真相）。

### 9.3 非 schema 的格式变更（需写迁移说明）

| 文件 | 变更 | 兼容策略 |
| --- | --- | --- |
| `*.wav.json` | 新增 `payload_version/params/fingerprint` | 无字段时回退 revision 判定，不报错 |
| `config.yaml` | `tts.segmentation` 新增 6 个键 | 缺省取默认值，老配置不改也能跑 |
| `.ndw/imports/` | 新目录 | 不存在即「无快照」 |
| `.ndw/groups/` | 新目录 | 不存在即「无分组」 |

---

## 10. CLI 接口草案（UI 后续按此接）

| 命令 | 关键参数 | 输出 | 对应 UI |
| --- | --- | --- | --- |
| `ndw line list` | `--chapter --line --status --json` | `{chapter, counts, lines[]}`，行含 `state/state_reason/changes` | P4 台词表 |
| `ndw line diff` | `--chapter --line --json` | `{line_id, state, changes[]}`；无快照时退出码 1 | 「改 N」浮层 |
| `ndw line revert` | `--chapter --line --json` | `{line_id, changed[], state, text_changed}`；无快照时退出码 1 | 撤回按钮 |
| `ndw tts plan` | `--chapter --scope --line --scene --status` | `{scope, selected, counts{pending,stale,failed,ready}, jobs}` | 范围选择器 |
| `ndw tts run` | 同上 + `--wait` | 入队条数 | 生成控制条 |
| `ndw tts batch` | `--chapter --chapters --all-chapters --scene --line --status --include-ready --dry-run` | 批次报告（NDJSON 流为后续项） | 批量控制 |
| `ndw chapter source` | `--diff --at --paragraph --json` | `{available, snapshot, summary, items[]}` 或单段定位结果 | 原文对照页 |
| `ndw chapter import` | `--on-duplicate skip/replace/new --force --force-replace --json` | `{mode, chapters[], snapshots[], skipped[], replaced[], refused[]}` | 导入对话框 |
| `ndw group list/diff/apply` | `--chapter --group` | 影响行与字段变更（`diff` 含 `covered_by`），`list` 含 `ungrouped/duplicated` | 分组面板 |
| `ndw tts seams` | `--chapter --dry-run` | 每行块数、裁切量、净间隔合计 | 切分预览 |

统一约定：

- 所有命令支持 `--json`，字段名与本文档一致，UI 不再做二次推导。
- `--status` 接逗号列表：`pending,stale,failed,ready`（历史值 `unknown` = 未识别说话人）。
- `--scope` 缺省推断：给了 `--line` → `selected`；给了 `--scene` → `scene`；否则 `pending`。
- `--scope pending` 与 `--status` 互斥（前者已固定为 `pending+stale+failed`）。
- **计数一致性**：`tts plan` 的 `jobs` 必须等于 `line list --status pending,stale,failed` 的行数；
  两者共用 `line_state.chapter_states` + `select_states`，输入（含 `root`/`live_ids`/`engine`）也必须一致。
- 退出码沿用工程既有约定：`0` 成功；`1` 拒绝执行（无参数快照，未做修改）；`2` 参数错误；`3` 适配器未对接；`4` 前置条件缺失；`5` 任务失败；`6` schema 校验失败。
- `chapter import` 重复命中**默认 `skip`**：输出「已存在章节 chp_xxx，跳过」并返回退出码 `1`（不静默）。
  判定顺序是「原文哈希 → 同来源文件 + 同标题 → 同序号 + 同标题」，改过正文再导入也能认出是同一章。
  `replace` 在目标章节已有台词行或已生成音频时**拒绝**并返回 `1`，需显式 `--force-replace`。
- 进度输出用 NDJSON（一行一个事件）仍是后续项，便于将来 SSE 直转。

---

## 11. 实施顺序、验收口径与改动文件清单

### 11.0 开工前置：冻结文件区

主会话正在改下面四个文件，**在接到放行前，任何阶段都不得编辑**。每个阶段都标了是否落在冻结区。

| 冻结文件 | 主会话在改什么 | 受影响阶段 | 冲突原因 |
| --- | --- | --- | --- |
| `src/novel_drama/text/splitter.py` | 对白 500 字上限 | 阶段 3 | 裁切需要 `split_text` 返回「是否硬切」，要改返回值 |
| `src/novel_drama/tts/cache.py` | 缓存批量写 | 阶段 3 | 裁切参数要进缓存 key，否则改参数不会重算 |
| `src/novel_drama/tts/batch.py` | 批量跨章续跑 | 阶段 1、2 | 批量路径要读侧车快照索引 |
| `src/novel_drama/tts/service.py` | 拼接逻辑 | 阶段 1、3 | 写侧车、块间静音都在这一个函数里 |

放行后按阶段顺序推进，每阶段独立验收、不合并提交。**阶段 3 完成前不动 UI**——否则行状态与时长同时变，出问题无法定位。

### 阶段 0：指纹与侧车模块（可立即开始，零冲突）

| 项 | 内容 |
| --- | --- |
| 新增 | `src/novel_drama/tts/fingerprint.py`：`FP_FIELDS`、`canonical()`、`line_fingerprint()`、`FP_VERSION` |
| 新增 | `src/novel_drama/tts/sidecar.py`：`dump(path, payload)` / `load(path)`，兼容 v1（无 `params/fingerprint` 时返回 `None` 快照，不抛错） |
| 新增 | `tests/test_fingerprint.py` |
| 改动现有文件 | 无 |
| 验收 | ① `pytest tests/test_fingerprint.py` 全过；② `0.95` 与 `0.9500001` 得到同一哈希；③ 对 demo 工程 3 个旧侧车调用 `load()` 不抛异常且 `snapshot is None`；④ 改动 `text` 任一字符哈希必变，改动 `latency_ms` 哈希不变 |

### 阶段 1：侧车 v2 写入（依赖阶段 0，冻结区）

| 项 | 内容 |
| --- | --- |
| 新增/加固 | `tts/service.py` 写侧车处改为 `sidecar.dump()`，补 `payload_version/params/fingerprint/dimensions` |
| 改动 | `tts/batch.py` 读取侧车时改用 `sidecar.load()`（等放行） |
| 验收 | ① 新合成 1 行，侧车含 `payload_version: 2`、10 个 `params` 字段、`fingerprint`；② 同参数连跑两次指纹一致；③ 改 `speed` 后指纹变化；④ 批量 3 行全部带指纹，旧侧车仍可读 |

### 阶段 2：过期判定改指纹（依赖阶段 1，无冲突）

| 项 | 内容 |
| --- | --- |
| 新增 | `src/novel_drama/tts/line_state.py`：`resolve_line_state()`（七态 + `changes`）、`diff_line()` |
| 改动 | `orchestration/pipeline.py`：`is_line_stale` 改为指纹优先、revision 兜底；`build_tts_jobs` 传入快照索引 |
| 改动 | `cli.py` 的 `cmd_line_list`（339 行起）改用 `resolve_line_state` |
| 验收 | ① 改 `speed` → `line list` 显示 `stale` 且 `changes` 含「语速」；② `line revert` 后 `state=ready`，`tts plan` 计数为 0；③ 只改 `text` 时 `changes` 只报文本；④ 杀进程后 `line list` 不出现僵尸 `running`（降级为 `pending`）；⑤ 旧侧车（无指纹）走 revision 兜底，行为与改造前一致 |

### 阶段 3：接缝静音裁切（依赖阶段 2，冻结区）

| 项 | 内容 |
| --- | --- |
| 新增 | `src/novel_drama/media/trim.py`：`detect_silence_edges()`、`trim_edges()`、`insert_ms()`（纯 numpy） |
| 改动 | `tts/service.py`：拼接处替换固定 `silence_ms`，改用 `trim.py` 计算（等放行） |
| 改动 | `text/splitter.py`：`split_text` 返回 `(text, is_hard_split)`（等放行） |
| 改动 | `tts/cache.py`：缓存 key 纳入 `edge_trim_ms`/`hard_split_ratio`（等放行） |
| 改动 | `config.py`：`tts.segmentation` 增 6 个键与默认值 |
| 验收 | ① 同一文本「切分 vs 不切分」时长差 ≤3%（当前 +29%）；② 单块输出与改造前逐样本一致（`trim_single_chunk=false`）；③ 5 块句末间隔实测 120±10ms；④ 硬切处间隔约为普通接缝一半；⑤ 改 `edge_trim_ms` 后相关行 `stale`；⑥ `tts seams --dry-run` 预估与实际渲染差 ≤5ms |

### 阶段 4：CLI 范围 + diff/revert（依赖阶段 2，无冲突）

| 项 | 内容 |
| --- | --- |
| 改动 | `cli.py`：新增 `cmd_line_diff`、`cmd_line_revert`；`cmd_tts` 增加范围参数 |
| 改动 | `cli_media.py`：`cmd_tts_batch` 增加 `--line/--status` 过滤 |
| 验收 | ① `tts plan --scope selected --line lin_x` 只统计该行；② 缺 `--line` 时退出码 2 且提示；③ `--scope pending` 计数 = `line list --status pending,stale,failed` 之和；④ `line revert` 在无侧车时拒绝并退出码 1；⑤ 所有新命令支持 `--json` 且字段与 §10 一致 |

### 阶段 5：原文快照与对照（无冲突）

| 项 | 内容 |
| --- | --- |
| 新增 | `src/novel_drama/store/imports_store.py`：写/读 `.ndw/imports/`，含 sha256 与 chars |
| 改动 | `cli.py`：`cmd_chapter_import` 落快照；新增 `chapter source` 子命令 |
| 验收 | ① 导入后 `.ndw/imports/<id>.txt` 与 `index.json` 均存在；② 人为删一段 → `chapter source --diff` 报 `missing` 且指出段落；③ 快照缺失时返回 `{"available": false}`；④ 24 字切分不导致整章判为 modified（按段落对齐） |

### 阶段 6：虚拟分组（依赖阶段 4）

| 项 | 内容 |
| --- | --- |
| 新增 | `src/novel_drama/store/groups_store.py`：`list/diff/apply`，写 `.ndw/groups/<chapter>.json` |
| 改动 | `cli.py`：`group list/diff/apply` 子命令 |
| 验收 | ① `group diff` 列出影响行与「旧值→新值」；② `apply` 后仅命中行变 `stale`，未命中行状态不变；③ 同一分组连续 `apply` 两次结果幂等（第二次 diff 为空）；④ 行被多个分组覆盖时 `diff` 明确提示；⑤ 章节 JSON 顶层键集合不变、且不出现 `groups` 字段；无改动的二次 `apply` 后章节 JSON 字节哈希完全不变（`apply` 本身要改行参数，字节必然变，故不按「任何情况下字节级不变」验） |

### 阶段 7：UI 接入（全部验收通过后）

按 §10 的命令契约接 P4 台词表与生成控制条，不在本文件展开。前置条件：阶段 2/3/4 全绿，且 `line list --json` 字段冻结。

## 12. 风险与未决

1. **裁切过度**：阈值 −45 dBFS 对气声大的播音可能误裁。缓解：`keep_min_ms=30` + `edge_trim_max_ms=400`，并保留 `tts seams --dry-run` 预先看数值。
2. **旧侧车缺失**：回退 revision 判定会让部分「参数其实变了」的行显示 ready。可接受，首次重生成即自愈。
3. **指纹字段增减**：新增字段会让全库 stale。缓解：`FP_FIELDS` 版本化（`fingerprint_version: 1`），升级时一次性提示「本次升级将重跑全部台词」。
   **前提（2026-11-10 拍板）**：本次 `FP_VERSION` 1→2 直接让存量侧车整体 stale，不加降级兼容分支。该做法只在「无真实作品存量」时成立——当前只有演示工程，重跑成本是秒级；一旦有正式工程，字段变更必须走迁移（补字段/回填指纹）而不是让用户整体重跑，因为「什么情况该重算」的判别分支会随每次字段变更继续扩散。
4. **未决**：`skipped` 是否需要独立的「静音占位」行（用于纯停顿）——当前用 `pause_after_ms` 表达，够用；若将来需要整行静音再引入。

### 12.1 已拍板（不再作为开放项）

| 决策 | 结论 | 落点 |
| --- | --- | --- |
| 撤回是否回滚文本 | **不回滚**。只回参数类字段；文本未变则指纹命中直接 `ready` 不重跑，文本已变则只回参数并提示仍需重生成 | §4.1、§4.2 |
| 并发度 | **固定为 1**。不提供 `--concurrency`，不允许并行推理；提速只能靠缓存、批内复用、接缝裁切 | §5.2、§10 |
