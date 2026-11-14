/**
 * 导出页：交付包 zip、剪映草稿 zip、导出历史。
 * 只调用现有 /api/ui/exports、/api/ui/export/*，不在前端拼产物。
 */
import { esc, icon, toast, fmtBytes, openDialog, closeDialog } from '../dom.js';
import { getJSON, postJSON, fileUrl } from '../api.js';
import { state, resetProject } from '../store.js';
import {
  railbar,
  topbar,
  chapterCtx,
  goChapter,
  goProjects,
  emptyProjectHTML,
  openSwitchProject,
} from '../shell.js';

let bound = false;
let exporting = '';
/** 导出前检查结果（缺配音 / 音色编排 / 时间轴） */
let preflight = null;
let lastResult = null;

/** 当前章节 */
function currentChapter() {
  return state.chapters.find((item) => item.id === state.chapterId) || null;
}

/** 导出记录行 */
function fileRows(files) {
  if (!files.length) {
    return `<tr><td colspan="4" style="color:var(--text-4)">还没有导出记录</td></tr>`;
  }
  return files
    .slice()
    .reverse()
    .map(
      (item) => `<tr>
        <td><a href="${fileUrl(item.rel, state.path)}" target="_blank" rel="noopener">${esc(item.rel.split('/').pop())}</a></td>
        <td class="mono">${esc(item.rel)}</td>
        <td class="mono">${fmtBytes(item.size)}</td>
        <td class="mono">${esc(item.mtime)}</td>
      </tr>`
    )
    .join('');
}

/** 主内容 */
function exportHTML(files) {
  const chapter = currentChapter();
  const ready = state.lines.filter((row) => row.state === 'ready' && row.audio).length;
  const chapterOnly = files.filter((item) => item.rel.includes('exports'));
  return `
    <div class="export-page">
      <div class="page-head">
        <div class="ph">
          <div class="eyebrow"><span class="ln"></span>导出</div>
          <h1 class="page-title">${esc(chapter?.title || '导出')}</h1>
          <div class="page-sub">已配 <b>${ready}</b> / ${state.lines.length} 行 · 导出会先重建时间轴，再打包本章产物。</div>
        </div>
        <div class="ph-actions">
          <button class="btn" data-act="refresh">${icon('refresh')} 刷新记录</button>
        </div>
      </div>
      <div class="exp-grid">
        <div style="display:flex;flex-direction:column;gap:14px;min-width:0">
          <div class="card">
            <div class="card-head"><span class="ct">导出方式</span></div>
            <div class="card-body">
              <div class="exp-opt">
                <div class="ico">${icon('export', 18)}</div>
                <div class="body">
                  <div class="t">本章交付包（zip）</div>
                  <div class="d">台词脚本 + SRT 字幕 + 交付清单 + 已配好的逐行音频；有混音轨与成片时一并收进 zip。</div>
                </div>
                <div class="act">
                  <button class="btn primary" data-act="export-bundle" ${exporting === 'bundle' ? 'disabled' : ''}>
                    ${exporting === 'bundle' ? '<span class="spin"></span> 打包中…' : '生成 zip'}
                  </button>
                </div>
              </div>
              <div class="exp-opt">
                <div class="ico">${icon('audio', 18)}</div>
                <div class="body">
                  <div class="t">剪映草稿（zip）</div>
                  <div class="d">按时间轴生成可继续剪辑的草稿，包含配音、音效、字幕与素材对账。</div>
                </div>
                <div class="act">
                  <button class="btn" data-act="export-jianying" ${exporting === 'jianying' ? 'disabled' : ''}>
                    ${exporting === 'jianying' ? '<span class="spin"></span> 导出中…' : '导出草稿'}
                  </button>
                </div>
              </div>
              ${
                lastResult
                  ? `<div class="summary-line" style="border-color:rgba(79,191,139,.4)">
                       <span>${icon('check', 14)} ${esc(lastResult.name || '已生成')}</span>
                       <span class="right">
                         <a class="btn sm primary" href="${fileUrl(lastResult.rel, state.path)}" target="_blank" rel="noopener">下载 zip</a>
                       </span>
                     </div>
                     ${
                       (lastResult.missing || []).length
                         ? `<div class="dlg-note warn">未包含：<br />${lastResult.missing.map((item) => `· ${esc(item)}`).join('<br />')}</div>`
                         : ''
                     }
                     ${
                       (lastResult.entries || []).length
                         ? `<div class="dlg-note">zip 内容：${lastResult.entries.map((item) => esc(item)).join('、')}</div>`
                         : ''
                     }`
                  : ''
              }
            </div>
          </div>
          <div class="card">
            <div class="card-head"><span class="ct">导出记录</span>
              <div class="right"><span class="badge mute">${chapterOnly.length} 个文件</span></div>
            </div>
            <div class="card-body" style="padding:0">
              <table class="files">
                <thead><tr><th>文件</th><th>位置</th><th>体积</th><th>生成时间</th></tr></thead>
                <tbody>${fileRows(files)}</tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-head"><span class="ct">导出前检查</span>
            <div class="right"><span class="badge ${preflight && (preflight.missing_total || !preflight.ok) ? 'warn' : 'ok'}">${
              preflight ? (preflight.missing_total || !preflight.ok ? '有问题' : '通过') : '检查中'
            }</span></div>
          </div>
          <div class="card-body">
            <div class="summary-line"><span>已配台词</span><span class="right">${preflight ? preflight.ready : ready} / ${
              preflight ? preflight.lines_total : state.lines.length
            } 行</span></div>
            ${
              preflight?.issues?.length
                ? `<div class="dlg-note ${preflight.ok ? 'warn' : 'bad'}">${preflight.issues
                    .slice(0, 4)
                    .map((item) => `· ${esc(item.message)}`)
                    .join('<br />')}</div>`
                : `<div class="hint">没有发现缺音频或音色编排问题，可以直接导出。</div>`
            }
            ${
              preflight?.missing?.length
                ? `<details style="margin-top:6px"><summary class="hint">缺配音明细（前 ${
                    preflight.missing.length
                  } 行）</summary>
                     ${preflight.missing
                       .slice(0, 8)
                       .map(
                         (item) =>
                           `<div class="imp-row"><span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(
                             item.speaker || ''
                           )} · ${esc((item.text || '').slice(0, 34))}</span></div>`
                       )
                       .join('')}
                   </details>`
                : ''
            }
            <div class="hint">
              未配台词不会中断导出，但 zip 里对应行没有音频；确认忽略后仍可导出。<br />
              需要完整成片时，先回到编辑页「补齐缺失配音」，再执行混音 / 混流。
            </div>
            <button class="btn" data-act="back-editor" style="margin-top:4px">${icon('back')} 回编辑页</button>
          </div>
        </div>
      </div>
    </div>`;
}

