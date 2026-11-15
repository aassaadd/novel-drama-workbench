# 阶段 6 待办方案（待确认，未动代码）

> 范围：① `cli_bridge` 退役；② `chapter.scenes` 废弃、由 `Script.shots` 唯一承接。
> 结论先行：两者都**不建议一次性切换**；按「先统一读、再切写」「先迁简单路由、长任务留隔离」推进，每步可独立比对与回滚。

## 一、cli_bridge 退役

现状：[cli_bridge.py](../src/novel_drama/interface/http/cli_bridge.py) 238 行，把请求拼成 CLI 字符串，`main()` 内联执行（秒级）或子进程执行（9 个长命令），再把退出码映射为 HTTP 状态。

### 1. 调用方清单（7 个文件）

| # | 文件 | 承担什么 | 现用命令 | 直调后的主要变化 |
| --- | --- | --- | --- | --- |
| 1 | `deps.py` | 定义 `run_cli(config, *argv)`，给 config 路由用 | 透传 | 变成 application 调用入口；`config_path` 不再需要透传 |
| 2 | `routers/config.py` | ①`/api/ui/project/init` 新建工程 ②设置页读写（`_cli`） | `init`；`setting …` | 返回 application 结果对象，前端字段需保持一致 |
| 3 | `routers/story/chapter.py` | `/api/ui/chapter/mix` 混音 | `mix --render` | 同步调用变长（秒~分钟），需走任务化 |
| 4 | `routers/align.py` | 真人录音写入后重建时间轴（惰性导入） | `timeline build` | 同上，且当前在请求内串行多章重建 |
| 5 | `routers/voice/tools.py` | 11 条路由：时间轴构建 ×5、`tts providers`、`cast check`、试音/批量等 | `timeline build`、`tts providers`、`cast check` | 读模型可直接换；`tts batch` 仍是独立子进程（529/1213 行，不属 bridge） |
| 6 | `routers/voice/blockers.py` | 提供 `_cli`/`_respond` 给 tools 复用（非路由） | 同上 | 随 tools 一起迁；模块若无实际调用方可删 |
| 7 | `routers/common.py` | `_business_error` 用 `fail_body` 拼错误体（1 处） | — | 改由 `use_cases` 统一抛 `HTTPException` |

> 说明：`align.py` 里另外 3 处 `run(` 是 ffmpeg（`composition.run`），不在退役范围。

### 2. 行为会变在哪（重点）

1. **退出码 → HTTP 状态**：`use_cases` 已完整镜像 `EXIT_TO_STATUS`（1→409、2→400、3→501、4→412、5→500、6→422，前置缺失含「不存在」降为 404）。缺口只有「拒绝执行 exit 1」目前没有对应异常类型，需要 application 明确抛出（新增一个 `RefusedError` 或复用 `PreconditionError` 并带 code=1）。**这会改变异常类型边界，需要你确认命名与语义。**
2. **长命令的子进程隔离**：9 个命令现在走独立进程，不占 `_RUN_LOCK`，页面其它接口不被阻塞。直接改同进程同步调用会让「生成期间切页面」卡住，属于可感知的体验回退。
   - 建议：长任务一律改为「创建任务 + 轮询」（现有 `job_tracker`/任务页已具备），HTTP 只负责入队；
   - 仍需要进程隔离的（模型/ffmpeg 崩溃会拖垮服务），保留一个**通用任务执行器**：子进程调用 application 入口函数（不是 CLI 字符串），透传 trace；
   - 明确不做：把长命令简单改成同步直调。
3. **输出通道**：现在读 CLI 的 `--json` stdout；直调后要在路由里序列化 application 返回值。要求响应体字段**逐字节一致**，否则前端 281 处调用里对应页面会坏。
4. **观测**：`_record_cli` 记录 argv/退出码/耗时；直调后改在任务层记录同样的字段，`/api/logs` 的展示口径不能变。
5. **参数校验**：现在 argparse 负责（exit 2）；直调后由 FastAPI/application 校验，错误消息要与现状对齐（`[TAG] 前缀` 剥离规则已在 `use_cases` 实现）。
6. **串行锁**：`_RUN_LOCK` 消除了同进程 stdout 抢占；直调后不再需要，但要确认写工程仍靠文件锁（`gpu.lock` / store 级锁）串行。

### 3. 迁移顺序（每个提交只动一个路由文件）

