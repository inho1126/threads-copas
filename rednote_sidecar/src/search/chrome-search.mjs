import { spawn } from 'node:child_process';

import { AppError } from '../errors.mjs';

export const DEFAULT_CHROME_SEARCH_TIMEOUT_MS = 20_000;
export const MAX_CHROME_SEARCH_RESULTS = 50;
export const MAX_CHROME_QUERY_CHARACTERS = 100;

const MAX_PROCESS_OUTPUT_BYTES = 512 * 1024;
const NOTE_PATH = /^\/(?:explore|search_result)\/([a-f0-9]{24})$/u;
const CONTROL_CHARACTER = /\p{Cc}/u;

// This script is static by design. The untrusted query is supplied only as the
// final osascript argv value and is never inserted into JavaScript source.
const BROWSER_EXTRACTION_SCRIPT = String.raw`(() => {
  const clean = (value) => typeof value === 'string' ? value.trim() : '';
  const noteSelector = 'a[href*="/explore/"], a[href*="/search_result/"]';
  const text = clean(document.body?.innerText);
  const loginRequired = /登录|登入|扫码登录|手机号登录|log\s*in|sign\s*in/i.test(text)
    && document.querySelectorAll(noteSelector).length === 0;
  const items = [];
  for (const anchor of document.querySelectorAll(noteSelector)) {
    const card = anchor.closest('section, article, li, [class*="note-item"], [class*="feeds-page"]') || anchor.parentElement || anchor;
    const href = clean(anchor.href || anchor.getAttribute('href'));
    const image = card.querySelector('img') || anchor.querySelector('img');
    const marker = card.querySelector('[class*="play"], [class*="video"], video, svg use[href*="play"], svg use[xlink\\:href*="play"]');
    const type = clean(card.getAttribute('data-type') || card.getAttribute('data-note-type')).toLowerCase();
    const titleNode = card.querySelector('[class*="title"], [class*="name"], h1, h2, h3');
    const descriptionNode = card.querySelector('[class*="desc"], [class*="content"]');
    const creatorNode = card.querySelector('[class*="author"], [class*="user"], [class*="creator"]');
    items.push({
      href,
      title: clean(titleNode?.textContent || image?.alt),
      description: clean(descriptionNode?.textContent),
      creator: clean(creatorNode?.textContent),
      thumbnailUrl: clean(image?.currentSrc || image?.src),
      isVideo: Boolean(marker) || type === 'video',
    });
  }
  return JSON.stringify({ loginRequired, items });
})()`;

const JXA_SCRIPT = String.raw`function run(argv) {
  if (!Array.isArray(argv) || argv.length !== 1) throw new Error('INVALID_QUERY');
  const query = argv[0];
  const chrome = Application('Google Chrome');
  if (!chrome.running() || chrome.windows.length === 0) throw new Error('CHROME_NOT_RUNNING');
  const targetWindow = chrome.windows[0];
  const searchUrl = 'https://www.rednote.com/search_result?keyword=' + encodeURIComponent(query) + '&source=web_search_result_notes';
  const tab = chrome.Tab({ url: searchUrl });
  targetWindow.tabs.push(tab);
  targetWindow.activeTabIndex = targetWindow.tabs.length;
  let latest = JSON.stringify({ loginRequired: false, items: [] });
  let previousCount = -1;
  let stableRounds = 0;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    delay(attempt === 0 ? 1.25 : 0.75);
    latest = tab.execute({ javascript: ${JSON.stringify(BROWSER_EXTRACTION_SCRIPT)} });
    let parsed;
    try { parsed = JSON.parse(latest); } catch (_) { parsed = { items: [] }; }
    if (parsed.loginRequired) break;
    const count = Array.isArray(parsed.items) ? parsed.items.length : 0;
    stableRounds = count === previousCount ? stableRounds + 1 : 0;
    previousCount = count;
    if (count >= 50 || stableRounds >= 2) break;
    tab.execute({ javascript: 'window.scrollBy(0, Math.min(window.innerHeight * 1.5, 1600)); true;' });
  }
  return latest;
}`;

function appError(code) {
  return new AppError(code);
}

function cleanText(value, maximumCharacters) {
  if (typeof value !== 'string') return '';
  const clean = [...value.trim()]
    .filter((character) => !CONTROL_CHARACTER.test(character))
    .slice(0, maximumCharacters)
    .join('');
  return clean;
}

