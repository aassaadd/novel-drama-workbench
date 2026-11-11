# R16 · 导出剪映草稿（调研 + 设计）

> 状态：已实现（按 2026-11-11 拍板范围：只出 zip、分镜级、素材自包含、30fps、无说话人前缀、不铺混音轨；未做剪映侧实测）
> 日期：2026-11-11
> 关联：`src/novel_drama/media/jianying.py`、`src/novel_drama/cli_media.py`、`src/novel_drama/web/app.py`、`src/novel_drama/web/static/page-export.js`
> 目标：让本工程产出的成片可以直接在剪映专业版（Mac）里打开并继续编辑，而不是从零拼轨道。
> 约束：所有结论标注依据，禁止用推断冒充结论；实现阶段跳过 §10 人工验收。

---

## 0. 一句话结论

剪映草稿就是「草稿根目录下的一个文件夹」，**最少只需要 `draft_content.json` + `draft_meta_info.json` 两个文件**即可被识别（社区实测）；
时间轴全部在 `draft_content.json`，时间单位**微秒**，素材默认用**绝对路径**引用；
剪映 6.0+ 对 `draft_content.json` **加密只影响读取、不影响写入**，所以「从零生成草稿」可行，但**不能用用户已有草稿当模板**。
据此，本工程最稳的方案是：**按时间轴生成分镜级草稿目录（素材复制进草稿内 `media/`）→ 打包 zip → 用户拷进剪映草稿目录并重启剪映**，同时提供一个可选的「一键写入草稿目录」按钮。

---

## 1. 证据分级与调研条件

每条结论标注下列标签之一：

| 标签 | 含义 |
| --- | --- |
| 【官方原文】 | 剪映/CapCut 官方帮助、厂商官方文档原文 |
| 【社区实测】 | 开源项目源码或作者在真实版本上验证过的记录（附仓库/文档链接） |
| 【推断（未验证）】 | 由已知事实推导，未在剪映里实测 |
| 【本机核实】 | 本次在本仓库/本机直接查到的结果 |

**重要前提（必须说清）**：
- 【本机核实】本机 `/Applications` 下**没有安装剪映/CapCut**，`~/Movies` 下也没有 `JianyingPro` 目录，因此**本次没有任何剪映侧实测**。
- 所有目录结构、字段、版本兼容结论均来自公开资料，需在实现后按 §10 的冒烟草稿逐项验证。

---

## 2. 剪映草稿的真实目录结构与必需文件

### 2.1 草稿根目录（drafts root）