1. `use_cases` 补齐 409/501 与「拒绝执行」语义，单测覆盖 6 种状态码；
2. `config.py`（init + 设置读写）→ 直调 application；比对 init 产物与设置页响应；
3. `chapter.py` 的 mix、`align.py` 的时间轴重建 → 先接任务化，再替换调用；
4. `voice/tools.py` + `blockers.py` 的读命令（timeline/tts providers/cast check）→ 直调；
5. `common.py` 的 `fail_body` 收口到 `use_cases`；
6. 删除 `cli_bridge.py` 与 `deps.run_cli`，全仓禁止 `subprocess` 调 `novel_drama.cli`（分层守卫加规则）。

### 4. 比对与回滚

- **路由级**：旧/新两套服务 + 同一工程副本，固定请求集逐字节对比（含成功/失败路径），错误路径覆盖 400/409/412/422/500/501；`/openapi.json` 全等。
- **产物级**：`mix`/`export`/`timeline build` 前后 `timelines/*.json`、`exports/` 哈希一致。
- **任务级**：离线适配器跑「创建任务→轮询→产物」，并测「生成期间并发读接口」的响应时间不退化。
- **回滚**：逐路由提交，单提交 revert 即可；`cli_bridge` 保留到最后一个调用方迁完；最差整分支回滚。

## 二、`chapter.scenes` 废弃、`Script.shots` 唯一承接

现状（代码统计，不含 pycache）：

- `chapter.scenes` 读取点 102 处、写入点 33 处；`scripts/*.json` 的 `shots` 相关 25 处。
- 双写同步在 `director/materialize.py`；场景由 `orchestration/pipeline.split_chapter_scenes` 生成并写回章节。
- 同一 `line_id` 在 `chapters/*.json` 与 `scripts/*.json` 各存一份，stale 判定与告警定位因此混乱（领域文档 §7.5 / §9 已标注）。

### 1. 影响面

| 区域 | 代表文件 | 读/写 |
| --- | --- | --- |
| 场景生成 | `orchestration/pipeline.py`（split_chapter_scenes / split_scene_lines） | 写 |
| 台本同步 | `director/materialize.py` | 双写 |
| 标注/重切 | `application/script/annotate.py`、`orchestration/resplit.py` | 写 |
| 行编辑 | `interface/cli/commands/line.py`、`media.py`、`voice.py` | 读写 |
| 配音规划 | `orchestration/pipeline.build_tts_jobs`、`tts/*` | 读 |
| 时间轴/混音/导出 | `adapters/media/*`、`commands/media.py` | 读 |
| 选角/告警 | `adapters/persistence/voice_cast.py`、`routers/voice/blockers.py` | 读 |
| HTTP 读模型 | `interface/http/routers/*`、`routers/stream.py` | 读 |

### 2. 迁移路径（4 步，每步 0 行为变化或可开关）

1. **统一读入口**：新增 application 读模型 `lines_of(chapter)`，默认仍读 `chapter.scenes`；加开关 `NDW_LINES_SOURCE=scenes|script` 用于对照。此步不改任何写路径。
2. **统一实体映射**：`Scene`/`Shot` 共用 `Line` 值对象；`materialize` 从「双向同步」改为「script → scenes 单派生」，scenes 变成派生产物。
3. **切换唯一写点**：行编辑只写 `scripts/*.json`，scenes 由派生产出（幂等重建）；开关默认切到 `script`。
4. **停写并移除字段**：schema 保留 `scenes` 只读一个版本周期；提供 `ndw` 迁移/自检补齐旧工程；确认无读点后从 `Chapter` 实体与 schema 移除。

### 3. 比对与回滚

- 语料：`tt1`（25 章）、`turn_ui_check`、`ndw_annotate_check`。
- 每步：`line list/diff`、`tts plan`、`timeline build`、`mix`、`export`，对比 `timelines/*.json` 与 `exports/` **逐字节**；写命令连跑两次验证幂等（第二次 0 文件变化）。
- 前端：行编辑（改/切分/合并/撤销）走查，console 0 warn 0 error。
- 回滚：开关切回 `scenes`；第 4 步前 scenes 写路径一直保留；每步独立提交。

### 4. 需要你确认的点

1. 顺序是否按「先统一读（1）→ 统一实体（2）→ 切唯一写点（3）→ 移除字段（4）」？
2. `chapter.scenes` 只读保留多久：一个版本周期，还是确认无读点后立即移除？
3. 是否需要提供旧工程迁移命令（`ndw migrate scenes`），还是只在加载时懒回填？
4. 「拒绝执行 exit 1」在 application 里用哪种异常表达（新增 `RefusedError` / 复用 `PreconditionError` 带 code）？
