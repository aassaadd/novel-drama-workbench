# 角色设定 → 自动生成音色（Voice Design）设计（R17）

> 状态：已实现（2026-11-12）
> 日期：2026-11-12
> 关联：`docs/spec-r13-indextts25.md`、`schema/voice.schema.json`、`schema/character.schema.json`、`src/novel_drama/web/static/page-voices.js`
> 上游结论：`VoxCPM2`（Apache-2.0，2B，官方支持「文字描述造音色、无需参考音」）为**唯一引擎**；`Qwen3-TTS`、CosyVoice 云端声音设计等**云端路径不做**。
> 约束：全本地运行；本轮只出设计，不写实现、不下载权重（已于 2026-11-12 获批下载）。

---

## 0. 一句话方案

人物档案 → LLM 产出结构化「音色描述」→ Voice Design 引擎一次生成 4 条候选 → 页面试听挑选 → 选中的 wav 落为**唯一参考音**并绑定角色；此后所有台词走现有零样本克隆。不满意就「重新抽卡」开新一批，旧候选保留到过期。

---

## 1. 音色描述怎么从人物设定推

### 1.1 参与字段

| 人物字段 | 作用 | 权重 |
| --- | --- | --- |
| `gender` | 决定基础音区，硬约束 | 高 |
| `age_range` | 年龄感、声带松弛度 | 高 |
| `role` | 主角/反派/配角，影响辨识度与气质 | 中 |
| `personality[]` | 气质词（沉稳/阴郁/活泼…） | 高 |
| `speech_style.tempo` | 语速 | 高 |
| `speech_style.pitch` | 音区 | 高 |
| `speech_style.accent` | 口音/方言 | 中 |
| `background` | 身份、职业、成长环境 | 中 |
| `appearance_prompt` | 只取体型/年龄相关线索（如「瘦削」「中年」），**不取颜值词** | 低 |

> 设计原则：音色只由「能发出声音的生理与语言习惯」决定，外貌词只做弱提示，避免"漂亮=甜妹音"这类刻板映射。

### 1.2 LLM 输出结构（存 JSON，不存自由文本）

```json
{
  "voice_summary": "三十岁出头、低沉克制的男声，语速偏慢，略带北方口音",
  "gender": "male",
  "age_range": "30-39",
  "pitch": "low",
  "timbre": ["低沉", "略带沙哑", "厚实"],
  "pace": "slow",
  "temperament": ["沉稳", "压抑"],
  "accent": "北方口音",
  "design_prompt": "A low-pitched, slightly raspy male voice in his early 30s, slow and restrained, subtle northern Chinese accent, calm and serious tone.",
  "negative_prompt": ["童声", "播音腔", "过度甜美"],
  "confidence": 0.82,
  "evidence": ["gender=male", "speech_style.pitch=低沉", "personality=沉稳"]
}
```

- `design_prompt` 是**唯一喂给模型**的字段，其余字段用于展示、复现与二次微调。
- 输出走 `WritingAdapter.generate_tool`（现有结构化工具调用能力），失败则回退到规则模板（性别+年龄+音区+语速拼句），保证功能不因 LLM 不可用而中断。
- LLM 不允许写真实人名、明星、主播名字；提示词里显式禁止。

### 1.3 存放位置

- **候选阶段**：`assets/voice/candidates/<batch_id>/batch.json`（含 profile、prompt、引擎、参数、时间）。
- **入库后**：写进 `voices.json` 的 `variant.generation`（见 §5），角色侧不重复存，靠 `voice_binding` 关联。
- 支持二次微调：页面可编辑 `design_prompt` 后「重新抽卡」，新 batch 会记录 `parent_batch_id`。

---

## 2. 抽卡交互

### 2.1 一次生成几条

- 默认 **4 条**（配置项 `voice.design.candidates`，允许 1~6）。
- 每条都用**同一句固定试听台本**（约 8 秒，含陈述句+疑问句，便于比较语气与断句），文案随工程语言走。
- 候选命名：`候选 A/B/C/D`，同时显示 `seed`（有则显示）与 `f0 中位数`。

### 2.2 试听与选中

- 每个候选卡片：`<audio controls>` + 时长 + 音区/性别提示 + 「采用这个音色」。
- 采用时弹出轻量确认：变体名（默认「默认」）、性别标签（默认取 profile，可改）、是否立即绑定当前角色（默认勾选）。
- 采用后：候选 wav 复制为 `assets/voice/ref/<variant_id>.wav`，写 `voices.json`，按需写角色 `voice_binding`。

### 2.3 未选中的候选