| 系统 / 版本 | 默认草稿根目录 | 依据 |
| --- | --- | --- |
| macOS · 剪映专业版 | `~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft/` | 【社区实测】掘金《使用 FFmpeg 无损导出剪映轨道视频片段》给出该路径；JianYingAPI 文档示例路径为 `/Users/<user>/Movies/JianyingPro/User Data/Projects/com.lveditor.draft/template_text/draft_info.json` |
| Windows · 剪映专业版 | `C:\Users\<user>\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\` | 【社区实测】同上掘金文章 |
| macOS · CapCut 国际版 | `~/Movies/CapCut/User Data/Projects/com.lveditor.draft/` | 【社区实测】cutconvert、filepathgeek、capcut-cli 文档三处一致 |
| 自定义 | 剪映「全局设置 → 草稿位置」可改；CapCut 同理 | 【官方原文（UI）】+【社区教程】 |

> 结论：**根目录不是硬编码，必须允许用户配置**。叶子目录名固定为 `com.lveditor.draft` 只是默认值。

### 2.2 单个草稿的文件夹内容

【社区实测】capcut-cli 的 draft-schema 文档给出 CapCut 草稿目录实测清单；pyJianYingDraft 只创建其中两个文件即可被剪映识别。整理如下：

```
<drafts-root>/<draft 文件夹名>/
├── draft_content.json        ← 时间轴主文件（必需）
├── draft_content.json.bak    ← 剪映保存时轮转的备份
├── draft_info.json           ← 旧版剪映/CapCut 先读它；与 content 同构
├── draft_info.json.bak
├── draft_meta_info.json      ← 草稿列表行元数据（名称/封面/时长/路径）（必需）
├── draft_settings            ← 草稿级设置（辅助）
├── draft.extra               ← 路径占位符映射等（辅助，二进制）
├── draft_agency_config.json  ← 辅助
├── draft_biz_config.json     ← 辅助
├── draft_cover.jpg           ← 草稿列表缩略图
├── timeline_layout.json      ← 轨道 UI 布局
├── attachment_*.json / key_value.json / perf / template-2.tmp ← 辅助或缓存
├── Timelines/                ← 新版剪映首次打开后自建
└── Resources/ 或 materials/  ← 仅当素材被「复制至草稿」时才存在
```

**哪些是必需的？**

| 文件 | 必需性 | 依据 |
| --- | --- | --- |
| `draft_content.json` | **必需**（时间轴本体） | 【社区实测】pyJianYingDraft `create_draft()` 只写它 + `draft_meta_info.json` |
| `draft_meta_info.json` | **必需**（列表条目元数据） | 【社区实测】同上，模板文件为 `draft_meta_info.json` |
| `draft_info.json` | 视版本而定 | 【社区实测】capcut-cli 记录：剪映 11.4 macOS 目录式草稿能打开明文 `draft_info.json`，写旧名 `draft_content.json` 会报「草稿内容已损坏」；社区工具（开源剪映小助手）因此采用「双文件同步写」 |
| `draft_settings` / `draft_cover.jpg` / `Timelines/` / `*.bak` | 非必需 | 【社区实测】pyJianYingDraft 不写这些文件，草稿仍能被剪映打开并自动补齐；capcut-cli 文档称其为 auxiliary |
| 素材目录 | 非必需 | 【社区实测】剪映默认「素材保留在原有位置」，只有勾选「复制至草稿」或使用模板素材时才进草稿目录 |

> ⚠️ 双文件这条证据来自单一社区仓库（capcut-cli，2026-09 在 11.4.0 macOS 校准）。实现时必须按 §10 在真实剪映上验证，**不能直接当定论**。

### 2.3 草稿的「注册」方式

- **没有注册表文件**。剪映按草稿根目录扫子文件夹，文件夹名即草稿标识。【社区实测】pyJianYingDraft 的 `list_drafts()` 就是 `os.listdir` 子目录；capcut-cli 也以「folder name = draftId」识别草稿。
- **新草稿出现在列表**：火山引擎「剧创」的官方 FAQ 明确写「复制文件夹到剪映草稿文件夹下 → **重启剪映**即可看到草稿」。【官方原文（厂商文档）】
- 社区也有「写入后触发文件系统变更即可，无需重启」的说法，但属于单一来源，不作为设计依据。【推断（未验证）】

### 2.4 `draft_content.json` 骨架

【社区实测】pyJianYingDraft 的 `DRAFT_CONTENT_TEMPLATE`（5.9 基线）关键字段：

```jsonc
{
  "canvas_config": { "width": 1920, "height": 1080, "ratio": "original" },
  "fps": 30.0,
  "duration": 0,                 // 微秒，草稿总时长
  "version": 360000,             // schema 整数，加载闸门
  "new_version": "110.0.0",      // 人类可读版本
  "platform":         { "app_id": 3704, "app_source": "lv", "app_version": "5.9.0", "os": "windows" },
  "last_modified_platform": { ...同上... },
  "config": { "maintrack_adsorb": true, "material_save_mode": 0, ... },
  "materials": {
    "videos": [], "audios": [], "texts": [], "effects": [], "stickers": [],
    "audio_fades": [], "beats": [], "speeds": [], "transitions": [], ...
  },
  "tracks": [],                  // 轨道数组，顺序即 z-order
  "keyframes": { "videos": [], "audios": [], "texts": [], ... },
  "relationships": [], "time_marks": null
}
```

- 所有时间值单位是**微秒**（1s = 1,000,000）。【社区实测】
- 轨道：`{"id","type":"video|audio|text|effect|sticker|filter","flag":0|3,"segments":[...]}`；`video` 用 `flag=0`（主轨），`audio`/`text` 用 `flag=3`。【社区实测】capcut-cli 01-tracks-and-segments。
- 素材：视频/图片进 `materials.videos[]`，音频进 `materials.audios[]`，字幕进 `materials.texts[]`；片段通过 `material_id` 反向引用。【社区实测】pyJianYingDraft `local_materials.py` / `text_segment.py`。
- 图片素材 `duration` 固定写 `10800000000`（约 3 小时），实际显示时长由片段 `target_timerange.duration` 决定。【社区实测】

### 2.5 `draft_meta_info.json` 关键字段

【社区实测】pyJianYingDraft 模板（节选，全部字段均为顶层字符串/数字/数组）：

```jsonc
{
  "draft_id": "BC69C7CD-7C5E-4185-B284-AF3E1047A664",
  "draft_name": "",              // 草稿列表显示名
  "draft_fold_path": "",         // 草稿文件夹绝对路径
  "draft_root_path": "",         // 草稿根目录绝对路径
  "draft_cover": "",             // 封面相对/绝对路径
  "draft_new_version": "",
  "tm_duration": 0,              // 微秒
  "draft_materials": [ { "type": 0, "value": [] }, ... 7 组 ],
  "draft_is_invisible": false,
  "draft_removable_storage_device": "",
  "draft_cloud_last_action_download": false,
  "draft_enterprise_info": { ... },
  "tm_draft_removed": 0
}
```

`draft_materials` 的 `type` 取值 0/1/2/3/6/7/8（视频/音频/图片等分组），只影响剪映「素材」面板的展示，不影响时间轴。【推断（未验证）】

---

## 3. 版本兼容性

### 3.1 两个版本字段

| 字段 | 作用 | 观测值 | 依据 |
| --- | --- | --- | --- |
| `version` | 整数 schema 版本，**加载闸门** | `360000`（5.9 模板；CapCut 167.x / 169.x 同为 360000） | 【社区实测】capcut-cli 05-version-differences |
| `new_version` | 人类可读版本，信息性 | `"110.0.0"`（pyJianYingDraft 模板）/ `"167.0.0"`、`"169.0.0"`（CapCut） | 【社区实测】同上 |
| `platform.app_version` | 写入方版本 | `"5.9.0"`（模板） | 【社区实测】 |

**草稿版本高于 App → 打不开；低于 App → 打开时提示升级并可打开。**【社区教程】剪映「草稿版本较新无法打开」的返还旧版教程，反向证明低版本草稿可被高版本打开。
→ 设计策略：**字段钉在低版本基线（`version: 360000`）**，让剪映自行升级，避免「版本过新」被拒。

### 3.2 剪映 6.0+ 的加密

| 结论 | 依据 |
| --- | --- |
| 剪映 6.0+ 把 `draft_content.json` 存成 AES 加密二进制，不再是明文 JSON | 【社区实测】capcut-cli `docs/jianying-encryption.md`、pyJianYingPro PyPI 说明「剪映6+版本对 draft_content.json 进行了加密，故模板模式仅支持 5.9 及以下」 |
| **加密只挡住「读取 App 写出的草稿」，不挡「写入新草稿」** | 【社区实测】capcut-cli：在剪映 11.4（macOS）上，明文草稿会被打开并**原地升级**成加密格式；pyJianYingDraft README 功能表显示 10.8 上「本地视频/图片/音频/文本/字幕/SRT 导入」全部 ✅ |
| 10.8+ 用 `draft_content.json` 当模板需要 `fallback_loader`（因为读不出来） | 【社区实测】pyJianYingDraft README 功能表「加载 draft_content.json 作为模板：10.8 🟡需 fallback_loader」 |
| 国际版 CapCut **没有**这套加密 | 【社区实测】capcut-cli 同一文档 |

**对设计的直接影响**：
1. 不能「拿用户已有草稿当模板」来继承封面/字体/转场（6+ 读不出来）。
2. 必须**从零构造完整 JSON**，字段取社区验证过的模板值。
3. 交付前必须准备「双文件名」兜底（§2.2）。

### 3.3 缺失字段的后果

| 缺失项 | 后果 | 依据 |
| --- | --- | --- |
| `draft_meta_info.json` | 草稿列表里没有条目或信息异常 | 【推断（未验证）】两个社区实现都强制写它 |
| `draft_content.json` / `draft_info.json` 都缺 | 打开报错/空白 | 【推断（未验证）】 |
| `canvas_config` / `fps` | 画布尺寸、帧率异常 | 【推断（未验证）】 |
| 素材 `path` 指向不存在的文件 | 剪辑显示「媒体丢失」，可右键「链接媒体」重链；导出时该段黑屏/静音 | 【社区实测】剪映「草稿媒体丢失」教程：素材在原有位置被移走即触发，需手动链接 |
| `draft_settings` / `draft_cover` / `Timelines/` | 无影响，剪映打开后自建 | 【社区实测】pyJianYingDraft 只写两份 JSON 即可用 |
| 字体未缓存 | 首次打开显示默认字体，**二次打开**才生效 | 【社区实测】pyJianYingDraft 功能表「字体：10.8 🟡未缓存的字体需要二次打开草稿」 |

### 3.4 建议的最低版本假设

| 版本 | 结论 | 依据 |
| --- | --- | --- |
| 剪映 5.9.x | 可用（明文基线，最保守） | 【社区实测】pyJianYingDraft 5.9 全绿 |
| 剪映 10.8 | **推荐验证基线** | 【社区实测】pyJianYingDraft 10.8 功能表全绿 |
| 剪映 11.x | 待实测；预计可用，但目录式草稿可能优先认 `draft_info.json` | 【社区实测（单一来源）】capcut-cli 11.4 macOS 记录 |
| CapCut 国际版 | 同 schema，未加密；路径与字体/语言不同 | 【社区实测】 |

> 设计口径：**最低支持剪映 5.9，主验证 10.8，11.x 作为兼容目标**；启动时把实际剪映版本写进产物 README，出问题可定位。

---

## 4. 素材引用方式

### 4.1 三种可用写法

| 写法 | 形式 | 可用性 | 依据 |
| --- | --- | --- | --- |
| 绝对路径 | `/Users/x/proj/assets/voice/lines/xxx.wav` | ✅ 社区实现默认用法 | 【社区实测】pyJianYingDraft `VideoMaterial`/`AudioMaterial` 都对路径做 `os.path.abspath()` |
| 草稿内占位符 | `##_draftpath_placeholder_<id>_##/Resources/xxx.png` | 🟡 CapCut 实测可用（加载时替换为当前草稿目录）；**剪映是否同样生效未验证** | 【社区实测】capcut-cli-david `COMPATIBILITY.md`、capcut-export 源码；剪映侧无实测 |
| 相对路径 | `media/xxx.wav` | ❌ 无证据支持，不采用 | 【推断（未验证）】 |

