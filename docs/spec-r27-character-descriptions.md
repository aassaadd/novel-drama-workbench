# spec-r27 人物阶段一次产出音色 / 外貌描述（中英文 + 负向）

## 目标

人物生成阶段就把后续音色与出图要用的描述性字段一次产齐，避免到音色面板、出图环节再临时拼描述。
本 spec 只保证数据在人物阶段齐备，**不动音色面板与出图消费端**。

## 字段（均为可选新增，不改已有字段语义）

| 字段 | 说明 | 用途 |
| --- | --- | --- |
| `voice_description_zh` | 中文音色描述：音色质感、语速、语气、年龄感、口音，80-160 字 | 界面展示与人工校对 |
| `voice_description_en` | 同上的英文描述，纯英文，覆盖 timbre / pace / tone / age feel / accent | 直接作为 TTS / voice design 的 prompt |
| `appearance_desc_zh` | 中文正面外貌：脸型、五官、发型、身形、惯常着装与气质 | 界面展示与人工校对 |
| `appearance_desc_en` | 英文正面出图 prompt：纯英文、具体可见特征 | 图片生成 prompt |
| `appearance_negative_zh` | 中文负向说明：名词短语列出应排除的元素（眼镜、胡须等） | 界面展示与人工校对 |
| `appearance_negative_en` | 英文 negative prompt：逗号分隔的排除项 | 出图 negative prompt |

写作要求统一收口在 `writing/service.py` 的 `_CHARACTER_DESC_RULES`：
禁止 `young woman`、`beautiful` 这类空泛描述；负向项用名词短语，不写 `not/no` 否定句堆砌。

## 生效范围

- 工具 schema `_CHARACTER_TOOL_SCHEMA`：新增 6 个 properties（批量与单人物共用）；
- 提示词：`generate_characters`（一次生成多个）、单人物生成/编辑
  （`section=character`）、两阶段逐人生成 `character_plan._character_prompt` 均追加 `_CHARACTER_DESC_RULES`；
- 清洗：`_clean_character_row` / `_clean_character_entry` 保留 6 个字段并做长度限制；
- 离线适配器 `OfflineWritingAdapter._desc_fields`：规则化产出 6 个字段，保证零依赖路径也有数据。

## 兼容

- `schema/character.schema.json` 只新增可选字段，不加 required；旧工程缺字段可正常读取与保存；
- `models/entities.new_character` 新增字段默认空串，新建人物字段齐全；
- 旧字段 `appearance_prompt` / `negative_prompt` 为空时，用 `appearance_desc_en` / `appearance_negative_en` 回填，
  已有内容绝不覆盖，既有出图链路无需改动。

## 存量补全

`character_plan.fill_character_descriptions(store, config, names=None, on_progress=None)`：

- 跳过旁白；`names`（ID 或姓名）可按角色过滤；
- 逐个角色调用 `emit_character`，只合并缺失且非空的字段，已有内容不覆盖；
- 每成功一个立即 `save_characters` 落盘，单人失败只记 `failures`，不影响其他人；
- 重跑自动跳过已齐全角色，因此失败项可重试；
- 返回 `{total, updated, skipped, failures, summary}`。

CLI：`ndw cast fill-descriptions [--name 角色]... [--json]`，存在失败时退出码 1，便于重试判断。

## 未做（后续单独接入）

- 音色面板读取 `voice_description_*`；
- 出图读取 `appearance_desc_*` / `appearance_negative_*`；
- 人物页展示中文描述。

## 验证

- 桩验证 22 项全过：旧档案缺字段可保存/读取；清洗保留 6 字段；旧字段缺省时用英文回填且不覆盖已有；
  离线适配器三份英文描述均为纯英文且覆盖 timbre；补全只补缺失、不覆盖已有、旁白跳过、
  单人失败不影响其他人、重跑只处理失败者、按 name 过滤时已齐全计入 skipped；
- `python -m compileall -q src` 通过；`ndw cast fill-descriptions --help` 参数解析正常；
- 未启动服务、未做浏览器实测。
