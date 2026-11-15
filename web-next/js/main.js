/**
 * 入口：按 hash 路由渲染页面。
 * 路由格式：
 *   #/projects
 *   #/settings[?project=<工程路径>]   全局设置（不依赖工程）
 *   #/logs                            全平台日志（操作 + LLM 调用）
 *   #/p/<encodeURIComponent(工程路径)>/c/<章节 ID>
 *   #/p/<encodeURIComponent(工程路径)>/export/<章节 ID>
 */
// 全站统一轻提示：接管 title / data-tip，替代浏览器原生提示
import './tooltip.js';
import { renderProjects } from './pages/projects.js';
import { renderEditor, leaveEditor } from './pages/editor.js';
import { renderCharacters } from './pages/characters.js';
import { renderNovel } from './pages/novel.js';
import { renderVoices, leaveVoices } from './pages/voices.js';
import { renderMedia } from './pages/media.js';
import { renderTasks, leaveTasks } from './pages/tasks.js';
import { renderSettings } from './pages/settings.js';
import { renderLogs, leaveLogs } from './pages/logs.js';
import { renderProjectSettings } from './pages/project-settings.js';
import { renderExport, leaveExport } from './pages/export.js';
import { decodePath } from './shell.js';
import { bindEnumWarnings } from './enums.js';
import { loadUIPrefs } from './store.js';
import { postJSON } from './api.js';
import { esc } from './dom.js';

const root = document.getElementById('app');
let currentView = '';
/** 每个工程只自检一次（页面存活期内），避免每次路由跳转都写盘 */
const selfchecked = new Set();

/**
 * 打开工程自检：迁移旧内嵌场景、补台本、清点孤儿行。
 * 有改动或孤儿行时在页面顶部固定展示一条可关闭的提示，孤儿行列出具体条目。
 * @param {string} path 工程目录
 */
async function selfcheck(path) {
  if (!path || selfchecked.has(path)) return;
  selfchecked.add(path);
  let report;
  try {
    report = await postJSON('/api/ui/project/selfcheck', { path });
  } catch (error) {
    // 自检失败不阻塞页面使用，只在控制台记一笔
    console.warn('工程自检失败：', error?.message || error);
    return;
  }
  if (!report || report.skipped) return;
  const parts = [];
  if (report.migrated) parts.push(`迁移旧场景 ${report.migrated} 章`);
  if (report.scripts_created) parts.push(`补出台本 ${report.scripts_created} 章`);
  if (report.orphan_count) parts.push(`孤儿台词行 ${report.orphan_count} 条（已不参与渲染）`);
  if (!parts.length) return;
  const orphans = (report.orphans || []).slice(0, 8);
  const listHTML = orphans.length
    ? `<ul style="margin:6px 0 0;padding-left:18px">${orphans
        .map(
          (item) =>
            `<li>${esc(item.chapter_title || item.chapter_id)} · 场景 ${Number(item.scene_index || 0) + 1} · ${esc(
              item.line_id || ''
            )} · ${esc(item.text || '')}</li>`
        )
        .join('')}</ul>${report.orphan_count > orphans.length ? `<div>另有 ${report.orphan_count - orphans.length} 条未列出</div>` : ''}`
    : '';
  const node = document.createElement('div');
  node.setAttribute('role', 'status');
  node.style.cssText =
    'position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:9999;max-width:min(820px,94vw);' +
    'padding:10px 14px;border:1px solid #d7a23a;border-radius:8px;background:#fff8e6;color:#7a5200;' +
    'font-size:13px;line-height:1.6;box-shadow:0 6px 20px rgba(0,0,0,.12)';
  node.innerHTML = `<div style="display:flex;gap:12px;align-items:flex-start">
      <div style="flex:1"><b>工程自检</b>：${esc(parts.join('，'))}${listHTML}</div>
      <button type="button" style="border:0;background:transparent;color:#7a5200;cursor:pointer;font-size:13px">知道了</button>
    </div>`;
  node.querySelector('button').addEventListener('click', () => node.remove());
  document.body.appendChild(node);
}