> 结论：**默认走绝对路径**；素材复制进草稿目录后，把绝对路径写成「草稿目录内文件的绝对路径」，这样拷贝整份草稿到别处会失效——所以最终安装时要**重写路径**（见 §6.3）。

### 4.2 混排与缺失表现

- 音频、视频、图片可以混在同一草稿的不同轨道，互不影响。【社区实测】pyJianYingDraft 支持多轨混排。
- 素材缺失时**不会自动变成占位图**，而是标记「媒体丢失」：轨道片段仍在、预览黑屏/静音，可「链接媒体」找回。【社区实测】剪映教程；【推断（未验证）】占位图行为。
- 素材宽高比不一致时会按 `clip` 变换裁剪/缩放，不会自动适配画布。【社区实测】pyJianYingDraft `ClipSettings`。

---

## 5. 我们现有数据能映射到什么

### 5.1 映射表

| 剪映对象 | 我们的来源 | 现状 | 说明 |
| --- | --- | --- | --- |
| 画布尺寸 | `settings.output.aspect_ratio`（`1:1/3:4/4:3/9:16/16:9`） | ✅ 有 | 映射见 §6.4 |
| 帧率 | **无工程配置**；`mux` 固定 24fps、1280×720 | ⚠️ 缺口 | 草稿默认写 30.0，可配置；与成片帧率不一致不影响剪映（剪映按草稿 fps 解释时间） |
| 视频轨（成片级） | `renders/<chapter>.mp4` | ✅ 有 | 一条整段，最省事 |
| 视频轨（分镜级） | 时间轴 `trk_video`（`assets/video/*.mp4`）+ `trk_image`（关键图） | ✅ 有 | 每段一个 segment，按 `start_ms` 排布 |
| 配音音频轨 | 时间轴 `trk_voice`（`assets/voice/lines/*.wav`） | ✅ 有 | 每行一个 segment |
| 混音音频轨 | `exports/<chapter>.mp3`（bundle 转码）或 `voice/<chapter>.mix.wav` | ✅ 有 | 二选一，避免与逐行配音重复出声 |
| 音效轨 | 时间轴 `trk_sfx` | ✅ 有 | 每段一个 segment |
| 音乐轨 | 章节 `bgm_cues`（`start_ms`/`gain_db`/淡入淡出/loop） | ✅ 有 | 当前时间轴 `trk_music` 恒为空，cue 在混音阶段才并入 |
| 字幕轨 | 时间轴 `trk_voice` 的 `start_ms/end_ms` + 章节台词 `text` | ✅ 有 | 与现有 SRT 同源；可选「说话人：」前缀 |
| 文字样式 | 无用户配置 | ⚠️ 缺口 | 用固定默认：白字、8.0 号、底部居中 |
| 转场 / 动画 / 特效 / 滤镜 / 贴纸 / 花字 | 无数据 | ❌ 无来源 | 一律不写 |
| 关键帧 / 变速 / 蒙版 | 无数据 | ❌ 无来源 | 一律不写 |
| 字体文件 | 无 | ❌ 无来源 | `font_path` 留空，用剪映默认字体 |
| 文字坐标 | 无 | ❌ 无来源 | 用默认底部位置 |
| 素材时长/宽高 | 需探测 | ⚠️ 需新增 | 用现有 `ffprobe` 能力读 mp4/wav/png |

