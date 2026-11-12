# R20 · 剪映草稿版本兼容性核对（10.8 / 11.x）

> 日期：2026-11-12
> 对象：`src/novel_drama/media/jianying.py` 产出的剪映草稿（`draft_content.json` / `draft_info.json` / `draft_meta_info.json` / `manifest.json` + `media/`）
> 目的：提前发现「在剪映里打开后素材丢失、时间轴错乱」这类硬伤，只修确定性高的问题，其余留待实测。
> 约束：本机未安装剪映，**没有做剪映侧实测**；本报告只做「公开资料 + 参考实现源码 + 本机静态/脚本自检」的核对。

---

## 0. 一句话结论

草稿骨架与 pyJianYingDraft 的 5.9 模板逐字段一致，`version: 360000` 钉低版本、由剪映打开时升级的策略成立；
真正会导致硬伤的是两类**数据不变量**问题（均已在本次修复）：**同一条音频/字幕轨内片段重叠**、**target/source/speed 三者不自洽**；
剩余最高风险是**素材绝对路径按「安装后位置」写入**，用户没把文件夹拷进剪映草稿根目录（或草稿根目录配置不对）时，会表现为整份草稿「媒体丢失」。

---

## 1. 证据分级

| 标签 | 含义 |
| --- | --- |
| 【参考实现源码】 | pyJianYingDraft（`GuanYixuan/pyJianYingDraft`，README 标注 5.9 / 10.8 支持）当前 main 分支源码与 `draft_content_template.json`、`draft_meta_info.json` 模板，本次直接拉取核对 |
| 【社区 schema 文档】 | capcut-cli-david `docs/draft-schema/*`（CapCut 167.x / 169.x 实测，维护者明确声明**没有剪映环境、剪映侧差异未实测**） |
| 【社区二手/不可查证】 | 单一社区仓库/文章记录，未在剪映 10.8/11.x 复现 |
| 【本机核对】 | 本次在本仓库直接跑脚本得到的输出与断言结果 |
| 【推断（未验证）】 | 由以上事实推导，未实测 |

---

## 2. 当前实现实际产出的字段

### 2.1 文件与目录

```
<工程>/exports/jianying/<草稿名>/
├── draft_content.json      # 主时间轴（明文 JSON，UTF-8、indent 4）
├── draft_info.json         # 与 draft_content.json 逐字节相同（11.x 目录式草稿兜底）
├── draft_meta_info.json    # 草稿列表元数据
├── manifest.json           # 本工程自有清单（剪映忽略）
├── 导入说明.txt             # 本工程自有说明（剪映忽略）
└── media/{video,image,voice,sfx,music,other}/NNN_<文件名>
```

`<草稿名>.zip` 打包目录，zip 内保留顶层草稿文件夹。【本机核对：zip 内 9 个文件，路径 `草稿名/draft_content.json`…】

### 2.2 `draft_content.json` 顶层

`canvas_config / color_space / config / cover / create_time / duration / extra_info / fps / free_render_index_mode_on / group_container / id / keyframe_graph_list / keyframes / last_modified_platform / platform / materials / mutable_config / name / new_version / relationships / render_index_track_mode_on / retouch_cover / source / static_cover_image_path / time_marks / tracks / update_time / version`

与 pyJianYingDraft `DRAFT_CONTENT_TEMPLATE` **键集合完全一致**（多 `name` 有意填入草稿名，其余取值同模板）。【本机核对 + 参考实现源码】

### 2.3 `materials` 桶

与参考实现 `ScriptMaterial.export_json()` 的桶集合一致，共 49 个 key；本实现未写 `common_mask`（仅新版蒙版使用）与 `placeholder_infos`（CapCut 167/169 有，剪映未验证）。【参考实现源码 / 社区 schema 文档】

实际使用的素材对象：

