/**
 * 入口：按 hash 路由渲染页面。
 * 路由格式：
 *   #/projects
 *   #/settings[?project=<工程路径>]   全局设置（不依赖工程）
 *   #/p/<encodeURIComponent(工程路径)>/c/<章节 ID>
 *   #/p/<encodeURIComponent(工程路径)>/export/<章节 ID>
 */
import { renderProjects } from './pages/projects.js';
import { renderEditor, leaveEditor } from './pages/editor.js';
import { renderCharacters } from './pages/characters.js';
import { renderNovel } from './pages/novel.js';
import { renderVoices, leaveVoices } from './pages/voices.js';
import { renderMedia } from './pages/media.js';
import { renderTasks, leaveTasks } from './pages/tasks.js';
import { renderSettings } from './pages/settings.js';
import { renderExport, leaveExport } from './pages/export.js';
import { decodePath } from './shell.js';
import { loadUIPrefs } from './store.js';

const root = document.getElementById('app');
let currentView = '';

/** 解析当前 hash（支持 `?line=` 直达台词行） */
function parseRoute() {
  const raw = location.hash.replace(/^#/, '');
  const [body, rawQuery = ''] = raw.split('?');
  const query = new URLSearchParams(rawQuery);
  if (!body || body === '/' || body === '/projects') return { view: 'projects', scope: 'home' };
  if (body === '/projects/all') return { view: 'projects', scope: 'all' };
  // 全局设置：不依赖工程路径；?project= 只用于额外展示「当前工程」区块
  if (body === '/settings') return { view: 'settings', path: query.get('project') || '' };
  const parts = body.split('/').filter(Boolean);
  if (parts[0] === 'p' && parts[1]) {
    const path = decodePath(parts[1]);
    if (parts[2] === 'c' && parts[3]) {
      return { view: 'editor', path, chapterId: decodePath(parts[3]), lineId: query.get('line') || '' };
    }
    if (parts[2] === 'characters') return { view: 'characters', path };
    if (parts[2] === 'novel') return { view: 'novel', path };
    if (parts[2] === 'voices') return { view: 'voices', path };
    if (parts[2] === 'media') return { view: 'media', path };
    if (parts[2] === 'tasks') return { view: 'tasks', path };
    // 工程内旧地址仍落到同一个全局设置页，只是额外带上该工程的上下文
    if (parts[2] === 'settings') return { view: 'settings', path };
    if (parts[2] === 'export' && parts[3]) return { view: 'export', path, chapterId: decodePath(parts[3]) };
  }
  return { view: 'unknown' };
}

/** 路由切换 */
async function route() {
  const target = parseRoute();
  if (currentView === 'editor') leaveEditor();
  if (currentView === 'export') leaveExport();
  if (currentView === 'tasks') leaveTasks();
  if (currentView === 'voices') leaveVoices();
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
// 首次启动：没有 hash 时落到工程列表
if (!location.hash) location.hash = '#/projects';
else route();