/** 读取导出记录 */
async function loadFiles() {
  const payload = await getJSON('/api/ui/exports', { path: state.path });
  return payload?.files || [];
}

/** 读取导出前检查 */
async function loadPreflight() {
  preflight = await getJSON('/api/ui/export/preflight', { path: state.path, chapter: state.chapterId });
  return preflight;
}

/**
 * 导出前检查有问题时弹出红色确认；确认后带 ignore_issues 再导出。
 * @param {object} check 预检结果
 * @param {Function} run 确认后的执行函数（参数为 ignoreIssues）
 */
function confirmIgnore(check, run) {
  const issues = check?.issues || [];
  const mask = openDialog({
    title: '导出前检查未通过',
    body: `<div class="dlg-note bad">确认忽略后仍会导出；缺音频的行在成品里会是静音/空缺。</div>
      <div class="summary-line"><span>缺配音</span><span class="right" style="color:var(--warn)">${Number(
        check?.missing_total || 0
      )} 行</span></div>
      ${
        issues.length
          ? `<div class="impact">${issues
              .slice(0, 8)
              .map(
                (item) =>
                  `<div class="imp-row"><span style="min-width:0">${esc(item.message || '')}</span>
                   <span class="badge ${item.level === 'error' ? 'bad' : 'warn'}">${
                     item.level === 'error' ? '错误' : '提示'
                   }</span></div>`
              )
              .join('')}</div>`
          : ''
      }`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn danger" id="pf-ok">仍然导出</button></div>`,
  });
  mask.querySelector('#pf-ok').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    closeDialog();
    await run(true);
  });
}

/** 导出交付包 */
async function exportBundle(ignoreIssues = false) {
  exporting = 'bundle';
  repaint([]);
  try {
    if (!ignoreIssues) {
      const check = await loadPreflight();
      if (check?.missing_total || !check?.ok) {
        exporting = '';
        repaint([]);
        confirmIgnore(check, (ignore) => exportBundle(ignore));
        return;
      }
    }
    // 打包可能涉及转码，耗时较长：不设前端超时
    const result = await postJSON(
      '/api/ui/export/bundle',
      {
        path: state.path,
        chapter: state.chapterId,
        ignore_issues: ignoreIssues,
      },
      { timeout: 0 }
    );
    lastResult = result;
    toast('交付包已生成', 'ok');
    const files = await loadFiles();
    await loadPreflight().catch(() => null);
    exporting = '';
    repaint(files);
  } catch (error) {
    exporting = '';
    toast(error.message, 'bad');
    const files = await loadFiles().catch(() => []);
    repaint(files);
  }
}

/** 导出剪映草稿 */
async function exportJianying() {
  exporting = 'jianying';
  repaint([]);
  try {
    const result = await postJSON(
      '/api/ui/export/jianying',
      {
        path: state.path,
        chapter: state.chapterId,
      },
      { timeout: 0 }
    );
    const zipPath = result?.zip || '';
    lastResult = zipPath ? { name: String(zipPath).split('/').pop(), rel: String(zipPath).split('/exports/').pop() } : null;
    if (lastResult && String(zipPath).includes('/exports/')) {
      lastResult.rel = `exports/${String(zipPath).split('/exports/').pop()}`;
    }
    toast('剪映草稿已导出', 'ok');
    const files = await loadFiles();
    exporting = '';
    repaint(files);
  } catch (error) {
    exporting = '';
    toast(error.message, 'bad');
    const files = await loadFiles().catch(() => []);
    repaint(files);
  }
}