- `videos[]`（视频 + 图片共用）：`audio_fade, category_id, category_name, check_flag(63487), crop, crop_ratio, crop_scale, duration, height, id, local_material_id, material_id, material_name, media_path(""), path, type(video|photo), width`
- `audios[]`：`app_id, category_id, category_name, check_flag(3), copyright_limit_type, duration, effect_id, formula_id, id, local_material_id, music_id, name, path, source_platform, type("extract_music"), wave_points`
- `texts[]`：`alignment, check_flag(15), content(JSON 字符串), force_apply_line_max_width, global_alpha, id, letter_spacing, line_feed, line_max_width, line_spacing, type("text"), typesetting`
- `speeds[]`：`curve_speed, id, mode, speed, type("speed")`；`audio_fades[]`：`id, fade_in_duration, fade_out_duration, fade_type, type`

以上键集合与 pyJianYingDraft `local_materials.py` / `text_segment.py` / `segment.py` **逐字段一致**（`check_flag=15` 对应「有描边」的 7|8）。【本机核对 + 参考实现源码】

### 2.4 `tracks[]` / `segments[]`

- 轨道：`{id, type(video|audio|text), flag(0 主视频轨 / 3 其余), attribute: 0, name: "", is_default_name: false, segments: []}`
- 片段公共：`enable_adjust, enable_color_correct_adjust, enable_color_curves, enable_color_match_adjust, enable_color_wheels, enable_lut, enable_smart_color_adjust, last_nonzero_volume, reverse, track_attribute, track_render_index(0), visible, id, material_id, target_timerange{start,duration}, common_keyframes, keyframe_refs`
- 视频/图片片段：+ `source_timerange, speed, volume, extra_material_refs([speed]), is_tone_modify(false), clip, uniform_scale, hdr_settings, render_index`
- 音频片段：+ `source_timerange, speed, volume, extra_material_refs([speed, audio_fade?]), is_tone_modify(false), clip(null), hdr_settings(null), render_index`
- 文本片段：+ `source_timerange(null), speed, volume, extra_material_refs([speed]), is_tone_modify(false), clip(transform.y=-0.8), uniform_scale, render_index`

与参考实现一致；`flag` 取值与 capcut-cli 文档的「0 主视频 / 3 叠加·音频·字幕」一致。【参考实现源码 + 社区 schema 文档 + 本机核对】

### 2.5 `draft_meta_info.json`

直接使用 pyJianYingDraft 模板的键集合，其中：

- `draft_fold_path` = 用户最终复制位置的绝对路径（`<drafts_root>/<草稿名>`）
- `draft_root_path` = `<drafts_root>`
- `tm_duration` = 微秒总时长
- `draft_materials` = 7 组 `{type, value: []}`（0/1/2/3/6/7/8）
- `draft_id` = 32 位大写十六进制（模板为带连字符 UUID）
- `draft_name` = 草稿名

### 2.6 媒体路径写法

- 统一写**绝对路径**，使用当前系统分隔符（macOS 为 `/`），无盘符前缀；
- 路径指向**用户把草稿文件夹复制到草稿根目录之后**的位置（`install_dir / media/...`），不是 `exports/` 里的实际生成位置；
- `draft_content.json` 素材对象使用 `path`；`media_path` 留空字符串；
- `file_Path`（大写 P）**没有使用**。

---

## 3. 字段对照表与风险等级

风险等级定义：**高**＝会直接导致「素材丢失/打不开/时间轴错乱」；**中**＝可能影响编辑体验或依赖版本行为，需实测；**低**＝与参考实现一致，通常无影响。

### 3.1 版本与画布