- 保留 **7 天**（配置 `voice.design.max_keep_days`），路径 `assets/voice/candidates/<batch_id>/`。
- 清理时机：① 下次生成新批次时顺带清理过期批次；② 批次面板上的「清理候选」按钮；③ `ndw` 启动时清理（可选）。
- 选中后该批次不立即删除，方便"回心转意"改选另一条，直到过期或手动清理。

### 2.4 「重新抽卡」

- **永远开新批次**（新 `batch_id`），不覆盖旧批次；旧候选在 TTL 内仍可查看与改选。
- 页面提供「只看当前批次 / 查看历史批次」切换；历史批次默认折叠。
- 同一角色可保留多批，避免误覆盖已选中的唯一参考音。

---

## 3. 稳定性：把"抽到的音色"焊死

官方明确 Voice Design 每次结果不同（VoxCPM2 PyPI 原文：建议生成 1~3 次挑一个）。因此：

1. **选中即固化**：候选 wav 被复制为正式参考音，后续所有台词只走克隆，**不再重新 design**。
2. **记录可复现信息**：

| 字段 | 说明 |
| --- | --- |
| `prompt` | 生成时实际使用的 `design_prompt` |
| `model_id` / `model_version` | 如 `openbmb/VoxCPM2` + commit/量化版本 |
| `seed` | 引擎支持则记录；VoxCPM2 当前文档未承诺 seed 语义，记 `null` 并注明 |
| `params` | cfg / timesteps / 采样参数（引擎暴露多少记多少） |
| `sha256` | 参考音内容哈希，防止被误替换 |
| `generated_at` | 生成时间 |
| `parent_batch_id` | 由哪次抽卡产生，便于回溯 |

3. 固定 seed 只能降低方差，**不能替代保存 wav**（推断）。
4. 参考音一旦被替换（重新选候选），`variant.revision + 1` 并保留旧文件为 `.orig.wav`（与降噪保留策略一致）。

---

## 4. 性别标签：避免"内置女声是男声"重演

问题根源：过去用基频单独判性别，`voice_04` 基频 282Hz 落在女声区间但实际是男声。

设计分三层：

1. **来源优先**：`character.gender` 有值就用它，`gender_source = "character"`。
2. **声学提示**：对每条候选计算 `f0 中位数` + 粗略共振峰（复用内置音色那套 `librosa.pyin` 实测方法），若与设定性别明显冲突（如设定 male 但 f0 > 200Hz），卡片打黄标「听感可能偏中性/与设定不符」。
3. **人工确认**：采用候选时必须确认性别下拉（默认 profile 值），记录 `gender_source = "manual"`；不点确认不能入库。

> 声学检查只做**提示**，不做自动改标签；最终以人选为准。原因：高音男声、低音女声都真实存在。

---

## 5. 数据落盘

```
<工程>/
  assets/voice/
    ref/<variant_id>.wav                  # 选中的唯一参考音（24k 单声道 s16le）
    candidates/<batch_id>/
      batch.json                          # profile + prompt + 引擎参数 + 创建时间
      cand-01.wav ... cand-04.wav         # 候选，TTL 7 天
```

- 所有路径**相对工程根**（与现有 `reference.audio_path` 一致），工程可整体迁移。
- 候选目录不进 `voices.json`，只有选中项才成为正式数据；因此候选可随时整目录删除而不影响音色库。
- 候选 wav 同样归一化为 **24kHz 单声道 s16le**，与 IndexTTS / CosyVoice 参考音要求一致。

### 5.1 schema 变更（`voice.schema.json`）

- `variant.source` 枚举增加 `"designed"`；`allOf` 中「必须有 `reference` + `consent`」的条件扩展到 `designed`。
- `variant` 增加可选 `generation`：

```json
{
  "type": ["object", "null"],
  "additionalProperties": false,
  "required": ["engine", "prompt", "generated_at"],
  "properties": {
    "engine": { "type": "string", "maxLength": 64 },
    "model_id": { "type": ["string", "null"] },
    "model_version": { "type": ["string", "null"] },
    "prompt": { "type": "string", "maxLength": 2000 },
    "profile": { "type": ["object", "null"] },
    "seed": { "type": ["integer", "null"] },
    "params": { "type": ["object", "null"] },
    "batch_id": { "type": ["string", "null"] },
    "generated_at": { "type": "string", "format": "date-time" },
    "license": { "type": ["string", "null"], "maxLength": 120 }
  }
}
```

- `reference` 增加可选 `sha256`（录制/上传路径也可复用）。
- `variant` 的 `consent.confirmed` 对 `designed` 允许 `true`，`note` 写「AI 生成音色，非真人录音」。

---

## 6. 接口与前端入口

### 6.1 入口位置

- **主入口**：人物页 → 选中角色 → 「音色」区 → 按钮 **「生成音色候选」**。因为主线是"角色驱动音色"，放在角色侧最顺。
- **次入口**（P2）：音色库页「造一个音色」，不绑定角色，仅入库。

