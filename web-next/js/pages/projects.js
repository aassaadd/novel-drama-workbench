/**
 * 工程列表页：列出本机工程、打开、新建、在访达中定位、删除。
 * 与参考的「工作区首页」对齐：卡片 + 悬浮操作 + 创建入口。
 */
import { esc, icon, toast, openDialog, closeDialog, fmtBytes } from '../dom.js';
import { getJSON, postJSON } from '../api.js';
import { state, lastProject, rememberProject } from '../store.js';
import { goChapter } from '../shell.js';

/** 标题前两个字，作为无封面工程的视觉标识 */
function glyphOf(title) {
  const chars = Array.from(String(title || '未'));
  return chars.slice(0, 2).join('');
}

/** 把后端 ISO 时间转成本地「2026-11-12 19:21」 */
function formatTime(value) {
  if (!value) return '尚未打开';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 16);
  const pad = (num) => String(num).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes()
  )}`;
}

/** 工程卡片 */
function card(item) {
  const chapters = typeof item.chapters === 'number' ? item.chapters : null;
  const words = typeof item.words === 'number' ? item.words : null;
  const recent = item.path === lastProject() ? '<span class="badge mute">上次打开</span>' : '';
  return `
    <article class="proj-card" data-project="${esc(item.path)}">
      <div class="pc-cover">
        <span class="glyph">${esc(glyphOf(item.title))}</span>
        <span class="pc-tag">${recent}</span>
        <div class="pc-act">
          <button class="btn icon sm" data-card-act="settings" title="工程设置：作品信息与制作要求">${icon('gear')}</button>
          <button class="btn icon sm" data-card-act="import" title="导入正文">${icon('arrow')}</button>
          <button class="btn icon sm" data-card-act="duplicate" title="复制工程">${icon('plus')}</button>
          <button class="btn icon sm" data-card-act="reveal" title="在访达中显示">${icon('folder')}</button>
          <button class="btn icon sm" data-card-act="delete" title="删除工程">${icon('x')}</button>
        </div>
      </div>
      <div class="pc-body">
        <div class="pc-title">${esc(item.title || '未命名工程')}</div>
        <div class="pc-path">${esc(item.path)}</div>
        <div class="pc-metrics">
          <span>章节 <b>${chapters === null ? '—' : chapters}</b></span>
          <span>字数 <b>${words === null ? '—' : words}</b></span>
          <span>占用 <b>${item.size_bytes ? fmtBytes(item.size_bytes) : '—'}</b></span>
        </div>
      </div>
      <div class="pc-foot">
        <span>${esc(item.last_opened_at ? `最近打开 ${formatTime(item.last_opened_at)}` : '尚未打开')}</span>
        <span class="right"><button class="btn sm primary" data-card-act="open">打开</button></span>
      </div>
    </article>`;
}

/** 新建工程弹窗 */
function openNewProject() {
  const body = `
    <div class="field"><label>作品标题</label><input class="input" id="np-title" placeholder="例如：长夜将明" /></div>
    <div class="field"><label>作者</label><input class="input" id="np-author" placeholder="可留空" /></div>
    <div class="field"><label>工程目录</label>
      <div style="display:flex;gap:6px">
        <input class="input" id="np-dir" placeholder="~/ndw-projects/作品名" />
        <button class="btn" id="np-pick">选择…</button>
      </div>
    </div>
    <div class="dlg-note">目录不存在会自动创建；创建后可直接导入 txt / markdown 正文。</div>`;
  const mask = openDialog({
    title: '新建工程',
    body,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="np-submit">创建并打开</button></div>`,
  });
  mask.querySelector('#np-pick').addEventListener('click', async () => {
    try {
      const picked = await postJSON('/api/ui/project/pick-dir');
      if (picked?.path) mask.querySelector('#np-dir').value = picked.path;
    } catch (error) {
      toast(error.message, 'bad');
    }
  });
  mask.querySelector('#np-submit').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const title = mask.querySelector('#np-title').value.trim();
    const author = mask.querySelector('#np-author').value.trim();
    const dir = mask.querySelector('#np-dir').value.trim();
    if (!title) return toast('请填写作品标题', 'warn');
    if (!dir) return toast('请填写或选择工程目录', 'warn');
    button.disabled = true;
    button.textContent = '创建中…';
    try {
      const result = await postJSON('/api/ui/project/init', { dir, title, author });
      closeDialog();
      rememberProject(result.root || dir);
      toast('工程已创建', 'ok');
      await loadProjects();
      // 新工程还没有章节：打开后会看到空目录，引导去原有界面导入
      const chapters = await getJSON('/api/project/chapters', { path: result.root || dir }).catch(() => []);
      if (chapters && chapters.length) goChapter(result.root || dir, chapters[0].id);
      else window.location.hash = '#/projects';
    } catch (error) {
      toast(error.message, 'bad');
      button.disabled = false;
      button.textContent = '创建并打开';
    }
  });
}