### 5.2 结论

**「视频 + 配音 + 字幕 + 音效 + BGM」五类轨道完全有数据来源，可以做到打开即可编辑**；缺失的是「转场/特效/字体/文字排版」这类美化字段，不影响可用性，属于第二阶段。

---

## 6. 导出方案设计

### 6.1 新增导出类型

CLI：

```bash
ndw export --chapter <章节> --format jianying [--layout shot|render] [--install] [--drafts-root <路径>]
```

- `--format`：现有 `srt` / `bundle` 之外新增 `jianying`。
- `--layout shot`（默认）：分镜级，视频轨由分镜视频段 + 关键图拼成。
- `--layout render`：成片级，视频轨只放 `renders/<chapter>.mp4` 整段。
- `--install`：生成后直接复制进剪映草稿根目录并重写素材路径。
- `--drafts-root`：覆盖配置里的草稿根目录。

Web：导出页新增卡片「剪映草稿」，字段为「章节（沿用当前）/ 轨道粒度（分镜级·成片级）/ 是否直接写入剪映 / 草稿目录」，按钮「生成剪映草稿」，复用现有全屏蒙版与后台 CLI 进程模式（`/api/ui/generate` 同款）。

### 6.2 产物位置

```
<工程>/
└── exports/
    └── jianying/
        ├── <书名>_<章节标题>_<短ID>/        ← 可直接拷进剪映草稿目录的草稿文件夹
        │   ├── draft_content.json
        │   ├── draft_info.json            ← 与 content 同内容（双文件名兜底）
        │   ├── draft_meta_info.json
        │   ├── 导入说明.txt
        │   └── media/
        │       ├── voice/<line_id>.wav
        │       ├── video/<segment_id>.mp4
        │       ├── image/<key_image_id>.png
        │       ├── sfx/<sfx_id>.wav
        │       ├── music/<cue_id>.wav
        │       └── mix/<chapter>.mp3
        └── <书名>_<章节标题>_<短ID>.zip   ← 交付用压缩包（对齐火山引擎剧创的做法）
```

