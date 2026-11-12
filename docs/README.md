# 文档索引

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
| [spec-r18-voice-bgm-preplanning.md](spec-r18-voice-bgm-preplanning.md) | 音色编排、旁白/龙套音色池、镜头级 BGM 描述 | 已实现并做界面验证，待整体验收 |
| [spec-r19-outline-and-chapter-continuation.md](spec-r19-outline-and-chapter-continuation.md) | 大纲续写与正文续写 | 已实现：CLI 闭环实测；正文页/大纲页浏览器实测通过 |
| [spec-r19-bgm-modes-and-memory.md](spec-r19-bgm-modes-and-memory.md) | 配乐叙事/铺底两档 + ComfyUI 显存释放 | 已实现并实测（归一、释放、耗时均有记录） |

## 调研（只作依据，不改生产代码）

| 文档 | 内容 | 状态 |
| --- | --- | --- |
| [research-r19-continuation-competitive-analysis.md](research-r19-continuation-competitive-analysis.md) | 同类产品续写入口/长度/上下文/落盘做法 | 调研定稿，证据按官方/二手分级标注 |
| [r20-jianying-draft-compat-review.md](r20-jianying-draft-compat-review.md) | 剪映草稿 10.8/11.x 字段兼容性核对：字段对照、风险等级、最可能问题 | 核对完成；两处确定性修复已应用，剪映侧未实测 |

## 阅读顺序建议

1. 先看仓库根目录 [README.md](../README.md) 的「快速开始」与「当前完成度」。
2. 要改数据或接口：先看 `data-model.md` 与 `schema/`。
3. 要改某块能力：先看对应 R spec；spec 头部状态过时以本索引和代码为准。