/** 删除确认 */
function confirmDelete(item) {
  const mask = openDialog({
    title: '删除工程',
    body: `<div class="dlg-note bad">工程会连同全部章节、配音、导出产物一起移入废纸篓（可从废纸篓恢复）。</div>
      <div class="summary-line"><span>${esc(item.title || '未命名工程')}</span><span class="right">${esc(item.path)}</span></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn danger" id="del-ok">移入废纸篓</button></div>`,
  });
  mask.querySelector('#del-ok').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      await postJSON('/api/project/delete', { path: item.path, confirm: true });
      closeDialog();
      toast('已移入废纸篓', 'ok');
      await loadProjects();
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
    }
  });
}

/** 导入正文：选文件或直接粘贴，按标题切章 */
function openImport(item) {
  const mask = openDialog({
    title: '导入正文',
    body: `
      <div class="field"><label>选择 txt / md 文件</label>
        <input type="file" id="imp-file" accept=".txt,.md,text/plain,text/markdown" /></div>
      <div class="field"><label>标题匹配规则</label>
        <input class="input" id="imp-pattern" value="^#{1,3}\\s+(.+)$" /></div>
      <div class="field"><label>重复章节</label>
        <select class="select" id="imp-dup">
          <option value="skip">跳过（默认）</option>
          <option value="replace">覆盖正文</option>
          <option value="new">再建一份</option>
        </select></div>
      <div class="field"><label>正文（选文件后自动填入，可直接编辑）</label>
        <textarea class="textarea" id="imp-text" rows="8" placeholder="粘贴正文，或用上面的文件选择"></textarea></div>
      <div class="hint">导入会按标题切章写入章节文件；纯文本无标题时整篇作为一章。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="imp-ok">开始导入</button></div>`,
  });
  mask.querySelector('#imp-file').addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      mask.querySelector('#imp-text').value = await file.text();
    } catch (error) {
      toast(`读取失败：${error.message}`, 'bad');
    }
  });
  mask.querySelector('#imp-ok').addEventListener('click', async (event) => {
    const text = mask.querySelector('#imp-text').value;
    if (!text.trim()) return toast('正文为空', 'warn');
    event.currentTarget.disabled = true;
    event.currentTarget.textContent = '导入中…';
    try {
      const result = await postJSON('/api/ui/chapter/import', {
        path: item.path,
        text,
        pattern: mask.querySelector('#imp-pattern').value.trim(),
        on_duplicate: mask.querySelector('#imp-dup').value,
      });
      closeDialog();
      const created = result?.created ?? result?.imported ?? result?.chapters?.length ?? 0;
      toast(created ? `已导入 ${created} 章` : '导入完成', 'ok');
      await loadProjects();
      const chapters = await getJSON('/api/project/chapters', { path: item.path }).catch(() => []);
      if (chapters?.length) {
        rememberProject(item.path);
        goChapter(item.path, chapters[0].id);
      } else {
        renderProjects(document.getElementById('app'));
      }
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
      event.currentTarget.textContent = '开始导入';
    }
  });
}

/** 复制工程 */
function openDuplicate(item) {
  const base = String(item.path).split('/').filter(Boolean).pop() || '工程';
  const mask = openDialog({
    title: '复制工程',
    body: `<div class="field"><label>副本目录名</label>
        <input class="input" id="dup-name" value="${esc(`${base}-copy`)}" /></div>
      <div class="hint">副本创建在原目录同级；章节、配音、设定与产物一并复制，创建后出现在工程列表。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="dup-ok">复制</button></div>`,
  });
  mask.querySelector('#dup-ok').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      const result = await postJSON('/api/ui/project/duplicate', {
        path: item.path,
        name: mask.querySelector('#dup-name').value.trim(),
      });
      closeDialog();
      toast(`已复制到 ${result.path}`, 'ok');
      await loadProjects();
      renderProjects(document.getElementById('app'));
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
    }
  });
}

