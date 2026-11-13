# spec-r29 补全人物描述流式进度

## 目标

人物页「补全缺失描述」从只有一个转圈的普通请求，改成可看到逐个角色进度、可中途停止的流式操作。

## 事件（SSE）

`POST /api/ui/characters/descriptions/fill/stream`，body `{path, names?}`：

| 事件 | 负载 | 说明 |
| --- | --- | --- |
| `start` | `{ok:true}` | 连接建立 |
| `total` | `{total, pending, skipped, narrator}` | 开始：待处理总数与分类（含跳过项） |
| `batch` | `{index, total, name}` | 每个角色开始，`total` 含跳过项 |
| `chars` | `{chars, index, total, name}` | 当前角色已接收字数（累计） |
| `item` | `{index, total, name, status, reason?, message?}` | 角色结束：`done` / `skipped`（reason=已齐全/旁白）/ `failed` |
| `done` | `{result}` | 最终 `{total, updated, skipped, failures, summary}` |
| `error` | `{message}` | 流程级错误 |

- 跳过项（已齐全、旁白）也走 `batch` + `item`，不再静默。
- 单个角色失败只记 `item.failed` 与 `result.failures`，后续角色继续。
- 页面进度沿用统一组件：`第 k/N 个：角色名 · 已接收 X 字`。

## 停止

- 蒙版里挂「停止补全」，页面按钮同时变成「停止补全」；点击 `AbortController.abort()`。
- 断开后服务端在下一个进度/增量回调处中断（`_progress_response` 的 stop 事件），
  已完成的角色此前已逐个 `save_characters` 落盘。
- 页面提示「已停止补全：已补全 X 个，剩余 Y 个可再点」；重跑只处理仍缺失的角色。

## 兼容

- 原非流式接口 `POST /api/ui/characters/descriptions/fill` 保持不动，
  返回字段 `{total, updated, skipped, failures, summary}` 语义不变。
- `fill_character_descriptions` 的 `skipped` 仍只含「已齐全的非旁白角色」；
  旁白只出现在进度事件里，不改变返回值。
- 不改 schema。

## 顺带修正

补全结果解析同时兼容 `{"character": {...}}` 与 `{"characters": [{...}]}`，
离线适配器（返回后一种）下补全不再必然报「模型没有返回缺失字段」。

## 验证

- 桩验证：`total` 分类正确；4 个角色（含旁白与已齐全）都有 batch；item 依次
  skipped/done/done/skipped；chars 有实际增量；单角色失败不中断且 item 带原因；
  进度回调抛异常时立即中断、已完成角色已落盘、重跑只补剩余；非流式返回字段不变。
- `python -m compileall -q src`、`node --check page-characters.js` 通过；新旧接口均可注册；
  未启动服务、未做浏览器实测。
