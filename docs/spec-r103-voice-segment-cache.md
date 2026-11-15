# R103 配音段落级缓存复用

## 目标

改了某几句台词后重新生成配音，只重新合成「内容或参数发生变化」的台词，其余沿用已有音频，
不整章 / 整批重跑；用户能在开始前看到哪些台词复用、哪些重新合成，以及预计省下多少时间。

## 现状与问题

旧实现（`tts/batch.py` + `tts/cache.py`，schema 1）已有行级缓存，但：

1. 缓存键只覆盖 text / variant_id / 参考音频 / provider / model / instruct / emotion / 切分与接缝裁切，
   漏了 **speaker_id、voice_id、emotion_intensity、speed、language、target_duration_ms、对齐配置**；
   改情绪强度或换音色条目但变体 id 相同时，会错误命中旧音频。
2. 缓存没有 `line_id` 索引，命中/失效无法解释；页面只能看到「本次计划 N 行 / 跳过 M 行」，
   看不出哪些是沿用、哪些是缓存命中、哪些真的重合成，也算不出节省。
3. 行的 `ready` 状态只看绑定 `voice_id`，音色变体或参考音频被替换时不会失效。

项目未上线，旧缓存与旧音频不做兼容，结构直接重设计。

## 设计

### 段落签名（缓存键）

一条台词 = 一个可复用的最小段落。签名取会影响音频产物的全部字段：

| 分组 | 字段 |
| --- | --- |
| 文本 | `text` |
| 说话人 | `speaker_id` |
| 音色 | `voice_id`、`variant_id`、参考音频内容 `ref_sha256`、参考文本 `ref_text` |
| 演绎 | `emotion`、`emotion_intensity`、`speed`、`instruct`（delivery_note）、`language` |
| 引擎 | `provider_id`、`model_version` |
| 切分 / 接缝 | `chunk_limit`、`chunk_silence_ms`、`edge_trim_ms`、`edge_trim_max_ms`、`keep_min_ms`、`silence_threshold_dbfs`、`hard_split_ratio` |
| 时长 | `target_duration_ms`、对齐配置（`enabled / max_error_ms / max_speed_change / on_exceed / max_regenerate`） |

签名归一化后做稳定 JSON 哈希，键形如 `seg:<sha256>`。任一分组变化 → 新键 → 必须重新合成；
参数完全一致 → 旧键 → 直接复制已有音频。供应商回落时按「实际 provider / model」归档。

### 存储（`.ndw/tts_cache.json`，schema 3）

```json
{
  "schema_version": 3,
  "entries": {
    "seg:...": {
      "line_id": "lin_...", "chapter_id": "chp_...",
      "signature": { "...": "..." },
      "asset_id": "assets/voice/lines/lin_..._r3.wav",
      "duration_ms": 4210, "bytes": 202112,
      "provider_id": "indextts", "model_version": "...",
      "voice_id": "voc_...", "variant_id": "vav_...",
      "created_at": "...", "updated_at": "..."
    }
  },
  "lines": { "lin_...": "seg:..." }
}
```

- `entries` 以签名键为主，同一段文本 / 参数只存一份；`lines` 是该行最近一次写入的键，
  用于对比并解释「这次为什么重合成」；
- 音频本体仍在 `assets/voice/lines/`，缓存只记产物位置；文件被删即视为未命中；
- 旧 schema 一律按空缓存处理，不做迁移。

### 复用计划（只读预检）

`tts.batch.plan_batch` 仍只把「缺失 / 过期 / 失败」的行纳入本次处理；`tts.reuse.plan_reuse`
对范围内每一行给出决策：

| decision | 含义 | 计入 |
| --- | --- | --- |
| `keep` | 行已就绪且签名未变，本次完全不动 | 复用（沿用已有） |
| `reuse` | 行需要处理，但缓存已有同签名音频 | 复用（缓存命中） |
| `synth` | 需要真正调用模型 | 重新合成 |
| `blocked` | 未解析到参考音频 | 阻塞 |
| `skip` | 空行 / 显式跳过 / 超出 limit | 跳过 |

`synth` 行附带 `changes`：与上一次签名逐字段对比，输出「文本 +12/−3 字」「情绪强度 0.4 → 0.9」
「音色 voc_a → voc_b」等中文说明。首次生成记为 `first`。

### 音色 / 引擎变更纳入计划

`plan_batch` 新增可选 `changed_checker(line, snapshot)`：行状态虽然是 `ready`，但当前解析出的
`voice_id / variant_id / ref_sha256` 或路由出的 `provider_id / model_version` 与侧车快照不同，
也纳入本次处理。由于签名随之变化，这类行不会被缓存误复用，只会精确重跑受影响的行。

## 接口

- `tts batch --dry-run --json` 新增 `reuse`：
  `{keep, reuse, regenerate, blocked, skipped, reused_audio_ms, saved_ms_estimate,
    avg_ms_per_line, items[], items_truncated}`；`items` 逐行带 `decision / reuse_reason / changes`。
- `GET /api/ui/tts/batch-all/plan` 透传 `reuse`；`eta_ms` 只按 `regenerate` 行数估算。
- `GET /api/ui/tts/batch-all/progress` 新增 `reused / saved_ms / reused_audio_ms`。
- 批次报告 `.ndw/tts_batches/<id>.json` 新增 `reuse` 汇总，`rows` 逐行带 `decision`。

## 交互

- 「生成配音 / 补齐本章配音」弹窗：显示「复用 X 行（沿用 K · 缓存命中 R）· 重新合成 M 行」、
  「预计节省 约 mm:ss」；影响清单每行标「复用缓存 / 重新合成 / 缺音色」，并展示失效原因；
  另有可折叠的「复用的台词」明细，逐行标「沿用已有 / 缓存命中」。
- 进度浮层：运行中显示累计复用行数；结束后显示「复用 R 行，约省 X 秒」。
- 小说页「流程衔接」卡：显示可复用行数与本次重合成行数。

## 范围边界

- 只改配音与音频合成链路：`tts/cache.py`、`tts/reuse.py`、`tts/batch.py`、`cli_media.py`、
  `web/voice_tools_api.py` 与对应前端组件；
- 不改正文生成逻辑，不改 `chapter.json` 的写入与 revision 判定，不在 `line.tts` 里新增字段
  （line schema `tts.additionalProperties=false`）。

## 验证

1. 单测：签名对文本 / 说话人 / voice_id / 变体 / 参考音频 / 情绪强度 / 语速 / 目标时长 / 对齐配置
   的敏感性；同参稳定；`describe_changes` 输出。
2. 复用计划：临时工程构造「2 行就绪 + 1 行改文本 + 1 行换音色」，预检应给出
   keep=2 / synth=1（改文本）/ synth=1（换音色），且 `saved_ms_estimate` 只按缓存命中计。
3. 静态检查：`ruff --select F821,F401`、`py_compile`、`node --check`。