/** 拉取工程列表（含章节/字数/占用） */
async function loadProjects() {
  const list = await getJSON('/api/ui/projects/overview');
  state.projects = list || [];
  state.projectInfos = {};
}

/** 首页筛选：状态（按配音完成度）/ 编辑时间 / 关键词，均为本地真实数据 */
const filters = { status: 'all', time: 'all', text: '', sort: 'recent' };

/** 工程状态：无台词行=未开始，全部已配=已完成，其余制作中 */
function statusOf(item) {
  const total = Number(item.total_lines || 0);
  const ready = Number(item.ready_lines || 0);
  if (!total) return 'none';
  if (ready >= total) return 'done';
  return 'doing';
}

/** 编辑时间窗：按最近写入时间过滤 */
function inTimeWindow(item, window) {
  if (window === 'all') return true;
  const value = item.updated_at;
  if (!value) return false;
  const at = new Date(value).getTime();
  if (Number.isNaN(at)) return false;
  const days = (Date.now() - at) / 86400000;
  if (window === '7') return days <= 7;
  if (window === '30') return days <= 30;
  if (window === 'older') return days > 30;
  return true;
}

/** 过滤后的工程列表（按筛选排序） */
function filteredProjects() {
  const key = filters.text.trim().toLowerCase();
  const list = state.projects.filter((item) => {
    if (filters.status !== 'all' && statusOf(item) !== filters.status) return false;
    if (!inTimeWindow(item, filters.time)) return false;
    if (!key) return true;
    return (
      String(item.title || '').toLowerCase().includes(key) ||
      String(item.path || '').toLowerCase().includes(key)
    );
  });
  const time = (item) => new Date(item.last_opened_at || item.updated_at || 0).getTime() || 0;
  const sorters = {
    recent: (a, b) => time(b) - time(a),
    size: (a, b) => Number(b.size_bytes || 0) - Number(a.size_bytes || 0),
    title: (a, b) => String(a.title || a.path || '').localeCompare(String(b.title || b.path || ''), 'zh'),
    progress: (a, b) => Number(b.ready_lines || 0) / Math.max(1, Number(b.total_lines || 0)) -
      Number(a.ready_lines || 0) / Math.max(1, Number(a.total_lines || 0)),
  };
  return list.sort(sorters[filters.sort] || sorters.recent);
}

/** 只重画卡片区（筛选后不整页重建） */
function paintCards() {
  const grid = document.getElementById('proj-cards');
  if (!grid) return;
  const list = filteredProjects();
  grid.innerHTML = list.length
    ? `${list.map(card).join('')}
       <button class="new-card" data-act="new">${icon('plus', 20)}<span>新建项目</span></button>`
    : `<div class="empty" style="grid-column:1/-1">${icon('search', 20)}
         <b>没有匹配的工程</b><span>换个筛选条件或关键词</span>
         <button class="btn" data-act="clear-filter">清除筛选</button></div>`;
}

/** 渲染工程列表页 */
/**
 * 首页左侧纵向导航（对齐参考：主页 / 任务 / AI 有声 / 项目 / 素材 / 设置）。
 * 任务、AI 有声、素材、设置都依赖「当前工程」，没有上次打开的工程时置灰并说明原因。
 * 团队 / 消息 / 积分 / 头像属于协作、会员与账号能力，本地版不适用，只标注不伪装入口。
 * @param {'home'|'all'} scope 当前页面：主页 / 项目
 * @returns {string} 导航 HTML
 */
