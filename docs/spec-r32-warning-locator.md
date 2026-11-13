# R32 标注告警点击定位

## 目标

「需要人工处理」的告警不再只给一行文字：点一条就跳到能处理的位置并高亮，减少人工翻找。
可自动修正的信息级提示（`【已自动修正】`）不提供定位入口。

## 定位分级

| 告警类型 | 结构化信息 | 点击后 |
| --- | --- | --- |
| 说话人未识别（`speaker_unknown`，正文锚点可用） | 章节、镜头序号、行 ID、镜头内行序号、台词文本 | 切到「配音」页并选中该行；滚动可见、行短暂闪烁；右栏展示编辑表单并聚焦 / 展开说话人下拉 |
| 台词未在正文找到（`orphan`） | 章节、镜头序号、镜头内行序号、片段 | 定位到「标注」页对应 `@shot` 镜头：滚动到该行、镜头与行高亮 10 秒，并显示台词片段说明条 |
| 音效序号非法（`sfx_position`，0/负数） | 章节、镜头序号、原始位置标记 | 定位到对应 `@shot` 镜头 |
| 无结构化信息 / 镜头在页面中找不到 | 仅有章节 | 滚到本章告警区并附加持久说明「没有更精确的台词或镜头位置」；不产生死链 |

信息级提示保持折叠纯文本，不可点击。

## 后端

### `director/markup.py`

- `parse_markup` 新增可选参数 `diagnostics`，硬告警产生时原地追加：
  ```json
  {
    "kind": "orphan | speaker_unknown | sfx_position",
    "message": "与 warnings 完全一致的文案",
    "chapter_id": "chp_...",
    "shot_index": 4,
    "scene_index": 3,
    "scene_label": "镜头名",
    "line_id": "lin_...",
    "line_index": 1,
    "text": "台词全文（≤200 字）",
    "snippet": "台词片段（≤60 字）",
    "speaker": "未识别名字（仅 speaker_unknown）",
    "anchor_state": "exact | shifted | orphan",
    "char_start": 123,
    "char_end": 140,
    "detail": "after-line=0（仅 sfx_position）"
  }
  ```
- 信息级提示不写入 `diagnostics`；`warnings` 字符串保持不变，CLI / 导出兼容。
- `chapter_id` 与 `scene_label` 在解析末尾统一补全。

### `writing/service.py`

- `suggest_script` 传入 diagnostics，返回值新增 `warning_items`；只进返回值，不写入 `director-script.schema.json` 校验的 `script`，不改 schema。

### `web/app.py`

- `GET /api/ui/script` 除回显落盘标注外，重新解析并返回 `warnings` 与 `warning_items`，页面重进后仍可看到并点击告警；解析失败只清空告警展示，不影响标注回显。

## 前端

### `page-script.js`

- 硬告警渲染为 `button.warn-link`，右侧标注目标层级（如「定位 · 台词行 · 镜头 3」「定位 · 镜头 3 · 第 1 行」「定位 · 本章」）。
- 按 `kind` 分派：
  - `speaker_unknown` 且锚点不是 orphan → `#/voice` + `NW.openLine(chapter, line_id)`；行闪烁，聚焦 `#fSpeaker` 并在浏览器支持时 `showPicker()`。
  - `orphan` / `sfx_position` → `#/script`，按 `data-shot-index` 滚动到镜头并高亮目标行、插入 `.warn-snippet` 片段说明。
  - 行不存在（预览未应用）或镜头找不到 → 降级到告警区，并 toast 说明降级原因。
- `ensureView` 在已处于目标视图时不重复触发页面加载；跨视图切换时等待 `loadPromise`，避免随后的 `innerHTML` 重渲染冲掉滚动与高亮。
- 镜头块可能很长时，滚动目标取「台词行」而不是整块，避免整块居中后目标行落在视口外。

### `styles.css`

- 新增 `.warn-link` / `.warn-go` / `.warn-tip`、`.shot.is-warn-target`、`.mk-line.is-warn-line`、`.warn-snippet`、`.warn-level-note`、`.row.row-flash` 及对应闪动动画。

## 验证

- 桩验证（不调模型）：`orphan` / `speaker_unknown` / `sfx_position` 三类均带齐章节、镜头、行号、行 ID、片段；信息级不进入 diagnostics。
- 真实浏览器（独立端口 8899 + 真实工程临时副本）：
  - 台词类：点「镜头 1 说话人未识别」→ 切到 `#/voice`，行选中，右栏出现说话人下拉，toast「已定位到台词行：镜头 1 · 第 5 行」。
  - 场景类：在临时标注注入一行正文不存在的台词 → 点该 orphan 告警，停留在 `#/script`，镜头块 `is-warn-target`，片段说明条可见。
  - 章节类降级：把 `@shot 4` 改为 `@shot 04`，后端镜头序号为 4、页面数据属性为 `04`，定位找不到 → 告警区 `is-warn-target` 并显示「这类告警没有更精确的台词或镜头位置」。
  - 服务端日志无 500 / Traceback；页面无红色错误。
- 验证后已停止服务，删除临时工程、日志与截图。

## 限制

- 预览未应用时，orphan 行的 `line_id` 尚未写入章节，定位会降级到标注镜头（并在 toast 说明）。
- 原生 `<select>` 的展开依赖 `showPicker()`，浏览器拒绝时退化为聚焦。
- 镜头序号与页面展示的 `@shot` 文本不一致（如补零）时按「找不到精确镜头」降级到告警区。