- 文件夹名取「书名_章节名_短ID」，过滤 `/ \ : * ? " < > |` 与首尾空格；短 ID 取时间轴 id 后 6 位，避免重名覆盖。
- 按用户既有约束：**产物必须落在工程目录内、工程内引用只用相对路径**；草稿 JSON 里的绝对路径只是剪映的运行期需要，另存一份 `path_map` 到 `导入说明.txt` 便于排查。

### 6.3 素材策略（关键决策）

| 策略 | 做法 | 优点 | 缺点 |
| --- | --- | --- | --- |
| A. 引原位置 | JSON 写 `<工程>/assets/...` 绝对路径 | 不占额外磁盘、素材永远最新 | 工程一移动就全部「媒体丢失」；不符合「可直接分享」 |
| B. 复制进草稿（推荐） | 复制到 `<draft>/media/**`，JSON 写副本绝对路径 | 草稿自包含、可整体拷贝/压缩发送 | 磁盘翻倍；需处理大文件 |
| C. 占位符相对引用 | `##_draftpath_placeholder_##/media/**` | 拷贝后无需改路径 | 剪映侧未验证，风险高 |

**设计结论：默认 B，提供 `--link-media` 开关退化为 A；C 作为验证通过后的优化项，不默认启用。**

`--install` 的流程：
1. 生成到 `exports/jianying/<draft>/`（绝对路径指向这里）。
2. 复制整份目录到 `<drafts-root>/<draft>/`。
3. **重写**两份 JSON 里所有 `path`/`draft_fold_path`/`draft_root_path` 为新位置的绝对路径。
4. 打印「请重启剪映后在草稿列表查看」。