| 字段 | 当前值 | 10.8 依据 | 11.x 依据 | 风险 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `version` | `360000` | 【参考实现源码】5.9 模板，README 标注 10.8 可用 | 【社区 schema 文档】CapCut 167/169 仍为 360000；剪映 11.x 无实测 | 中 | 低版本草稿由高版本剪映打开时自动升级；反向（版本过高）会被拒。当前策略安全，但 11.x 是否要求更大整数不可查证 |
| `new_version` | `"110.0.0"` | 【参考实现源码】模板原值，10.8 实测可用 | 无实测 | 中 | 纯信息性字段；若 11.x 用它比较版本，110 这个值来源不可考。暂不改，等实测 |
| `platform` / `last_modified_platform` | `app_id 3704 / app_source "lv" / app_version "5.9.0" / os "mac"` | 【参考实现源码】模板（模板写 `os: "windows"`，本机改 mac 更贴近写入方） | 【社区二手/不可查证】剪映可能用 `app_source: "jy"` | 低 | `app_source` 只影响来源标识，不改 |
| `canvas_config` | 宽高按工程画幅取偶数、`ratio: "original"` | 【参考实现源码】模板同结构 | 【社区 schema 文档】同结构 | 低 | 16:9→1920×1080、9:16→1080×1920、1:1→1080×1080、4:3→1440×1080、3:4→1080×1440 |
| `fps` | `30.0` | 【参考实现源码】 | 同 | 低 | 与成片帧率不同不影响剪映解释时间轴 |

### 3.2 `materials`

| 项 | 当前 | 风险 | 说明 |
| --- | --- | --- | --- |
| 桶集合 | 与 5.9 模板一致 | 低 | 缺 `common_mask`（蒙版）、`placeholder_infos`（CapCut 167/169 有），本工程不用蒙版 |
| 视频/图片 `path` | 绝对路径 | 低 | 与参考实现一致；`path` 是 `draft_content.json` 素材的正式键 |
| `file_Path` | 未使用 | 低 | 【社区二手/不可查证】`file_Path` 只出现在 `draft_meta_info.draft_materials[].value[]` 的「素材面板」条目里（JianYingProDraft 源码 `_data['file_Path']` 用于 meta 素材条目），不是时间轴素材路径；本实现 `draft_materials` 留空，不涉及 |
| `media_path` | `""` | 低 | 【参考实现源码】同为空串 |
| 图片 `duration` | `10800000000`（3h） | 低 | 与参考实现一致，实际时长由片段决定 |
| `check_flag` | 视频 63487 / 音频 3 / 文本 15 | 低 | 文本 15 = 7（基础）+8（描边），与参考实现按效果叠加的规则一致 |
| `speeds` / `audio_fades` | 每个片段一条 speed；有淡入淡出才建 audio_fade | 低 | 与参考实现一致；本实现文本片段也会登记 speed（参考实现文本片段未登记，属其悬挂引用，不影响我们） |

### 3.3 轨道与片段（本次重点）

| 项 | 当前 | 风险 | 说明 |
| --- | --- | --- | --- |
| `tracks[].flag` | 主视频 0、叠加视频/音频/字幕 3 | 低 | 【社区 schema 文档】0=primary、3=secondary/overlay；【参考实现源码】全部写 0 也能用，说明 3 不是必填约束，写 3 更贴近剪映 UI 产出 |
| `tracks[].attribute` / `is_default_name` | `0` / `false` | 低 | 与 CapCut 实测样例一致 |
| `target_timerange` 单位 | 微秒 | 低 | 【参考实现源码 + 社区 schema 文档】均为 µs，已用 `_us()` 统一换算 |
| `source_timerange` 单位 | 微秒 | 低 | 同上 |
| 同轨片段不重叠 | **修复前配音/音效/BGM/字幕均可能重叠** | **高** | 【参考实现源码】`Track.add_segment` 对重叠直接抛 `SegmentOverlap`；同轨重叠在剪映侧会表现为片段被挤开/时间轴错位。已在本次修复 |
| `target.duration × speed == source.duration` | **修复前固定 speed=1.0，短素材填长槽时不自洽** | **高** | 【参考实现源码】`AudioSegment`/`VideoSegment` 由 source/target 反推 speed 并写入 `speeds[]`。不自洽时剪映会自行纠正（拉伸或截断），导致画面/声音与字幕错位。已在本次修复 |
| `render_index` / `track_render_index` | 轨道序号 / 恒 0 | 低 | 与参考实现一致（跨轨 z-order 由 tracks 数组顺序决定） |
| 缺失字段 | 无 `render_timerange`、`is_loop`、`enable_hsl` 等 CapCut 167/169 字段 | 低-中 | 5.9 基线不需要；11.x 打开旧草稿时按「未知字段=默认值」处理（社区文档的宽松读取策略），未实测 |

