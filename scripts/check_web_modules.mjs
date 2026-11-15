#!/usr/bin/env node
/**
 * 前端脚本防复发检查。
 *
 * 1. 把 `web-next/js` 下所有 `.js` 复制为临时 `.mjs`，按 ES module 模式执行
 *    `node --check`。直接检查 `.js` 会被当成脚本，`await` 被当作标识符，
 *    非 async 回调里的 `await` 会漏报。
 * 2. 扫描 `addEventListener` / `forEach` / `map` / `then` / `setTimeout` 等
 *    非 async 回调，若回调体内出现 `await` 直接报错。
 *
 * 用法：node scripts/check_web_modules.mjs
 * 退出码：0 全部通过；1 有语法错误或可疑 await。
 */
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const WEB_JS = join(ROOT, 'web-next', 'js');

/** 递归收集 .js 文件 */
function collect(dir) {
  const rows = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) rows.push(...collect(full));
    else if (name.endsWith('.js')) rows.push(full);
  }
  return rows.sort();
}

/**
 * 从 `{` 开始做括号配对，跳过字符串、模板字符串与注释。
 * @param {string} text 文件内容
 * @param {number} openIndex `{` 的下标
 * @returns {number} 配对 `}` 的下标；找不到返回 -1
 */
function matchBrace(text, openIndex) {
  let depth = 0;
  let i = openIndex;
  let quote = '';
  let lineComment = false;
  let blockComment = false;
  while (i < text.length) {
    const c = text[i];
    const n = text[i + 1];
    if (lineComment) {
      if (c === '\n') lineComment = false;
      i += 1;
      continue;
    }
    if (blockComment) {
      if (c === '*' && n === '/') {
        blockComment = false;
        i += 2;
      } else i += 1;
      continue;
    }
    if (quote) {
      if (c === '\\') {
        i += 2;
        continue;
      }
      if (c === quote) quote = '';
      i += 1;
      continue;
    }
    if (c === '/' && n === '/') {
      lineComment = true;
      i += 2;
      continue;
    }
    if (c === '/' && n === '*') {
      blockComment = true;
      i += 2;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') {
      quote = c;
      i += 1;
      continue;
    }
    if (c === '{') depth += 1;
    else if (c === '}') {
      depth -= 1;
      if (depth === 0) return i;
    }
    i += 1;
  }
  return -1;
}

/** 非 async 回调里带 await 的可疑写法 */
const CALLBACK_PATTERNS = [
  /\.addEventListener\(\s*[^,]+,\s*(?!async\b)(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{/g,
  /\.(?:forEach|map|filter|reduce|reduceRight|some|every|find|findIndex|findLast|findLastIndex|flatMap|sort)\(\s*(?!async\b)(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{/g,
  /\.then\(\s*(?!async\b)(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{/g,
  /(?:setTimeout|setInterval)\(\s*(?!async\b)(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{/g,
];

/**
 * 判断一段回调体里是否存在「不属于任何嵌套 async 函数」的 await。
 * 外层回调本身已确认不是 async，所以只要 await 落在嵌套 async 函数体内就是合法的
 * （常见写法：`el.addEventListener('click', () => { btn.addEventListener('click', async () => { await x(); }) })`）。
 * @param {string} body 回调体（不含最外层花括号）
 * @returns {boolean} true 表示存在可疑 await
 */
function hasLooseAwait(body) {
  let i = 0;
  let quote = '';
  let lineComment = false;
  let blockComment = false;
  let pendingAsync = false;
  while (i < body.length) {
    const c = body[i];
    const n = body[i + 1];
    if (lineComment) {
      if (c === '\n') lineComment = false;
      i += 1;
      continue;
    }
    if (blockComment) {
      if (c === '*' && n === '/') {
        blockComment = false;
        i += 2;
      } else i += 1;
      continue;
    }
    if (quote) {
      if (c === '\\') {
        i += 2;
        continue;
      }
      if (c === quote) quote = '';
      i += 1;
      continue;
    }
    if (c === '/' && n === '/') {
      lineComment = true;
      i += 2;
      continue;
    }
    if (c === '/' && n === '*') {
      blockComment = true;
      i += 2;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') {
      quote = c;
      i += 1;
      continue;
    }
    const prev = body[i - 1] || '';
    const boundary = !/[\w$]/.test(prev);
    if (boundary && body.startsWith('async', i) && !/[\w$]/.test(body[i + 5] || '')) {
      pendingAsync = true;
      i += 5;
      continue;
    }
    if (pendingAsync && c === '{') {
      // 整个嵌套 async 函数体跳过（含内部 await 一律合法）
      const close = matchBrace(body, i);
      pendingAsync = false;
      if (close < 0) return true;
      i = close + 1;
      continue;
    }
    if (boundary && body.startsWith('await', i) && !/[\w$]/.test(body[i + 5] || '')) return true;
    // 函数头（`async (...) =>`）里会出现空格、括号等字符，只有遇到语句边界才清掉标记
    if (c === ';' || c === '}') pendingAsync = false;
    i += 1;
  }
  return false;
}

/** 行号（从 1 开始） */
function lineAt(text, index) {
  return text.slice(0, index).split('\n').length;
}

const files = collect(WEB_JS);
const tmp = mkdtempSync(join(tmpdir(), 'ndw-webcheck-'));
const failures = [];
const suspicious = [];

try {
  files.forEach((file, index) => {
    const text = readFileSync(file, 'utf8');
    const copy = join(tmp, `${String(index).padStart(3, '0')}-${basename(file, '.js')}.mjs`);
    writeFileSync(copy, text);
    const result = spawnSync(process.execPath, ['--check', copy], { encoding: 'utf8' });
    if (result.status !== 0) {
      failures.push({ file, message: (result.stderr || result.stdout || '').trim() });
    }
    for (const pattern of CALLBACK_PATTERNS) {
      pattern.lastIndex = 0;
      let match;
      while ((match = pattern.exec(text))) {
        const open = text.indexOf('{', match.index + match[0].length - 1);
        if (open < 0) continue;
        const close = matchBrace(text, open);
        if (close < 0) continue;
        const body = text.slice(open + 1, close);
        if (!hasLooseAwait(body)) continue;
        suspicious.push({
          file,
          line: lineAt(text, match.index),
          snippet: match[0].replace(/\s+/g, ' ').slice(0, 80),
        });
      }
    }
  });
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

const rel = (file) => relative(ROOT, file);
console.log(`扫描 ${files.length} 个前端脚本（ES module 模式）。`);
if (failures.length) {
  console.error(`ES module 语法检查失败 ${failures.length} 个：`);
  for (const row of failures) {
    console.error(`\n- ${rel(row.file)}\n${row.message.split('\n').slice(0, 3).join('\n')}`);
  }
}
if (suspicious.length) {
  console.error(`\n非 async 回调内出现 await ${suspicious.length} 处：`);
  for (const row of suspicious) {
    console.error(`- ${rel(row.file)}:${row.line}  ${row.snippet}…`);
  }
}
if (!failures.length && !suspicious.length) {
  console.log(`前端脚本检查通过：${files.length} 个文件，ES module 语法 0 失败，非 async 回调 await 0 处。`);
}
process.exit(failures.length || suspicious.length ? 1 : 0);