/** 重画主区 */
function repaint(files) {
  const main = document.getElementById('export-main');
  if (!main) return;
  main.innerHTML = exportHTML(files);
}

async function onRootClick(event) {
  // 监听绑在共享根节点上：离开导出页后不再处理，避免抢走其它页面的同名操作
  if (!document.getElementById('export-main')) return;
  const chapterRow = event.target.closest('[data-chapter]');
  if (chapterRow) return goChapter(state.path, chapterRow.dataset.chapter);
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const act = button.dataset.act;
  if (act === 'back') return goProjects();
  if (act === 'back-editor') return goChapter(state.path, state.chapterId);
  if (act === 'switch-project') return openSwitchProject().catch((error) => toast(error.message, 'bad'));
  if (act === 'export-bundle') return exportBundle();
  if (act === 'export-jianying') return exportJianying();
  if (act === 'refresh') {
    const [files] = await Promise.all([
      loadFiles().catch((error) => {
        toast(error.message, 'bad');
        return [];
      }),
      loadPreflight().catch(() => null),
    ]);
    repaint(files);
  }
}

/** 进入导出页 */
export async function renderExport(root, { path, chapterId }) {
  if (state.path !== path) resetProject(path);
  if (!bound) {
    root.addEventListener('click', onRootClick);
    bound = true;
  }
  root.innerHTML = `
    ${topbar({ left: '', crumbs: '<span class="cur">正在加载导出信息…</span>', right: '' })}
    <div class="page"><div class="empty">${icon('refresh', 20)}<span>正在读取章节与导出记录…</span></div></div>`;
  try {
    const [info, chapters, characters] = await Promise.all([
      getJSON('/api/project/info', { path }),
      getJSON('/api/ui/chapters', { path }),
      getJSON('/api/ui/characters', { path }),
    ]);
    state.info = info;
    state.chapters = chapters || [];
    state.characters = (characters?.characters || []).map((item) => ({ id: item.id, name: item.name }));
    state.chapterId = state.chapters.some((item) => item.id === chapterId)
      ? chapterId
      : state.chapters[0]?.id || '';
    if (!state.chapterId) {
      // 空工程：导出页保留导航骨架，主区引导先起稿或导入正文
      root.innerHTML = `
        ${topbar({
          left: `<button class="btn icon" data-act="back" title="回工程列表">${icon('back')}</button>
                 <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">导出<small>本地版 · NEW</small></span></div>`,
          crumbs: `<a href="#/projects">${esc(state.info?.work?.title || '工程')}</a>
                   <span class="sep">/</span><span class="cur">导出</span>`,
          right: '',
        })}
        <div class="studio no-right">
          ${railbar('export', { chapters: 0 })}
          ${chapterCtx({ search: state.chapterSearch })}
          <div class="page" style="min-width:0;overflow:auto">${emptyProjectHTML(path)}</div>
        </div>`;
      return;
    }
    const [payload, files, check] = await Promise.all([
      getJSON('/api/ui/lines', { path, chapter: state.chapterId }),
      loadFiles(),
      loadPreflight().catch(() => null),
    ]);
    preflight = check;
    state.lines = payload?.lines || [];
    state.counts = payload?.counts || {};
    const chapter = currentChapter();
    root.innerHTML = `
      ${topbar({
        left: `<button class="btn icon" data-act="back" title="回工程列表">${icon('back')}</button>
               <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">导出<small>本地版 · NEW</small></span></div>`,
        crumbs: `<a href="#/projects">${esc(state.info?.work?.title || '工程')}</a>
                 <span class="sep">/</span>
                 <a href="#/p/${encodeURIComponent(path)}/c/${encodeURIComponent(
          state.chapterId
        )}">${esc(chapter?.title || '')}</a>
                 <span class="sep">/</span><span class="cur">导出</span>`,
        right: `<button class="btn icon" data-act="switch-project" title="切换工程">${icon('refresh')}</button>`,
      })}
      <div class="studio no-right">
        ${railbar('export', { chapters: state.chapters.length, ready: state.lines.filter((row) => row.state === 'ready').length, total: state.lines.length })}
        ${chapterCtx({ search: state.chapterSearch })}
        <div id="export-main" style="min-width:0;overflow:hidden;display:flex">${exportHTML(files)}</div>
      </div>`;
  } catch (error) {
    root.innerHTML = `<div class="page"><div class="empty">${icon('warn', 22)}<b>无法读取导出信息</b><span>${esc(
      error.message
    )}</span></div></div>`;
  }
}

/** 离开导出页 */
export function leaveExport() {}
