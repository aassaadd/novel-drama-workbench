# spec-r28 大纲文本就地编辑

## 目标

大纲页不再只能改世界 / POV 绑定：幕标题与概要、章标题与概要、单个节拍文本都能就地编辑，
已落地与未落地章节一视同仁。只改大纲侧文本，不覆盖正文。

## 交互

- 幕 / 章标题行新增「编辑」按钮；双击幕标题、章标题、节拍文本也可进入编辑。
- 幕 / 章编辑器为标题输入 + 概要多行框 + 「保存 / 取消」；节拍编辑器为单行文本 + 「保存 / 取消」。
- 空标题不允许保存，前端先提示「标题不能为空，请填写后再保存」，后端兜底报错。
- 保存成功后重新拉取大纲；失败按字段给出原因（如「章标题过长：最多 200 字，当前 201 字」）。

## 后端

`writing/outline_edit.update_outline_text(store, act?, chapter?, beat?, fields?)`：

- `fields` 只接受 `title` / `summary`；按对象限制字段（节拍只允许 `summary`）；
- 标题去空白并剥掉序号前缀（复用 `text/titles.strip_index_prefix`），只写序号视为空标题；
- 长度上限对齐 `setting-bible.schema.json`：幕/章标题 200、幕/章概要 2000、节拍 1000；
- 只改设定库大纲文本，不重排序号、不重排节拍、不写章节文件、不碰正文与标注；
- 保存走 `SettingsStore.save`（schema 校验 + 原子写），校验失败不会半写。

接口：`POST /api/ui/outline/text`，body `{path, act?, chapter?, beat?, title?, summary?}`；
业务错误返回 409 与中文原因。

## 兼容

- 不新增 schema 字段，幕仍然只有 `id/title/summary/chapters`，不写 `index`；
- 已落地章节的章节文件（含 `outline` 缓存与正文）保持原样，只有大纲页文本变化。

## 验证

- 桩验证：幕/章/节拍更新、序号与节拍 ID 不变、已落地章节文件字节不变、revision +1、
  保存后 schema 校验通过；空标题、只写序号、超长、越界、未知字段、缺失对象均返回中文原因。
- `python -m compileall -q src`、`node --check page-outline.js` 通过；新接口可正常注册；
  未启动服务、未做浏览器实测。
