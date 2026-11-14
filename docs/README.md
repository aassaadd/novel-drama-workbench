# 文档索引

> **项目状态：未上线（内部开发中）**——所有改动以新代码为唯一实现，不为旧工程、旧数据、旧代码做兜底、兼容分支、迁移脚本或双写；旧工程数据可直接重建或丢弃。仅当用户明确声明「当前项目已上线」后才要求兼容。本目录中所有「兼容旧数据」「零改动兼容」「旧工程平滑升级」「迁移 / backfill 兼容」类表述，**未上线阶段暂不要求**，仅保留供上线后参考。

本目录按「基线设计 → 迭代 spec → 调研」三类组织。状态以代码与实测为准，文档头部的旧状态若与下表冲突，以下表为准。

## 基线设计（长期有效，随实现增量修订）

| 文档 | 内容 | 状态 |
| --- | --- | --- |
| [PRD.md](PRD.md) | 目标用户、场景、核心流程、MVP 边界、开放问题 | 草案 v2，部分结论已被后续 spec 覆盖 |
| [design-v2.md](design-v2.md) | 七工具工作台与导演脚本的总设计 | 设计基线，已按后续 spec 增量实现 |
| [data-model.md](data-model.md) | 实体、字段、状态机、幂等与增量 | 基线，持续增量（v2 字段均为可选） |
| [architecture.md](architecture.md) | 分层、适配层、ComfyUI 网关、串行调度 | 基线；文中「全部占位」的描述已过时 |
| [ui-plan.md](ui-plan.md) | 页面/面板与 CLI 命令清单 | 基线，页面已按 7 个 hash 路由落地 |
| [roadmap-remaining.md](roadmap-remaining.md) | 剩余里程碑、依赖顺序与验收标准 | 早期路线图，R 系列推进后仅作历史参考 |
| [tts-line-state-and-seams.md](tts-line-state-and-seams.md) | 台词行状态、diff/revert、接缝裁切、虚拟分组 | 已实现（行状态/diff/revert/分组/裁切均在用；文档头部「未实现」未更新） |

## 迭代 spec（按 R 编号）