### 6.2 交互与进度

- 点击后先展示确认弹层：显示 LLM 生成的 `voice_summary` 与可编辑的 `design_prompt`、候选条数，确认才开始。
- 生成走**异步任务 + 轮询**（模型加载与 4 条生成可能 1~3 分钟，同步请求容易超时）：
  - `POST /api/ui/voices/design/batches` → `{batch_id}`
  - `GET  /api/ui/voices/design/batches/{batch_id}` → `{status, stage, done, total, candidates[]}`
  - `POST /api/ui/voices/design/batches/{batch_id}/select` → `{voice, variant_id}`
  - `DELETE /api/ui/voices/design/batches/{batch_id}` → 清理
- 进度展示：**全屏蒙版 + 分阶段文案**（解析人物 → 生成描述 → 加载模型 → 生成 2/4…），不阻塞其他页面操作（蒙版可取消）。
- 按钮文案：「生成音色候选（4 条，约 1~3 分钟）」「重新抽卡」「采用这个音色」「清理候选」。
- 已绑定音色的角色，按钮旁提示「重新生成会新增音色，不会覆盖当前绑定」。

### 6.3 与现有链路的关系

- 采用候选 = 走现有 `voices.json` 写入 + `voice_binding` 绑定，**不新增合成入口**。
- 生成完成后，配音仍由 `indextts_25` / `cosyvoice3_mlx` 负责，Voice Design 只负责"造参考音"。

---

## 7. 许可与合规

| 项 | 结论 | 来源 |
| --- | --- | --- |
| VoxCPM2 权重 | Apache-2.0，允许商用 | 官方模型卡（HF） |
| 产出音频 | 模型许可允许商用；但**不自动等于你对成品享有完整版权**，仍需遵守平台/发行方条款 | 推断（许可证不涉及输出物版权归属） |
| 标注 | 音色记录写 `generation.engine` + `consent.note="AI 生成音色，非真人录音"`，页面展示「AI 生成」标签 | 本设计 |
| 禁止 | 描述里不得出现真实人名/明星/主播；不得用生成音色冒充真人 | 本设计（合规底线） |

> 说明：本项目**只走本地模型**，不接入云端 TTS/声音设计 API；`generation.license` 固定记录 `Apache-2.0`。

---

## 8. 成本：只看本地

| 项 | 数值 | 来源 |
| --- | --- | --- |
| 权重体积 | 约 4.6GB（`model.safetensors`）+ `audiovae.pth` 等 | HF 仓库文件表 |
| 单角色耗时（4 条候选） | 首次约 2~4 分钟（含模型加载），热态约 1.5~3 分钟 | **推断，未实测** |
| 内存峰值 | 约 6~8GB | 推断 |
| 磁盘占用 | 权重 4.6GB + 每条候选 ~0.4MB（8s/24k/mono） | 计算 |

云端 TTS / 声音设计 API **本项目不做**，不在配置与代码中出现。

---

## 9. 配置项（拟新增）

```yaml
voice:
  design:
    enabled: true
    engine: voxcpm2                 # 仅本地 VoxCPM2
    model_dir: /Volumes/other/models/ndw/voxcpm2
    hf_repo: openbmb/VoxCPM2
    candidates: 4
    candidate_duration_hint_s: 8
    max_keep_days: 7
    device: auto                    # auto | mps | cpu
```

---

## 10. 实施拆解（确认后）

| 步骤 | 内容 | 预估改动 |
| --- | --- | --- |
| 1 | schema 增加 `designed` / `generation` / `sha256` | `voice.schema.json` |
| 2 | 新增 `tts/voice_design.py`（描述生成 + 调引擎 + 归一化 + 批次落盘） | 新文件 ~250 行 |
| 3 | 新增 `providers/voxcpm2_design.py`（懒加载、MPS/CPU、进度回调） | 新文件 ~200 行 |
| 4 | 模型管理接入 `models`（权重就位检查） | 小改 |
| 5 | Web：4 个接口 + 人物页面板 + 蒙版进度 | `app.py` / `page-voices.js` / `index.html` |
| 6 | CLI：`ndw voice design --character <id> --count 4` | `cli_voice.py` |
| 7 | 文档与修改记录 | 本文件 + 记录表 |

---

## 11. 已确认项（2026-11-12）

1. **全本地**：只保留 VoxCPM2，云端 Voice Design 路径不做。
2. **已批准下载** VoxCPM2 权重（~4.6GB）到 `/Volumes/other/models/ndw/voxcpm2`。
3. 候选默认 4 条、保留 7 天。
4. 主入口放「人物档案 → 音色」，音色库页也提供同等入口。
5. `variant.source` 新增 `designed`。