function normalizeQuery(value) {
  if (typeof value !== 'string') throw appError('INVALID_REQUEST');
  const clean = value.trim();
  if (!clean || [...clean].some((character) => CONTROL_CHARACTER.test(character))) {
    throw appError('INVALID_REQUEST');
  }
  return [...clean].slice(0, MAX_CHROME_QUERY_CHARACTERS).join('');
}

function normalizeHref(value) {
  if (typeof value !== 'string' || value.length === 0) return null;
  let url;
  try {
    url = new URL(value, 'https://www.rednote.com');
  } catch {
    return null;
  }
  const match = NOTE_PATH.exec(url.pathname);
  if (
    url.protocol !== 'https:'
    || url.hostname !== 'www.rednote.com'
    || url.port !== ''
    || url.username !== ''
    || url.password !== ''
    || url.hash !== ''
    || !match
  ) {
    return null;
  }
  return {
    noteId: match[1],
    canonicalUrl: `https://www.rednote.com/explore/${match[1]}`,
    privateHref: url.href,
  };
}

function normalizeThumbnail(value) {
  if (typeof value !== 'string' || value.length === 0) return '';
  let url;
  try {
    url = new URL(value);
  } catch {
    return '';
  }
  if (
    url.protocol !== 'https:'
    || url.username !== ''
    || url.password !== ''
    || url.hostname === ''
  ) {
    return '';
  }
  url.search = '';
  url.hash = '';
  return url.href;
}

function isVideoRow(row) {
  if (row?.isVideo === true) return true;
  const type = typeof row?.type === 'string' ? row.type.toLowerCase() : '';
  const noteType = typeof row?.noteType === 'string' ? row.noteType.toLowerCase() : '';
  return type === 'video' || noteType === 'video';
}

function hasSignedSearchToken(value) {
  try {
    return new URL(value).searchParams.has('xsec_token');
  } catch {
    return false;
  }
}

function normalizeDetailed(value) {
  const rows = Array.isArray(value) ? value : [];
  const resultIndexes = new Map();
  const detailed = [];

  for (const row of rows) {
    if (row === null || typeof row !== 'object' || Array.isArray(row) || !isVideoRow(row)) continue;
    const href = normalizeHref(row.href ?? row.url ?? row.canonicalUrl);
    if (!href) continue;
    const existingIndex = resultIndexes.get(href.noteId);
    if (existingIndex !== undefined) {
      const existing = detailed[existingIndex];
      if (!hasSignedSearchToken(existing.privateHref) && hasSignedSearchToken(href.privateHref)) {
        detailed[existingIndex] = { ...existing, privateHref: href.privateHref };
      }
      continue;
    }
    if (detailed.length === MAX_CHROME_SEARCH_RESULTS) continue;
    resultIndexes.set(href.noteId, detailed.length);
    detailed.push({
      publicResult: Object.freeze({
        noteId: href.noteId,
        canonicalUrl: href.canonicalUrl,
        title: cleanText(row.title, 300),
        description: cleanText(row.description, 6_000),
        creator: cleanText(row.creator, 300),
        thumbnailUrl: normalizeThumbnail(row.thumbnailUrl ?? row.thumbnail),
        isVideo: true,
      }),
      privateHref: href.privateHref,
    });
  }

  return detailed;
}

export function normalizeChromeSearchResults(value) {
  return normalizeDetailed(value).map(({ publicResult }) => publicResult);
}

export function parseChromeSearchFixture(html) {
  if (typeof html !== 'string') throw new TypeError('검색 결과 fixture가 올바르지 않습니다.');
  const match = /<script\b[^>]*\bid=(?:"rednote-search-data"|'rednote-search-data')[^>]*>([\s\S]*?)<\/script>/iu.exec(html);
  if (!match) throw new TypeError('검색 결과 fixture가 올바르지 않습니다.');
  let payload;
  try {
    payload = JSON.parse(match[1]);
  } catch {
    throw new TypeError('검색 결과 fixture가 올바르지 않습니다.');
  }
  return normalizeChromeSearchResults(payload?.items);
}