> 这样 bundle 与 install 用同一套生成逻辑，只多一步「搬家 + 改路径」，避免两套代码。

### 6.4 画布与帧率

| 工程画幅 | 剪映 `canvas_config` |
| --- | --- |
| `16:9` | `1920 × 1080` |
| `9:16` | `1080 × 1920` |
| `1:1` | `1080 × 1080` |
| `4:3` | `1440 × 1080` |
| `3:4` | `1080 × 1440` |

- 帧率默认 `30.0`（剪映默认），新增配置项可改；`--layout render` 时也可探测成片实际帧率。
- 宽高必须为偶数；`ratio` 写 `"original"`。
- 时间统一 `ms × 1000` 转微秒；`end_ms <= start_ms` 的片段直接跳过并记 warning。

### 6.5 轨道映射规则

| 轨道顺序（z-order 从下到上） | type | 内容 |
| --- | --- | --- |
| 1 | `video` | `--layout shot`：按 `trk_video`+`trk_image` 顺序铺满；空隙用前一段定格或补黑 |
| 1 | `video` | `--layout render`：`renders/<chapter>.mp4` 一条整段 |
| 2 | `audio` | 逐行配音（`trk_voice`），`volume` 按现有增益 |
| 3 | `audio` | 音效（`trk_sfx`） |
| 4 | `audio` | BGM cues（`trk_music` + `bgm_cues`），应用 `gain_db` 与淡入淡出 |
| 5 | `text` | 每行一条字幕，文本 = `说话人：台词`（可开关），时间与配音段一致 |

规则细节：
- `--layout shot` 时**不要**同时铺混音轨，否则配音重复；`--layout render` 时成片已含声音，音频轨只保留 BGM/音效或全空，由参数控制。
- 分镜视频段与关键图在时间轴上可能重叠，剪映同轨不允许重叠：按 `start_ms` 排序后，后一段裁剪到前一段结束；仍有重叠则拆到第二条视频轨（`flag=3`）。
- 字幕段最短 100ms，避免 0 时长片段导致剪映异常。【推断（未验证）】

### 6.6 生成后用户的导入路径

两条，页面与 CLI 输出都要写清楚：

1. **手动（默认，最稳）**：解压/打开 `exports/jianying/<draft>/` → 整个文件夹复制到剪映草稿根目录 → **重启剪映** → 草稿列表出现同名草稿。【官方原文（厂商文档）】火山引擎剧创 FAQ 步骤一致。
2. **一键（可选）**：页面勾选「直接写入剪映」，或 CLI `--install`；需要用户先在设置里确认草稿根目录存在可写。

---

## 7. 改动范围

