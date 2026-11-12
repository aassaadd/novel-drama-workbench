# spec-r24 导演标注：预算 / 截断 / 批次续跑 / 流式

## 背景

`script suggest` 原先按场景串行分批（默认每批 8 个镜头），每批全量重发整章台词；
请求不设 `max_tokens`（缺失配置时按 2048 兜底），文本模式不检查 `finish_reason=length`；
任一批异常整章作废。本次针对这四点做优化，并把标注改为流式。

## 1. 按批估算输出预算

`_script_batch_max_tokens(config, scenes)`：

```
estimate = 400 + 每镜头 320 + 每句台词 45
max_tokens = min(16384, max(estimate, min(16384, omlx.max_tokens 或 0)))
```

8 镜头 / 80 句的批次约 6560 token，不再落到 2048。

## 2. 截断显式报错

流式与非流式都检查 `finish_reason`；等于 `length` 时把该批记为失败，错误文案：

> 第 3–4 个镜头（第 2/3 批）标注输出被截断（finish_reason=length）：本批内容超出输出上限。
> 请调小每批场景数（--scenes-per-round）或提高 omlx.max_tokens 后重试

不再退化成语义含糊的「无法解析标注」。

## 3. 批次失败可恢复

- 每批结果落 `<工程>/.ndw/script_batches/<章节>.json`（完成一批写一次，临时文件 + 原子替换）；
- 单批失败记 `failures[{batch, start_scene, end_scene, message}]`，其它批照常保留；
- 结果新增 `failures / batches / partial`；有失败时**不应用**到章节；
- `resume=true`（CLI `--resume`）只重试失败/未完成的批次，全部成功后删除中间文件；
- 全部批次失败才抛错，错误里带前几批的镜头范围与原因。

## 4. 流式

- 适配器新增 `generate_stream(req, on_delta)`（离线实现一次性回调全文），
  `realtime` 流式解析 SSE 增量并聚合出与非流式一致的 `TextGenResult`；
- 新接口 `POST /api/ui/script/suggest/stream`（SSE）：
  `start → batch{index,total,start,end} → chars{chars,...}* → done(result) / error`；
- 页面显示「第 2/4 批 · 已接收 1.8k 字」，新增「停止标注」按钮中断请求
  （断开后服务端在下一次增量处停止）；
- 流式拼接出的完整标注文本仍走原 `parse_markup`，结果与非流式一致；
- `apply=true` 时在生成完整（无失败批次）后校验并同步到章节，行为与 CLI 一致。

## 5. 提示词切片

每批 prompt 只带本批镜头 + 前/后各一个参考镜头（标记「参考·」，只读不输出），
并写明编号规则：`@shot` 必须是本批范围内的整数、与本批【镜头 N】一一对应，不得重号/跳号/新建。

## 兼容性

- CLI `ndw script suggest` 原参数不变，新增可选 `--resume`；
- 原 `POST /api/ui/script/suggest` 保留，新增可选 `resume` 透传；
- 未改 `director-script` / `setting-bible` schema，未改 `parse_markup`。

## 涉及文件

- `src/novel_drama/writing/service.py`：预算估算、批次循环、截断检查、失败记录/续跑、流式进度、切片 prompt；
- `src/novel_drama/adapters/writing.py`：`generate_stream` 与增量回调；
- `src/novel_drama/adapters/offline_writing.py`：离线 `generate_stream`；
- `src/novel_drama/web/stream_api.py`：`/api/ui/script/suggest/stream`（含 apply 同步）；
- `src/novel_drama/cli.py`、`cli_writing.py`、`web/app.py`：`--resume` 与失败提示；
- `src/novel_drama/web/static/page-script.js`、`index.html`：流式进度与「停止标注」。

## 验证

- 桩：8 镜头 / 80 句预算 6560；5 镜头按 2 个一批共 3 批，第 2 批 `finish_reason=length` →
  首次结果保留第 1、3 批（镜头 1/2/5），失败项文案含「截断」与镜头范围；
  `resume=true` 只重跑第 2 批，最终 5 个镜头全部生成并清理中间文件；
- 切片：首批 prompt 不含第 5 场台词，含「参考·镜头 3」与编号规则；中间批含前后参考；
- 页面桩：请求走 `/api/ui/script/suggest/stream`，进度「第 1/3 批 · 已接收 1800 字」，
  失败批次弹确认后带 `resume=true` 重试，结束后停止按钮隐藏；
- `python -m compileall -q src`、`node --check page-script.js` 通过；未启动服务、未做浏览器实测。
