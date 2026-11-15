# 阶段 5-6：CLI 迁入 interface/cli（交接文档）

> 本文件只服务阶段 5-6 的施工与上下文交接，完成后随阶段收尾归档。
> 分支 `refactor/domain-model`；前置提交 `e40628a`（5-5 HTTP 路由拆分，已验证通过）。

## 1. 目标结构

```
src/novel_drama/
  cli.py                          # 兼容入口（仅 re-export main / build_parser，供 pyproject `ndw` 与 `python -m novel_drama.cli`）
  interface/cli/
    __init__.py                   # 导出 main / build_parser
    main.py                       # 顶层参数、组装各域 add_parser、main()、_init_oplog、退出码映射
    common.py                     # 退出码常量、PreconditionError/TaskFailedError、_emit/_resolve_store/_tts_service/cast_hint
    helpers.py                    # _build_adapters/_make_queue/_queue_service/_sort_jobs_by_group/_drain/_add_scope_args
    commands/
      __init__.py
      project.py                  # init / doctor / project{info,list,set}
      chapter.py                  # chapter{add,list,show,import,source}；generate/save/edit/summarize/continue 的处理函数仍各自在 writing/continuation
      writing.py                  # ← cli_writing.py
      continuation.py             # ← cli_continue.py
      setting.py                  # ← cli_setting.py
      outline.py                  # ← cli_outline.py
      world.py                    # ← cli_world.py
      character.py                # ← cli_character.py + cli.py 的 add/list/bind-voice
      cast.py                     # ← cli_cast.py
      voice.py                    # ← cli_voice.py
      script.py                   # cmd_script（voice.py）+ cmd_script_suggest（writing.py）
      line.py                     # ← cli_line.py（含 scene split）
      group.py                    # ← cli_group.py
      tts.py                      # tts{plan,run,providers,route,say,compare}（cli.py）+ tts batch（cli_media）
      media.py                    # ← cli_media.py + cli.py 的 cmd_media / cmd_mix
      music.py                    # ← cli_music.py + cli_media 的 cmd_bgm
      sfx.py                      # ← cli_sfx.py
      comfy.py                    # ← cli_comfy.py
      models.py                   # ← cli_models.py
      queue.py                    # cli.py 的 cmd_queue
      pipeline.py                 # cli.py 的 cmd_pipeline / cmd_serve
      produce.py                  # ← cli_produce.py
```

约束：单文件 < 1500 行；函数级中文注释；interface 只依赖 domain / application / interface / infrastructure（+ 迁移期 legacy），**不 import `adapters.*`**，适配器符号统一经组合根 `novel_drama/composition.py` 取。组合根需补齐 CLI 用到的符号（媒体、持久化仓储、ComfyUI、art 等）。

## 2. 命令清单来源与归属

- 来源：`build_parser()` 的 subparsers 全树，共 148 个命令节点（含分组节点）。导出脚本：解析 `novel_drama.cli.build_parser()`，逐节点记录 `path` 与 option 集合，输出 JSON 供比对。
- 归属表：
  | 命令组 | 归属模块 |
  | --- | --- |
  | init / doctor / project | commands/project.py |
  | chapter（12） | 处理函数：chapter.py（add/list/show/import/source）、writing.py（generate/save/edit/summarize）、continuation.py（continue）；parser 统一在 chapter.py |
  | setting（9） | setting.py |
  | outline（6） | 处理函数：writing.py（show/generate/apply）、outline.py（check/bind）、continuation.py（continue）；parser 在 outline.py |
  | world（5） | world.py |
  | character（8） | character.py |
  | cast（4） | cast.py |
  | voice（11） | voice.py |
  | script（6） | voice.py（syntax/parse/apply/show/validate）、writing.py（suggest）；parser 在 script.py |
  | scene / line（10） | line.py |
  | group（3） | group.py |
  | tts（7） | tts.py |
  | sfx（4） / image（2） / video（2） | sfx.py（generate/providers）、media.py（plan/run） |
  | timeline / concat / mux / mix / export（8） | media.py |
  | bgm（5） | music.py |
  | comfy（10） | comfy.py |
  | queue（4） | queue.py |
  | pipeline / serve | pipeline.py |
  | models（4） | models.py |
  | produce / run show | produce.py |

## 3. 比对口径（沿用 5-5）