| 文档 | 范围 | 状态 |
| --- | --- | --- |
| [spec-r13-indextts25.md](spec-r13-indextts25.md) | IndexTTS 2.5 接入与 TTS provider 层 | 已实现并实测出音 |
| [spec-r14-full-pipeline.md](spec-r14-full-pipeline.md) | 小说 → 有声剧全链路（produce） | 已实现 |
| [spec-r15-resident-tts-worker.md](spec-r15-resident-tts-worker.md) | 常驻配音 worker（整批复用进程） | 已实现（仅静态检查，未做整章跑测） |
| [spec-r16-jianying-draft-export.md](spec-r16-jianying-draft-export.md) | 剪映草稿 zip 导出 | 已实现（未做剪映侧人工导入实测） |
| [spec-r17-voice-design.md](spec-r17-voice-design.md) | 按人物设定生成音色（本地 VoxCPM2） | 已实现；真实 worker 跑通单条候选与入库，未做 4 条候选的完整页面验收 |
| [spec-r19-bgm-modes-and-memory.md](spec-r19-bgm-modes-and-memory.md) | 配乐叙事/铺底两档 + ComfyUI 显存释放 | 已实现并实测（归一、释放、耗时均有记录） |
| [spec-r20-sfx-provider.md](spec-r20-sfx-provider.md) | 音效 provider：本地 Stable Audio 3 Small-SFX（独立 venv + 常驻 worker） | 已实现并实测出音；2 条样例与参数快扫；页面未接入 |
| [spec-r21-bgm-local-provider.md](spec-r21-bgm-local-provider.md) | 配乐 provider：本地 Stable Audio Open 1.0（独立 venv + 常驻 worker，可回落 ACE-Step） | 已实现并实测两档样例；媒体页已接入后端选择与本地生成；ACE-Step 出音回归待 ComfyUI 启动 |
| [spec-r22-hierarchical-outline.md](spec-r22-hierarchical-outline.md) | 三级分层大纲生成（幕 → 章 → 节拍） | 已实现；桩验证层级进度、单幕失败与续跑，未做页面实测 |
| [spec-r23-chapter-continue-panel.md](spec-r23-chapter-continue-panel.md) | 正文续写参数面板（与大纲续写统一） | 已实现；桩验证面板入参与需求回显，未做页面实测 |
| [spec-r24-script-annotate-streaming.md](spec-r24-script-annotate-streaming.md) | 导演标注：按批预算、截断报错、批次续跑、流式与提示词切片 | 已实现；桩验证预算/截断/续跑/进度，未做页面实测 |
| [spec-r25-dialogue-quotes.md](spec-r25-dialogue-quotes.md) | 正文对白统一使用中文全角双引号 “” | 已实现；桩验证引号规范化、漏引号提示与切分兼容 |
| [spec-r26-resplit-scene-line.md](spec-r26-resplit-scene-line.md) | 重新切分场景 / 台词：影响清单、人工修改保护、章节页与标注页入口 | 已实现；桩验证保留模式与 CLI impact，未做页面实测 |
| [spec-r27-character-descriptions.md](spec-r27-character-descriptions.md) | 人物阶段一次产出音色 / 外貌描述（中英文 + 负向）与存量补全（未上线阶段暂不要求回填旧数据） | 已实现；桩验证清洗兼容、补全与重跑，消费端未接入 |
| [spec-r28-outline-inline-edit.md](spec-r28-outline-inline-edit.md) | 大纲页就地编辑幕/章标题与概要和节拍文本 | 已实现；桩验证校验与不写章节文件，未做页面实测 |
| [spec-r29-character-desc-fill-stream.md](spec-r29-character-desc-fill-stream.md) | 补全人物描述流式进度（逐角色、可停止） | 已实现；桩验证事件序列/失败隔离/取消保留，未做页面实测 |
| [spec-r30-script-warning-noise.md](spec-r30-script-warning-noise.md) | 导演标注告警降噪：说话人归一化、越界夹紧、告警分级与合并 | 已实现；桩验证 + 真实工程重跑 45→0 条告警 |
| [spec-r31-anchor-orphan-fix.md](spec-r31-anchor-orphan-fix.md) | 标注锚点误判 orphan（游标漂移 / 标点差异）修复 | 已实现；真实工程副本 orphan 148→0 |
| [spec-r32-warning-locator.md](spec-r32-warning-locator.md) | 标注/配音告警点击定位：台词行 / 镜头 / 告警区三级跳转与降级说明 | 已实现；真实浏览器验证三类目标与降级 |

## 调研（只作依据，不改生产代码）

| 文档 | 内容 | 状态 |
| --- | --- | --- |
| [r20-jianying-draft-compat-review.md](r20-jianying-draft-compat-review.md) | 剪映草稿 10.8/11.x 字段兼容性核对：字段对照、风险等级、最可能问题 | 核对完成；两处确定性修复已应用，剪映侧未实测 |
| [research-sfx-local-commercial-options.md](research-sfx-local-commercial-options.md) | 本地可商用音效（SFX）方案调研：候选模型许可/体积/条件生成/ComfyUI 支持、与本项目匹配度、推荐与备选路线 | 已随实测更新：11 项待查逐条标注，ComfyUI 模板路线已停用，改本项目 provider |

## 梳理（只读整理，不改生产代码）

| 文档 | 内容 | 状态 |
| --- | --- | --- |
| [llm-calling-patterns.md](llm-calling-patterns.md) | LLM 调用模式：适配层与参数来源、单次/多轮与终止条件、工具调用、思考模式、各业务调用点归类、上下文注入与裁剪、代码可见风险 | 按当前代码整理，结论均带源码位置 |

## 阅读顺序建议

1. 先看仓库根目录 [README.md](../README.md) 的「快速开始」与「当前完成度」。
2. 要改数据或接口：先看 `data-model.md` 与 `schema/`。
3. 要改某块能力：先看对应 R spec；spec 头部状态过时以本索引和代码为准。