function defaultRunProcess(command, args, { timeoutMs, signal }) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(command, args, {
        env: {
          HOME: process.env.HOME,
          LANG: process.env.LANG ?? 'en_US.UTF-8',
          PATH: '/usr/bin:/bin:/usr/sbin:/sbin',
        },
        shell: false,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      });
    } catch (error) {
      reject(error);
      return;
    }

    const stdout = [];
    const stderr = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let settled = false;
    let timedOut = false;

    const terminate = () => {
      if (!child.killed) child.kill('SIGTERM');
    };
    const onAbort = () => terminate();
    const timer = setTimeout(() => {
      timedOut = true;
      terminate();
    }, timeoutMs);
    timer.unref?.();
    signal?.addEventListener('abort', onAbort, { once: true });

    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      if (error) reject(error);
      else resolve(value);
    };
    const collect = (target, chunk, currentBytes) => {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      if (currentBytes + bytes.length > MAX_PROCESS_OUTPUT_BYTES) {
        terminate();
        finish(Object.assign(new Error('Process output exceeded limit.'), { code: 'EOVERFLOW' }));
        return currentBytes;
      }
      target.push(bytes);
      return currentBytes + bytes.length;
    };

    child.stdout.on('data', (chunk) => { stdoutBytes = collect(stdout, chunk, stdoutBytes); });
    child.stderr.on('data', (chunk) => { stderrBytes = collect(stderr, chunk, stderrBytes); });
    child.once('error', (error) => finish(error));
    child.once('close', (exitCode) => {
      if (signal?.aborted) {
        finish(Object.assign(new Error('Search cancelled.'), { code: 'ABORT_ERR' }));
        return;
      }
      if (timedOut) {
        finish(Object.assign(new Error('Search timed out.'), { code: 'ETIMEDOUT' }));
        return;
      }
      finish(null, {
        exitCode,
        stdout: Buffer.concat(stdout, stdoutBytes).toString('utf8'),
        stderr: Buffer.concat(stderr, stderrBytes).toString('utf8'),
      });
    });
  });
}

function permissionFailure(stderr) {
  return /(?:not authorized|not permitted|apple\s*events?|(-1743)|automation permission)/iu.test(stderr);
}

function withDeadline(operation, timeoutMs) {
  let timer;
  const deadline = new Promise((resolve, reject) => {
    timer = setTimeout(() => {
      reject(Object.assign(new Error('Chrome search timed out.'), { code: 'ETIMEDOUT' }));
    }, timeoutMs);
    timer.unref?.();
  });
  return Promise.race([Promise.resolve().then(operation), deadline])
    .finally(() => clearTimeout(timer));
}

function validateOptions({ runProcess, timeoutMs }) {
  if (
    typeof runProcess !== 'function'
    || !Number.isSafeInteger(timeoutMs)
    || timeoutMs < 1
    || timeoutMs > 120_000
  ) {
    throw new TypeError('Chrome 검색 설정이 올바르지 않습니다.');
  }
}

export function createChromeSearch({
  runProcess = defaultRunProcess,
  timeoutMs = DEFAULT_CHROME_SEARCH_TIMEOUT_MS,
} = {}) {
  validateOptions({ runProcess, timeoutMs });

  return Object.freeze({
    async search(query, { signal } = {}) {
      const cleanQuery = normalizeQuery(query);
      if (signal?.aborted) throw appError('REQUEST_CANCELLED');
      let execution;
      try {
        execution = await withDeadline(
          () => runProcess(
            '/usr/bin/osascript',
            ['-l', 'JavaScript', '-e', JXA_SCRIPT, cleanQuery],
            { shell: false, signal, timeoutMs },
          ),
          timeoutMs,
        );
      } catch (error) {
        if (signal?.aborted || error?.code === 'ABORT_ERR') throw appError('REQUEST_CANCELLED');
        if (error?.code === 'ETIMEDOUT') throw appError('CHROME_SEARCH_TIMEOUT');
        if (error?.code === -1743 || permissionFailure(error?.message ?? '')) {
          throw appError('CHROME_PERMISSION_REQUIRED');
        }
        throw appError('CHROME_SEARCH_FAILED');
      }

      const exitCode = execution?.exitCode ?? execution?.status ?? execution?.code;
      const stderr = typeof execution?.stderr === 'string' ? execution.stderr : '';
      if (exitCode !== 0) {
        if (permissionFailure(stderr)) throw appError('CHROME_PERMISSION_REQUIRED');
        throw appError('CHROME_SEARCH_FAILED');
      }

      let payload;
      try {
        payload = JSON.parse(execution.stdout);
      } catch {
        throw appError('CHROME_SEARCH_FAILED');
      }
      if (payload?.loginRequired === true) throw appError('CHROME_LOGIN_REQUIRED');
      const detailed = normalizeDetailed(payload?.items);
      if (detailed.length === 0) throw appError('CHROME_SEARCH_EMPTY');

      return Object.freeze({
        results: Object.freeze(detailed.map(({ publicResult }) => publicResult)),
        privateHrefs: Object.freeze(detailed.map(({ privateHref }) => privateHref)),
      });
    },
  });
}
