# spec-r23 正文续写参数面板（与大纲续写统一）

## 目标

正文续写原本在章节页工具条里用「篇幅下拉 + 单行意图输入」采集参数，与大纲续写的弹面板
交互不一致。本次统一为：点「续写正文」先弹面板，确认后才发起流式预览。

## 面板内容

复用现有 `modal-mask` / `modal-editor` 结构与 `modal-title / modal-hint / stack / modal-foot` 样式：

- 篇幅：短 500 / 中 800 / 长 1200（默认沿用上次选择）；
- 续写起点：`自动（从正文末尾继续）` + 本章节拍列表（从 `/api/ui/outline` 读取，
  第 N 拍对应 `from_beat=N`；读取失败只保留「自动」）；
- 续写需求：多行 textarea，示例 placeholder，下方提示「留空则按当前正文自然续写」；
- Ctrl/Cmd + Enter 直接生成预览；Esc 或点遮罩关闭；
- 底部按钮：取消 / 生成预览；若已有未应用草稿，额外显示「应用续写草稿（+N 字）」；
- textarea `width:100% + box-sizing + resize:vertical`，按钮区 `flex-wrap`，窄屏不挤压。

## 请求与预览

- 预览仍走 `POST /api/ui/chapter/continue/stream`，请求体保持 `{path, chapter, words, intent, retry}`
  并新增可选的 `from_beat`（后端原本就支持）；
- 预览弹窗新增需求行：有需求显示「续写需求：…」，空需求显示
  「未填写续写需求，按当前正文自然续写」；
- 「重抽」复用上次 `{words, intent, fromBeat}`，预览 / 重抽 / 应用 / 取消行为不变；
- 后端 `continue_chapter` 返回值新增 `intent` 回显字段（只增不改，保持兼容）。

## 工具条

移除工具条上的 `chContinueWords` 下拉与 `chContinueIntent` 输入框，只保留
「续写正文」与（有草稿时）「应用续写草稿」，避免两处入口并存。

## 涉及文件

- `src/novel_drama/web/static/page-chapter.js`：`openContinuePanel / submitContinuePanel`、
  `requestContinue(retry, options)`、预览弹窗需求行；
- `src/novel_drama/web/static/index.html`：工具条清理；
- `src/novel_drama/writing/continuation.py`：返回值回显 `intent`。

## 验证

- 桩（极简 DOM + SSE fetch 桩）：面板显示 2 个下拉、3 个起点选项（自动 + 2 个节拍）、
  多行需求与提示；预览入参 `words=1200 / intent=收束师父的旧案 / from_beat=2`；
  预览弹窗回显「续写需求：收束师父的旧案」；空需求入参 `intent=""` 且弹窗显示
  「未填写续写需求，按当前正文自然续写」；
- 工具条 HTML 已无 `chContinueWords / chContinueIntent`；
- `python -m compileall -q src`、`node --check page-chapter.js` 通过；未启动服务、未做浏览器实测。