| 文件 | 改动 | 规模 |
| --- | --- | --- |
| `src/novel_drama/media/jianying.py`（新增） | 草稿构建（canvas/轨道/素材）、媒体复制与探测、双文件名写入、zip 打包、install 重写路径 | 约 500–700 行，超 800 行拆 `jianying_draft.py` / `jianying_media.py` |
| `src/novel_drama/cli_media.py` | `cmd_export` 增加 `jianying` 分支与参数校验 | ~40 行 |
| `src/novel_drama/cli.py` | `export` 子命令新增 `--format jianying`、`--layout`、`--install`、`--drafts-root`、`--link-media` | ~15 行 |
| `src/novel_drama/media/__init__.py` | 导出新函数 | ~3 行 |
| `config/config.yaml` + `config.example.yaml` | 新增 `export.jianying.{drafts_root,fps,layout,copy_media,subtitle_prefix}` | ~10 行 |
| `src/novel_drama/web/app.py` | `POST /api/ui/export/jianying`（后台 CLI）、`GET /api/ui/jianying/status`（草稿目录是否存在/可写） | ~60 行 |
| `src/novel_drama/web/static/page-export.js` | 新增「剪映草稿」卡片逻辑、生成按钮、结果展示 | ~80 行 |
| `src/novel_drama/web/static/index.html` + `styles.css` | 卡片结构与小样式 | ~40 行 |
| `docs/` + `项目修改记录文档.md` | 本文档 + 实现条目 | — |

不改：`timeline_builder.py` 的时间轴结构、`export.py` 现有 SRT/bundle 行为、`mux.py` 渲染参数、schema 文件。

---

## 8. 风险清单

| # | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| 1 | 剪映 6+ 加密导致无法读取模板 | 不能继承用户草稿样式 | 从零生成；不做模板模式 |
| 2 | 版本字段漂移导致「草稿内容已损坏」/打不开 | 功能不可用 | 钉 `version:360000`；双文件名；启动冒烟草稿验证；配置里留版本档位 |
| 3 | 绝对路径导致工程搬家后素材全丢 | 交付不可用 | 素材复制进草稿；install 时重写路径 |
| 4 | 直接写草稿目录：权限（macOS TCC）、覆盖用户草稿、剪映运行中缓存回写 | 数据被覆盖 | 默认不写；写入前检测目标不存在或二次确认；提示先退出剪映 |
| 5 | 磁盘占用翻倍 | 大章节体积大 | 提供 `--link-media`；README 说明 |
| 6 | 剪映同轨不允许重叠 | 分镜视频段重叠时时间轴异常 | 后段裁剪 + 溢出到第二视频轨 |
| 7 | 剪映自动下载特效/字体可能超时，表现为「xx 加载失败」 | 首开报错 | 不写特效/字体，只用默认样式 |
| 8 | 无本机剪映，无法自动验收 | 兼容性未知 | §10 冒烟草稿人工验收清单 |
| 9 | 字体/文字位置不可控 | 字幕样式与成片不完全一致 | 明确写进「已知差异」说明 |

---

## 9. 待用户拍板的点

1. **默认轨道粒度**：分镜级（`shot`，可逐镜调整，但素材多、体积大）还是成片级（`render`，一条整段，最稳最省）？
2. **默认是否复制素材**：复制进草稿（自包含、占磁盘）还是引原位置（省空间、搬家即失效）？
3. **是否允许一键写入草稿目录**：默认关闭、只在页面提供手动复制指引，还是提供「直接写入 + 重启剪映提示」？
4. **字幕内容**：是否带「说话人：」前缀？字号/位置是否用固定默认（8.0 号、底部居中）？
5. **帧率**：草稿固定 30fps，还是跟随成片探测值（成片当前是 24fps）？
6. **BGM 与配音是否同时保留**：分镜级下若同时铺混音轨会重复出声，是否只在 `render` 模式保留混音？
7. **交付形态**：只出文件夹，还是同时出 zip？
8. **CapCut 国际版**是否纳入同一套实现（路径可配即可，基本零成本）。

---

## 10. 实现后的验证计划（必须做，不能跳过）

本机未安装剪映，以下每一步都需要人工在真实剪映里完成：

**Step 1 · 冒烟草稿（最小可用）**
- 生成 1 条 3 秒纯色视频 + 1 段 TTS + 1 条字幕的草稿。
- 验收：剪映能出现在草稿列表（重启后）；打开无报错；画面/声音/字幕时间对齐；能拖动、能导出。

**Step 2 · 真实章节（分镜级）**
- 验收：视频轨段落数量与时间轴一致；无重叠报错；配音逐行对位；BGM 有声音；字幕文本无乱码；媒体全部在线（无「媒体丢失」）。