function homeNavHTML(scope) {
  const last = lastProject();
  const enc = last ? encodeURIComponent(last) : '';
  const projectTip = last ? '' : '先打开一个工程';
  const items = [
    { key: 'home', label: '主页', icon: 'chapter', href: '#/projects', active: scope === 'home' },
    { key: 'tasks', label: '任务', icon: 'task', href: last ? `#/p/${enc}/tasks` : '', tip: projectTip },
    { key: 'studio', label: 'AI 有声', icon: 'mic', act: 'nav-studio', tip: projectTip },
    { key: 'projects', label: '项目', icon: 'folder', href: '#/projects/all', active: scope === 'all' },
    { key: 'media', label: '素材', icon: 'image', href: last ? `#/p/${enc}/media` : '', tip: projectTip },
    // 设置是全局能力，不依赖是否打开过工程
    { key: 'settings', label: '设置', icon: 'gear', href: '#/settings' },
  ];
  const muted = [
    { label: '团队', icon: 'user', tip: '团队协作属于云端能力，本地版不适用' },
    { label: '消息', icon: 'audio', tip: '站内消息属于云端能力，本地版不适用' },
    { label: '积分', icon: 'cloud', tip: '会员积分属于付费能力，本地版不适用' },
  ];
  return `
    <aside class="home-nav">
      <div class="hn-brand" title="小说改编有声剧工作台"><span class="brand-mark"></span></div>
      <nav class="hn-items">
        ${items
          .map((item) => {
            const disabled = !item.href && !item.act;
            const tag = item.href ? 'a' : 'button';
            const attrs = item.href
              ? `href="${item.href}"`
              : `type="button"${item.act ? ` data-act="${item.act}"` : ''}${disabled ? ' disabled' : ''}`;
            const tip = item.tip || item.label;
            return `<${tag} class="hn-item ${item.active ? 'active' : ''}" ${attrs} title="${esc(tip)}">
              <span class="hn-ic">${icon(item.icon, 18)}</span><span class="hn-label">${item.label}</span>
            </${tag}>`;
          })
          .join('')}
      </nav>
      <div class="hn-foot">
        ${muted
          .map(
            (item) => `<button type="button" class="hn-item muted" disabled title="${esc(item.tip)}">
              <span class="hn-ic">${icon(item.icon, 16)}</span><span class="hn-label">${item.label}</span>
            </button>`
          )
          .join('')}
        <span class="hn-avatar" title="本地版没有账号体系">${icon('user', 17)}</span>
      </div>
    </aside>`;
}