### 3.4 `draft_meta_info.json`

| 字段 | 当前 | 风险 | 说明 |
| --- | --- | --- | --- |
| `draft_fold_path` | 安装后绝对路径 | 中 | 【参考实现源码】模板留空也能被 10.8 识别（pyJianYingDraft 直接拷贝空模板）；我们填安装路径是为了列表/迁移提示更准确。若用户实际没拷到该路径，剪映会以自己的扫描结果为准，不致命 |
| `draft_root_path` | `<drafts_root>` | 中 | 同上；`drafts_root` 若配置错误，素材绝对路径随之全错（见 §4.1） |
| `tm_duration` | 微秒 | 低 | 与模板一致 |
| `draft_materials` | 7 组空数组 | 低 | 只影响「素材」面板列表，不影响时间轴；模板同为空 |
| `draft_id` | 32 位大写 hex | 低 | 模板是带连字符 UUID，但参考实现直接沿用模板固定值也能用，格式非强约束 |

### 3.5 文件形态

| 项 | 当前 | 风险 | 说明 |
| --- | --- | --- | --- |
| `draft_content.json` 明文 | 是 | 低 | 【社区二手/不可查证】剪映 6+ 加密只影响「读取 App 写出的草稿」，明文新草稿可被打开并原地升级 |
| `draft_info.json` 双写 | 是 | 低-中 | 【社区二手/不可查证，单一来源】capcut-cli 记录 11.4 macOS 目录式草稿优先识别 `draft_info.json`，写旧名会报「草稿内容已损坏」；双写把两个名字都占上 |
| `manifest.json` / `导入说明.txt` | 在草稿目录内 | 低 | 剪映按约定忽略未知文件；若担心 11.x 更严格，可把它们移出草稿目录（本次不动，避免改变交付结构） |

---

## 4. 在 10.8 / 11.x 打开时最可能出的问题

### 4.1 素材路径（风险最高）

`materials.videos[].path` / `materials.audios[].path` 写的是「用户把草稿文件夹复制到草稿根目录之后」的绝对路径。以下任一情况都会导致**所有素材显示「媒体丢失」**、预览黑屏/静音：

1. 用户没有把文件夹拷进剪映草稿根目录，直接在 `exports/` 里打开；
2. 设置里的「剪映草稿根目录」与实际草稿位置不一致；
3. 复制后又移动/重命名了草稿文件夹；
4. 换机器/换用户目录。

缓解：`manifest.json` 的 `path_map` 记录了「包内相对路径 ↔ 安装后绝对路径」，导入说明写了正确步骤；剪映侧可右键「链接媒体」逐个重链。

**路径策略已拍板收口（2026-11-12）**：只交付 zip，解压后的位置由用户负责；草稿内固定写绝对路径，按剪映默认草稿目录推导，不引入占位符或相对路径方案，后续不再讨论该选型。
因此 zip 内的 `导入说明.txt` 明确写出目标解压路径（`<草稿根目录>/<草稿名>`）与「解压后不要移动/改名文件夹」的原因（素材是绝对路径，移动即「媒体丢失」）；CLI 与导出页在生成成功后各输出一行「导入位置」提示。

### 4.2 版本与升级链路

- 10.8：以 `version: 360000` 打开会提示升级并原地加密改写，之后本工具不再能把它当模板读——我们本来就不读用户草稿，无影响。【社区二手/不可查证 + 推断（未验证）】
- 11.x：目录式草稿可能优先读 `draft_info.json`（已双写兜底）；若 11.x 抬高了 `version` 闸门或强制要求新桶（如 `placeholder_infos`），可能报「草稿版本过低/内容损坏」。**这一条只有实测能定性**。

### 4.3 编辑体验类（不致命）

- 字幕使用剪映默认字体，未缓存字体时首次打开字形不同，二次打开恢复；【参考实现源码 README：10.8 未缓存字体需二次打开】
- 未铺转场/特效/滤镜/关键帧/蒙版（工程无数据来源），剪映不会因此报错；
- 图片素材宽高来自 ffprobe，探测失败时回落 1920×1080，横竖混排时可能初始缩放不一致（`uniform_scale` 已开）。

