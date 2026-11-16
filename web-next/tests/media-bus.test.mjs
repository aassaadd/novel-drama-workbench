/**
 * 媒体总线纯函数单测（Node 20+ 内置 node:test）。
 *
 * 只测不依赖 DOM 的展示层：状态 / 通道标签、进度与错误归一、计划与任务卡片 HTML。
 * 运行：node --test web-next/tests/media-bus.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MEDIA_STATUS,
  MEDIA_CHANNELS,
  mediaStatusLabel,
  mediaChannelLabel,
  jobProgressText,
  jobErrorSummary,
  mediaPlanHTML,
  mediaJobCardHTML,
  mediaJobRowsHTML,
} from '../js/media-format.js';

test('MEDIA_STATUS 覆盖全部终态且均为中文文案', () => {
  const expected = {
    queued: '排队',
    running: '进行中',
    succeeded: '已完成',
    partial: '部分完成',
    failed: '失败',
    canceled: '已取消',
    interrupted: '已中断',
  };
  assert.deepEqual(MEDIA_STATUS, expected);
  for (const [status, label] of Object.entries(expected)) {
    assert.equal(mediaStatusLabel(status), label);
  }
  assert.equal(mediaStatusLabel('unknown'), '状态未知');
  assert.equal(mediaStatusLabel(undefined), '状态未知');
});

test('MEDIA_CHANNELS 与通道文案：未识别通道归入「其他」', () => {
  assert.deepEqual(MEDIA_CHANNELS, {
    audio: '声音',
    image: '图片',
    video: '视频',
    llm: '模型',
    other: '其他',
  });
  assert.equal(mediaChannelLabel('audio'), '声音');
  assert.equal(mediaChannelLabel('image'), '图片');
  assert.equal(mediaChannelLabel('video'), '视频');
  assert.equal(mediaChannelLabel('llm'), '模型');
  assert.equal(mediaChannelLabel('mystery'), '其他');
  assert.equal(mediaChannelLabel(''), '其他');
});

test('jobProgressText：百分比 + 当前项 / 阶段 / 空值', () => {
  assert.equal(jobProgressText({ percent: 42.4, current: '第 3 行' }), '42% · 第 3 行');
  assert.equal(jobProgressText({ percent: 100 }), '100%');
  assert.equal(jobProgressText({ percent: null, stage: '合成语音' }), '合成语音');
  assert.equal(jobProgressText({ percent: null, current: '载入中' }), '载入中');
  // 兼容从 progress 子对象读取百分比
  assert.equal(jobProgressText({ progress: { percent: 7, current: '第 1 章' } }), '7% · 第 1 章');
  assert.equal(jobProgressText({}), '—');
});

test('jobErrorSummary：最多两行，兼容字符串与对象', () => {
  const job = { error: '第一行错误\n第二行细节\n第三行不该出现' };
  assert.equal(jobErrorSummary(job), '第一行错误 / 第二行细节');
  assert.equal(jobErrorSummary({ error: { message: '模型未就绪' } }), '模型未就绪');
  assert.equal(jobErrorSummary({ error: { reason: '超时' } }), '超时');
  assert.equal(jobErrorSummary({}), '');
});

test('mediaJobCardHTML：状态文案、通道、产物数与取消按钮', () => {
  const running = mediaJobCardHTML({
    id: 'job_1',
    title: '本章批量配音',
    kind: 'tts.chapter',
    channel: 'audio',
    status: 'running',
    percent: 60,
    current: '第 6/10 行',
    artifacts: ['a.mp3', 'b.mp3'],
    cancelable: true,
  });
  assert.match(running, /本章批量配音/);
  assert.match(running, /声音/);
  assert.match(running, /进行中/);
  assert.match(running, /产物 2/);
  assert.match(running, /60% · 第 6\/10 行/);
  assert.match(running, /data-job="job_1"/);
  assert.match(running, /data-media-cancel="job_1"/);

  const done = mediaJobCardHTML({
    id: 'job_2',
    kind: 'image',
    channel: 'image',
    status: 'partial',
    percent: 100,
    cancelable: true,
  });
  assert.match(done, /配图/); // title 缺失时用 kind 中文兜底
  assert.match(done, /图片/);
  assert.match(done, /部分完成/);
  assert.ok(!done.includes('data-media-cancel'), '已结束的任务不渲染取消按钮');

  const locked = mediaJobCardHTML({ id: 'job_3', kind: 'video', channel: 'video', status: 'running', cancelable: false });
  assert.match(locked, /视频/);
  assert.ok(!locked.includes('data-media-cancel'), '不可取消的任务不渲染取消按钮');
});

test('mediaJobCardHTML：错误摘要与状态未知兜底', () => {
  const html = mediaJobCardHTML({
    id: 'job_4',
    title: '配乐',
    channel: 'audio',
    status: 'failed',
    error: 'provider 超时\n请稍后重试\n第三行',
  });
  assert.match(html, /失败/);
  assert.match(html, /provider 超时 \/ 请稍后重试/);
  assert.ok(!html.includes('第三行'));

  const unknown = mediaJobCardHTML({ id: 'job_5', channel: 'audio', status: 'weird' });
  assert.match(unknown, /状态未知/);
  assert.match(unknown, /媒体任务/);
});

test('mediaPlanHTML：count 为 null 时显示「视范围而定」，并展示计划事实', () => {
  const html = mediaPlanHTML({
    title: '全书批量配音',
    channel: 'audio',
    channel_label: '声音',
    stages: ['载入', '逐章筛选', '逐行合成', '汇总结果'],
    count: null,
    gpu: false,
    cancelable: true,
    usage: 'path',
    cancel_text: '可随时取消',
  });
  assert.match(html, /这次做什么/);
  assert.match(html, /全书批量配音/);
  assert.match(html, /声音/);
  assert.match(html, /载入 → 逐章筛选 → 逐行合成 → 汇总结果/);
  assert.match(html, /视范围而定/);
  assert.match(html, /不排队，立即执行/);
  assert.match(html, /可随时取消/);
  assert.match(html, /path/);
  assert.ok(!html.includes('null'), 'count 为 null 时不能把原值渲染出来');
});

test('mediaPlanHTML：有数量 / 需要排队 / 不可取消', () => {
  const html = mediaPlanHTML({
    title: '图生视频',
    channel: 'video',
    stages: ['排队', '生成视频'],
    count: 3,
    queue: true,
    cancelable: false,
    usage: 'path、chapter',
  });
  assert.match(html, /3 个/);
  assert.match(html, /需要排队/);
  assert.match(html, /不可取消/);
  assert.match(html, /视频/);
});

test('mediaJobRowsHTML：运行中优先，其余按创建时间倒序', () => {
  const jobs = [
    { id: 'done_old', title: '旧任务', status: 'succeeded', created_at: '2026-01-01T00:00:00Z' },
    { id: 'run_new', title: '运行中', status: 'running', created_at: '2026-01-02T00:00:00Z' },
    { id: 'done_new', title: '新任务', status: 'failed', created_at: '2026-01-03T00:00:00Z' },
    { id: 'queued', title: '排队中', status: 'queued', created_at: '2026-01-01T12:00:00Z' },
  ];
  const html = mediaJobRowsHTML(jobs);
  const order = ['run_new', 'queued', 'done_new', 'done_old'].map((id) => html.indexOf(`data-job="${id}"`));
  assert.ok(order.every((index) => index >= 0), '所有任务都要渲染');
  assert.deepEqual([...order].sort((left, right) => left - right), order, '顺序应为：运行中 → 排队 → 其余倒序');
});

test('mediaJobRowsHTML：空列表给出中文空态', () => {
  assert.match(mediaJobRowsHTML([]), /暂无媒体任务/);
  assert.match(mediaJobRowsHTML(null, { emptyText: '本工程还没有媒体任务' }), /本工程还没有媒体任务/);
});