/** 解析当前 hash（支持 `?line=` 直达台词行） */
function parseRoute() {
  const raw = location.hash.replace(/^#/, '');
  const [body, rawQuery = ''] = raw.split('?');
  const query = new URLSearchParams(rawQuery);
  if (!body || body === '/' || body === '/projects') return { view: 'projects' };
  // 全局设置：与任何工程无关，不再接受 ?project= 参数；
  // focus 用于从生成弹窗跳转后聚焦具体设置项
  if (body === '/settings')
    return { view: 'settings', section: query.get('section') || '', focus: query.get('focus') || '' };
  // 全平台日志：与工程无关，筛选条件在页面内维护
  if (body === '/logs') return { view: 'logs', trace: query.get('trace') || '' };
  const parts = body.split('/').filter(Boolean);
  if (parts[0] === 'p' && parts[1]) {
    const path = decodePath(parts[1]);
    if (parts[2] === 'c' && parts[3]) {
      return {
        view: 'editor',
        path,
        chapterId: decodePath(parts[3]),
        lineId: query.get('line') || '',
        act: query.get('act') || '',
        // 工作室一级视图（画本 / 录音 / 后期 / 对轨），与页面名 view 区分
        studioView: query.get('view') || '',
      };
    }
    if (parts[2] === 'characters') return { view: 'characters', path, character: query.get('character') || '' };
    if (parts[2] === 'novel')
      return { view: 'novel', path, chapter: query.get('chapter') || '' };
    if (parts[2] === 'voices') return { view: 'voices', path };
    if (parts[2] === 'media') return { view: 'media', path };
    if (parts[2] === 'tasks') return { view: 'tasks', path };
    if (parts[2] === 'settings') return { view: 'project-settings', path };
    if (parts[2] === 'export' && parts[3]) return { view: 'export', path, chapterId: decodePath(parts[3]) };
  }
  return { view: 'unknown' };
}

/** 路由切换 */
async function route() {
  const target = parseRoute();
  // 自检结果挂在 body 上，不受页面重绘影响；同一工程只跑一次
  if (target.path) selfcheck(target.path);
  if (currentView === 'editor') leaveEditor();
  if (currentView === 'export') leaveExport();
  if (currentView === 'tasks') leaveTasks();
  if (currentView === 'voices') leaveVoices();
  if (currentView === 'logs') leaveLogs();
  currentView = target.view;
  root.innerHTML = '';
  if (target.view === 'projects') {
    await renderProjects(root, target);
    return;
  }
  if (target.view === 'editor') {
    await renderEditor(root, target);
    return;
  }
  if (target.view === 'characters') {
    await renderCharacters(root, target);
    return;
  }
  if (target.view === 'novel') {
    await renderNovel(root, target);
    return;
  }
  if (target.view === 'voices') {
    await renderVoices(root, target);
    return;
  }
  if (target.view === 'media') {
    await renderMedia(root, target);
    return;
  }
  if (target.view === 'tasks') {
    await renderTasks(root, target);
    return;
  }
  if (target.view === 'settings') {
    await renderSettings(root, target);
    return;
  }
  if (target.view === 'logs') {
    await renderLogs(root, target);
    return;
  }
  if (target.view === 'project-settings') {
    await renderProjectSettings(root, target);
    return;
  }
  if (target.view === 'export') {
    await renderExport(root, target);
    return;
  }
  root.innerHTML = `
    <div class="page"><div class="empty"><b>页面不存在</b>
      <span>地址可能已失效</span>
      <a class="btn" href="#/projects" style="margin-top:10px">回工程列表</a>
    </div></div>`;
}

window.addEventListener('hashchange', route);
// 启动前恢复全局界面偏好（与工程无关，供画本编辑直接使用）
loadUIPrefs();
bindEnumWarnings();
// 首次启动：没有 hash 时落到工程列表
if (!location.hash) location.hash = '#/projects';
else route();