---

## 5. 结论：哪些是确定的、哪些只能实测

| 判定 | 项 |
| --- | --- |
| **确定错误（已修）** | ① 配音/音效/BGM/字幕同轨重叠；② target/source/speed 不自洽 |
| 确定正确（有源码依据） | 时间单位 µs、`canvas_config`、`materials` 各类型必填键、`path`（而非 `file_Path`）、`flag` 0/3、`draft_meta_info` 键集合 |
| 不可查证（保持现状） | 剪映 11.x 是否接受 `version 360000` / `new_version "110.0.0"` / `app_source "lv"`；`draft_fold_path` 填实值是否有副作用；`draft_info.json` 双写是否必要 |
| 需实测才能决定 | 路径占位符方案、`placeholder_infos` 等新桶是否必须、`manifest.json` 是否应移出草稿目录 |

---

## 6. 本次修改（只改 `jianying.py`）

| # | 修改 | 依据 | 风险 |
| --- | --- | --- | --- |
| 1 | 配音、音效、BGM 三类音频轨与字幕轨引入「分层」：同一类轨道内按开始时间贪心分配到多条不重叠轨道，保证任意轨道内片段不重叠 | 参考实现 `Track.add_segment` 重叠即抛错；spec §8 风险 6 | 低（只增加轨道数，不改变片段起止与内容） |
| 2 | 片段速度按 `source.duration / target.duration` 计算并同时写入片段 `speed` 与 `speeds[]` 素材，消除三者不自洽 | 参考实现 `AudioSegment`/`VideoSegment` 的构造逻辑 | 低（1:1 素材仍为 1.0，只有「素材短于时间槽」时才 <1.0） |
| 3 | 未改动：版本号、`app_source`、路径策略、字段增删、`manifest.json` 位置 | 无实测依据，不猜着改 | — |

自检方式：`.verify_tmp/verify_jianying.py`（临时脚本，验证后删除）用 ffmpeg 生成真实 mp4/wav/png，构造含重叠配音/音效/字幕、且视频短于时间槽的时间轴，导出后断言：
时间轴无同轨重叠、`source ≤ 素材时长`、`|speed - source/target| ≤ 1e-4`、所有 `material_id` / `extra_material_refs` 均可解析。
修复前报 6 条问题，修复后 0 条；`python -m compileall` 通过。

---

## 7. 参考来源

- pyJianYingDraft（5.9/10.8 功能矩阵、模板、素材与片段实现）：
  - <https://github.com/GuanYixuan/pyJianYingDraft>
  - <https://raw.githubusercontent.com/GuanYixuan/pyJianYingDraft/main/pyJianYingDraft/assets/draft_content_template.json>
  - <https://raw.githubusercontent.com/GuanYixuan/pyJianYingDraft/main/pyJianYingDraft/assets/draft_meta_info.json>
  - `local_materials.py` / `segment.py` / `track.py` / `text_segment.py` / `_script_file_segments.py` / `draft_folder.py`
- capcut-cli-david schema 文档（CapCut 167.x/169.x 实测，剪映差异未实测）：
  - <https://cdn.jsdelivr.net/npm/capcut-cli-david@1.13.0/docs/draft-schema/01-tracks-and-segments.md>
  - <https://cdn.jsdelivr.net/npm/capcut-cli-david@1.13.0/docs/draft-schema/05-version-differences.md>
- JianYingProDraft（`file_Path` 仅用于 meta 素材条目）：<https://github.com/xiaoyiv/JianYingProDraft>
- 草稿目录/加密/双文件名（社区二手，单一来源）：
  - <https://raw.githubusercontent.com/renezander030/capcut-cli/HEAD/docs/jianying-encryption.md>
  - <https://cdn.jsdelivr.net/npm/capcut-cli-david@1.13.0/docs/draft-schema/00-overview.md>
- 本仓库设计基线：[spec-r16-jianying-draft-export.md](spec-r16-jianying-draft-export.md)