1. `--help` 全树逐条一致：对每个命令节点跑 `<cmd> --help`，比对帮助文本（按 5-5 的 normalize：仅去路径/时间等易变行，其余逐字）。
2. 28 条命令并跑：`/tmp/ndw_s5/compare.py`（`NDW_OLD_ROOT=/tmp/ndw_s5/base55/src`、`NDW_NEW_ROOT=<仓库>/src`，工程副本 `cmp_old` / `cmp_new`），stdout、退出码、落盘产物三者 0 差异。
3. `pytest`（`NDW_GOLDEN_PROJECTS=...`）、`python scripts/check_layering.py`、`ruff check src`（基线 8 项既有告警，不新增）。
4. 真机走查：CLI 全命令树 `--help` + 真实工程上跑 init/chapter/line/voice 等常用命令，退出码与输出可读；页面走查留到 5-7 阶段末。

## 4. 进度

| 组 | 内容 | 状态 |
| --- | --- | --- |
| 0 | 本交接文档 + 命令清单导出 | 已完成 |
| 1 | `interface/cli/` 骨架、`common.py`、组合根补齐、18 个 `cli_*.py` 迁入（提交 `30ae6dc`） | 已完成 |
| 2 | 拆 `cli.py` → `main.py` + `helpers.py` + `commands/*`；`cli.py` 收为 14 行兼容入口（提交 `c33e4f5`） | 已完成 |
| 3 | 全量比对（149 条 help / 28 条命令 / pytest 256 passed / 分层 0 违规 / ruff 无新增）+ 修改记录（提交 `ab19527`） | 已完成 |

### 验证结果

- `--help`：149 个节点（含根命令）逐字一致，0 差异。
- 28 条命令并跑：25 只读 + 3 离线写，stdout/退出码/落盘 0 差异。
- pytest 256 passed / 1 skipped；分层 214 文件 0 违规；ruff F/I 仅 6 条既有告警。
- 真机走查：`http://127.0.0.1:8790`（工程 `turn_ui_check`）项目/角色/音色三视图渲染正常，DevTools 控制台 0 警告 0 错误，20 个请求全部 200。
- 临时产物（worktree `base52..55`/`new52..54`/`old`/`headtree`、`/tmp/ndw_s5`、`/tmp/ndw_p4`）已清理。

### 5-7 收尾（已完成）

- `web/actions.py` 按用户决定「只搬家」→ `interface/http/cli_bridge.py`，7 处调用方改新路径，行为/退出码映射/子进程隔离不变（`593667c`）。
- 并跑：29 只读 + 3 离线写，0 差异。角色生成的差异已证明是离线生成器按工程路径取种子（旧代码两份不同目录副本同样不同），与搬家无关。
- pytest 256 passed / 1 skipped；分层 215 文件 0 违规；ruff F/I 仅 6 条既有告警；真机起服务（8791）root/health/characters/voices 全 200。
- 清理：`web/static/`（仅 .DS_Store，无跟踪文件）与 `web/.DS_Store` 已删。

### web/ 残留清单（阶段 5 结束时）

| 路径 | 行数/内容 | 为什么留 | 归属阶段 |
| --- | --- | --- | --- |
| `web/use_cases.py` | 93 行，`call()` 把 `AdapterError`/`PreconditionError`/`TtsError`/schema 异常翻译成 HTTP 状态码 | 被 6 个 router 引用；是接口层错误翻译，搬家需改 6 处 import，属纯位置调整但与 `cli_bridge` 职责相邻，留待一并处理 | 阶段 6（并入 `interface/http`，与 `cli_bridge` 一起整理） |
| `web/__init__.py` | 1 行 docstring | 包标记，随 `use_cases.py` 一起迁走 | 阶段 6 |
| `web/static/` | 空（仅 .DS_Store） | 旧静态界面，已无跟踪文件 | 已在 5-7 删除 |
| `cli_bridge`（`interface/http/cli_bridge.py`） | 238 行 | 仍是「HTTP → CLI 字符串命令」通道 | 阶段 6 彻底退役：7 处调用方改走 application 用例（行为面变更，需单独立项） |

### 阶段 6 待办（已登记）

1. `web/use_cases.py` + `web/__init__.py` → `interface/http/`。
2. `cli_bridge` 彻底退役：`deps.py`、`routers/{align,common,config,story/chapter,voice/blockers,voice/tools}.py` 改调 application 用例，移除字符串命令与退出码映射。

## 5. 外部引用（迁移时必须同步改）

- `pyproject.toml`：`ndw = "novel_drama.cli:main"`（保留 cli.py 兼容入口即可不动）。
- `src/novel_drama/web/actions.py`：子进程 `python -m novel_drama.cli ...`（兼容入口保留后无需改）。
- `src/novel_drama/web/use_cases.py`：`from ..cli_common import PreconditionError, TaskFailedError` → 改 `..interface.cli.common`。
- `src/novel_drama/orchestration/produce.py`：`..cli_voice` / `..cli_common` / `..cli_media` 三处延迟导入 → 改 `..interface.cli.commands.*`。