**Step 3 · 搬家测试**
- 把草稿文件夹拷到另一台机器/另一个路径，验收素材是否仍在线（验证复制策略与路径重写）。

**Step 4 · 版本矩阵**
- 剪映 10.8、11.x（以及可选 CapCut）各开一次，记录是否需要 `draft_info.json`、是否提示升级、是否有字段告警。

**Step 5 · 回归**
- 确认 `ndw export --format srt/bundle` 与导出页原有功能未受影响。

---

## 11. 参考来源

- pyJianYingDraft（剪映草稿生成，5.9/10.8 功能矩阵、模板 JSON、素材与文本结构）：<https://github.com/GuanYixuan/pyJianYingDraft>
- pyJianYingDraft 模板 `draft_content.json`：<https://raw.githubusercontent.com/GuanYixuan/pyJianYingDraft/main/pyJianYingDraft/assets/draft_content_template.json>
- pyJianYingDraft 模板 `draft_meta_info.json`：<https://raw.githubusercontent.com/GuanYixuan/pyJianYingDraft/main/pyJianYingDraft/assets/draft_meta_info.json>
- 剪映 6.0+ 草稿加密说明（capcut-cli）：<https://raw.githubusercontent.com/renezander030/capcut-cli/HEAD/docs/jianying-encryption.md>
- CapCut/剪映草稿 schema 总览、版本差异、轨道与片段（capcut-cli-david）：
  - <https://cdn.jsdelivr.net/npm/capcut-cli-david@1.13.0/docs/draft-schema/00-overview.md>
  - <https://cdn.jsdelivr.net/npm/capcut-cli-david@1.13.0/docs/draft-schema/05-version-differences.md>
  - <https://cdn.jsdelivr.net/npm/capcut-cli-david@1.13.0/docs/draft-schema/01-tracks-and-segments.md>
- macOS 草稿路径（掘金）：<https://juejin.cn/post/7341288829684318260>
- CapCut Mac 草稿路径：<https://cutconvert.com/guides/where-are-capcut-projects-saved>、<https://filepathgeek.com/posts/capcut-drafts-location/>
- 火山引擎剧创「导出的剪映草稿如何导入剪映」（复制文件夹 + 重启剪映）：<https://www.volcengine.com/docs/87744/2261813?lang=zh>
- 剪映草稿「媒体丢失 / 链接媒体」：<http://m.toutiao.com/group/7345467435732189747/>
- 草稿内路径占位符 `##_draftpath_placeholder_##`：<https://cdn.jsdelivr.net/npm/capcut-cli-david@1.3.0/COMPATIBILITY.md>、<https://deepwiki.com/emosheeep/capcut-export/6-reference>

---

## 12. 实现状态（2026-11-11）

实现按用户拍板收敛，以下为最终口径，与上文「可选方案」不一致处以本节为准：

| 项 | 最终实现 |
| --- | --- |
| 交付形态 | 只出 zip；不做 `--install`，不直接写剪映草稿目录 |
| 轨道粒度 | 仅分镜级 `shot`（视频段 + 关键图），不做成片级 |
| 素材 | 复制进草稿 `media/`，自包含；导出前做「引用 ↔ 实际文件」对账，缺一个即失败并列出缺失项 |
| 路径 | 素材与 `draft_meta_info` 的绝对路径按配置项 `export.jianying.drafts_root` + 草稿名写入（解压后整份复制即生效） |
| 帧率 | 固定 30 |
| 字幕 | 全部有文本台词逐句生成，不带「说话人：」前缀；白字 8.0、黑边、底部居中；缺时间的行按 `tts.duration_ms` 或字数估算补齐，并写入 `subtitle_gaps` |
| 声道 | 只铺逐行配音 + 音效 + BGM，不铺混音轨 |
| 兜底 | 同写 `draft_content.json` 与 `draft_info.json`；另出 `draft_meta_info.json`、`manifest.json`、`导入说明.txt` |
| 入口 | `ndw export --format jianying`；导出页「生成剪映草稿」卡片 |
| 产物 | `<工程>/exports/jianying/<草稿名>/` 与同名 `.zip` |

静态检查与示例工程实跑结果见《项目修改记录文档.md》同日条目；**未在剪映中实测**。