/** 工程列表页：按 scope 渲染主页 / 项目两个入口 */
export async function renderProjects(root, target = {}) {
  const scope = target.scope === 'all' ? 'all' : 'home';
  root.innerHTML = `
    <div class="home-layout">
      ${homeNavHTML(scope)}
      <div class="home-main">
        ${renderShell()}
        <main class="page" id="projects-main">
          <div class="empty">${icon('refresh', 20)}<span>正在读取本机工程…</span></div>
        </main>
      </div>
    </div>`;
  try {
    await loadProjects();
  } catch (error) {
    toast(error.message, 'bad');
  }
  const main = root.querySelector('#projects-main');
  if (!main) return;
  const totalSize = fmtBytes(state.projects.reduce((sum, item) => sum + Number(item.size_bytes || 0), 0));
  const title = scope === 'all' ? '项目' : '最近项目';
  const sub =
    scope === 'all'
      ? `本机全部 <b>${state.projects.length}</b> 个工程，占用 <b>${totalSize}</b>。`
      : `本机共 <b>${state.projects.length}</b> 个工程，占用 <b>${totalSize}</b>。打开后进入画本编辑，左侧目录可直接切章。`;
  main.innerHTML = `
    <div class="page-head">
      <div class="ph">
        <h1 class="page-title">${title}</h1>
        <div class="page-sub">${sub}</div>
      </div>
      <div class="ph-actions">
        <button class="btn" data-act="refresh" title="重新扫描默认工程目录并重建本机索引">${icon('refresh')} 刷新索引</button>
        <button class="btn primary" data-act="new">${icon('plus')} 新建项目</button>
      </div>
    </div>
    <div class="ws-filters">
      <button class="filter-pill" disabled title="来源属于云端账号，本地版不适用">全部来源</button>
      <button class="filter-pill" disabled title="项目属于团队空间，本地版不适用">全部项目</button>
      <select class="select ws-filter" id="f-status" title="按制作状态筛选">
        <option value="all">全部状态</option>
        <option value="none">未开始</option>
        <option value="doing">制作中</option>
        <option value="done">已完成</option>
      </select>
      <select class="select ws-filter" id="f-time" title="按编辑时间筛选">
        <option value="all">全部时间</option>
        <option value="7">最近 7 天</option>
        <option value="30">最近 30 天</option>
        <option value="older">30 天前</option>
      </select>
      <select class="select ws-filter" id="f-sort" title="排序方式">
        <option value="recent">按最近打开</option>
        <option value="size">按占用从大到小</option>
        <option value="progress">按完成度</option>
        <option value="title">按书名</option>
      </select>
      <div class="search" style="width:220px;margin-left:auto">
        ${icon('search', 14)}
        <input type="search" id="f-text" placeholder="搜索项目" value="${esc(filters.text)}" />
      </div>
    </div>
    <div class="cards" id="proj-cards"></div>`;

  paintCards();
  main.querySelector('#f-status').value = filters.status;
  main.querySelector('#f-time').value = filters.time;
  main.querySelector('#f-sort').value = filters.sort;
  main.querySelector('#f-status').addEventListener('change', (event) => {
    filters.status = event.target.value;
    paintCards();
  });
  main.querySelector('#f-time').addEventListener('change', (event) => {
    filters.time = event.target.value;
    paintCards();
  });
  main.querySelector('#f-sort').addEventListener('change', (event) => {
    filters.sort = event.target.value;
    paintCards();
  });
  main.querySelector('#f-text').addEventListener('input', (event) => {
    filters.text = event.target.value;
    paintCards();
  });

  main.addEventListener('click', (event) => {
    const cardEl = event.target.closest('[data-project]');
    if (cardEl) {
      const item = state.projects.find((project) => project.path === cardEl.dataset.project);
      const action = event.target.closest('[data-card-act]')?.dataset.cardAct || 'open';
      if (!item) return;
      if (action === 'open') {
        rememberProject(item.path);
        getJSON('/api/project/chapters', { path: item.path })
          .then((chapters) => {
            if (chapters && chapters.length) goChapter(item.path, chapters[0].id);
            else toast('这个工程还没有章节，先导入正文', 'warn');
          })
          .catch((error) => toast(error.message, 'bad'));
        return;
      }
      if (action === 'settings') {
        rememberProject(item.path);
        window.location.hash = `#/p/${encodeURIComponent(item.path)}/settings`;
        return;
      }
      if (action === 'import') {
        openImport(item);
        return;
      }
      if (action === 'duplicate') {
        openDuplicate(item);
        return;
      }
      if (action === 'reveal') {
        postJSON('/api/project/reveal', { path: item.path })
          .then(() => toast('已在访达中打开', 'ok'))
          .catch((error) => toast(error.message, 'bad'));
        return;
      }
      if (action === 'delete') confirmDelete(item);
      return;
    }
    const button = event.target.closest('[data-act]');
    if (!button) return;
    if (button.dataset.act === 'new') openNewProject();
    if (button.dataset.act === 'clear-filter') {
      filters.status = 'all';
      filters.time = 'all';
      filters.text = '';
      document.getElementById('f-status').value = 'all';
      document.getElementById('f-time').value = 'all';
      document.getElementById('f-text').value = '';
      paintCards();
    }
    if (button.dataset.act === 'refresh') {
      button.disabled = true;
      postJSON('/api/ui/projects/reindex', {})
        .then(async (result) => {
          await loadProjects();
          paintCards();
          toast(`索引已刷新：${Number(result?.count || state.projects.length)} 个工程`, 'ok');
        })
        .catch((error) => toast(`刷新索引失败：${error.message}`, 'bad'))
        .finally(() => {
          button.disabled = false;
        });
    }
  });

  // 左侧导航：AI 有声需要先解析上次工程的章节，不能只靠 hash
  root.querySelector('.home-nav')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-act="nav-studio"]');
    if (!button) return;
    const last = lastProject();
    if (!last) return toast('先打开一个工程', 'warn');
    getJSON('/api/project/chapters', { path: last })
      .then((chapters) => {
        if (chapters && chapters.length) goChapter(last, chapters[0].id);
        else toast('这个工程还没有章节，先导入正文', 'warn');
      })
      .catch((error) => toast(error.message, 'bad'));
  });
}

/** 工程列表页顶栏 */
function renderShell() {
  return `
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark"></span>
        <span>小说改编有声剧工作台<small>本地版 · NEW</small></span>
      </div>
      <div class="tb-right">
        <span class="save-state">${icon('audio', 13)} 本机推理 · 数据不出盘</span>
      </div>
    </header>`;
}

/** 卡片上的体积信息（后续给卡片底部用，当前保留避免未使用告警） */
export { fmtBytes };
