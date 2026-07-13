import { createServer as createHttpServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { randomUUID, timingSafeEqual } from 'node:crypto';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { downloadVideo as defaultDownloadVideo } from './download/download-job.mjs';
import { ThreadsCopyService } from './copy/threads-copy-service.mjs';
import { AppError } from './errors.mjs';
import {
  allocateOutputPaths as defaultAllocateOutputPaths,
  cleanupOutputAllocation as defaultCleanupOutputAllocation,
} from './files/output-paths.mjs';
import { FrameStore } from './frames/frame-store.mjs';
import { sendJson } from './http/json.mjs';
import { serveFileRange as defaultServeFileRange } from './http/range.mjs';
import { JobStore } from './jobs/job-store.mjs';
import { createRedNoteClient, readResolvedHtml as defaultReadResolvedHtml } from './rednote/client.mjs';
import { selectMediaCandidates as defaultSelectMediaCandidates } from './rednote/media-selector.mjs';
import { extractVideoNote as defaultExtractVideoNote } from './rednote/state-extractor.mjs';
import { createChromeSearch } from './search/chrome-search.mjs';
import { SessionStore } from './session-store.mjs';

const DEFAULT_MAX_JSON_BYTES = 16 * 1024;
const DEFAULT_BODY_TIMEOUT_MS = 10_000;
const DEFAULT_SEARCH_TTL_MS = 5 * 60 * 1_000;
const JOB_ID = /^(?:[a-f0-9]{32,}|[A-Za-z0-9_-]{22,})$/u;
const FRAME_INDEX = /^[1-5]$/u;

const SECURITY_HEADERS = {
  'content-security-policy': [
    "default-src 'self'",
    "base-uri 'none'",
    "connect-src 'self'",
    "font-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "img-src 'self' data:",
    "media-src 'self' blob:",
    "object-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
  ].join('; '),
  'referrer-policy': 'no-referrer',
  'x-content-type-options': 'nosniff',
  'x-frame-options': 'DENY',
};

const STATIC_FILES = new Map([
  ['/', { file: new URL('../public/index.html', import.meta.url), type: 'text/html; charset=utf-8' }],
  ['/index.html', { file: new URL('../public/index.html', import.meta.url), type: 'text/html; charset=utf-8' }],
  ['/styles.css', { file: new URL('../public/styles.css', import.meta.url), type: 'text/css; charset=utf-8' }],
  ['/app.js', { file: new URL('../public/app.js', import.meta.url), type: 'application/javascript; charset=utf-8' }],
  ['/workflow-state.js', { file: new URL('../public/workflow-state.js', import.meta.url), type: 'application/javascript; charset=utf-8' }],
  ['/video-frames.js', { file: new URL('../public/video-frames.js', import.meta.url), type: 'application/javascript; charset=utf-8' }],
  ['/frame-selector.js', { file: new URL('../public/frame-selector.js', import.meta.url), type: 'application/javascript; charset=utf-8' }],
  ['/threads-copy.js', { file: new URL('../public/threads-copy.js', import.meta.url), type: 'application/javascript; charset=utf-8' }],
  ['/threads-copy-safety.js', { file: new URL('../public/threads-copy-safety.js', import.meta.url), type: 'application/javascript; charset=utf-8' }],
]);

function isApiLikeTarget(target = '') {
  return target === '/api'
    || target.startsWith('/api/')
    || /^https?:\/\/.*\/api(?:[/?#]|$)/i.test(target);
}

function sendText(response, statusCode, body) {
  response.writeHead(statusCode, {
    'content-length': Buffer.byteLength(body),
    'content-type': 'text/plain; charset=utf-8',
  });
  response.end(body);
}

function sendBadRequest(request, response) {
  if (isApiLikeTarget(request.url)) {
    sendJson(response, 400, { error: 'Bad Request' });
    return;
  }

  sendText(response, 400, 'Bad Request');
}

function sendInternalError(request, response) {
  if (response.headersSent) {
    response.destroy();
    return;
  }

  if (isApiLikeTarget(request.url)) {
    sendJson(response, 500, { error: 'Internal Server Error' });
    return;
  }

  sendText(response, 500, 'Internal Server Error');
}

function appError(code) {
  return new AppError(code);
}

function sendAppError(response, error, headers = {}) {
  if (response.writableEnded || response.destroyed) return;
  for (const [name, value] of Object.entries(headers)) response.setHeader(name, value);
  response.setHeader('cache-control', 'no-store');
  sendJson(response, error.status, { ok: false, error: error.toJSON() });
}

function parseLoopbackHost(host) {
  if (typeof host !== 'string' || host.length === 0 || host !== host.trim()) return false;
  let parsed;
  try {
    parsed = new URL(`http://${host}`);
  } catch {
    return false;
  }
  if (parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) return false;
  const hostname = parsed.hostname.toLowerCase();
  if (hostname !== 'localhost' && hostname !== '127.0.0.1' && hostname !== '[::1]') return false;
  if (parsed.port !== '') {
    const port = Number(parsed.port);
    if (!Number.isInteger(port) || port < 1 || port > 65_535) return false;
  }
  return parsed.host.toLowerCase() === host.toLowerCase();
}

function validateRequestSource(request) {
  const host = request.headers.host;
  if (!parseLoopbackHost(host)) throw appError('INVALID_ORIGIN');
  const origin = request.headers.origin;
  if (origin !== undefined && origin !== `http://${host}`) throw appError('INVALID_ORIGIN');
}

function validateRequestTarget(request, target) {
  const rawTarget = request.url;
  if (typeof rawTarget !== 'string') throw appError('INVALID_REQUEST');
  const hasAuthority = rawTarget.startsWith('//')
    || /^[A-Za-z][A-Za-z0-9+.-]*:\/\//u.test(rawTarget);
  if (!hasAuthority) return;
  const host = request.headers.host;
  if (
    target.protocol !== 'http:'
    || target.username !== ''
    || target.password !== ''
    || target.host.toLowerCase() !== host.toLowerCase()
  ) {
    throw appError('INVALID_ORIGIN');
  }
}

function secretsMatch(actual, expected) {
  if (typeof actual !== 'string') return false;
  const actualBytes = Buffer.from(actual);
  const expectedBytes = Buffer.from(expected);
  return actualBytes.length === expectedBytes.length
    && timingSafeEqual(actualBytes, expectedBytes);
}

function validateApiAuthentication(request, pathname, sidecarKey) {
  if (!sidecarKey || pathname === '/api/health') return;
  if (!secretsMatch(request.headers['x-rednote-sidecar-key'], sidecarKey)) {
    throw appError('SIDECAR_AUTH_REQUIRED');
  }
}

function publicSearchResult(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw appError('CHROME_SEARCH_FAILED');
  }
  const noteId = value.noteId;
  const canonicalUrl = value.canonicalUrl;
  if (
    typeof noteId !== 'string'
    || !/^[a-f0-9]{24}$/u.test(noteId)
    || canonicalUrl !== `https://www.rednote.com/explore/${noteId}`
    || typeof value.title !== 'string'
    || typeof value.description !== 'string'
    || typeof value.creator !== 'string'
    || typeof value.thumbnailUrl !== 'string'
    || value.isVideo !== true
  ) {
    throw appError('CHROME_SEARCH_FAILED');
  }
  return Object.freeze({
    noteId,
    canonicalUrl,
    title: value.title,
    description: value.description,
    creator: value.creator,
    thumbnailUrl: value.thumbnailUrl,
    isVideo: true,
  });
}

function validatePrivateSearchHref(value, noteId) {
  if (typeof value !== 'string') throw appError('CHROME_SEARCH_FAILED');
  let url;
  try {
    url = new URL(value);
  } catch {
    throw appError('CHROME_SEARCH_FAILED');
  }
  if (
    url.protocol !== 'https:'
    || url.hostname !== 'www.rednote.com'
    || url.port !== ''
    || url.username !== ''
    || url.password !== ''
    || url.hash !== ''
    || ![`/explore/${noteId}`, `/search_result/${noteId}`].includes(url.pathname)
  ) {
    throw appError('CHROME_SEARCH_FAILED');
  }
  return url.href;
}

class SearchResultStore {
  #entries = new Map();
  #now;
  #randomId;
  #ttlMs;

  constructor({ now = Date.now, randomId = randomUUID, ttlMs = DEFAULT_SEARCH_TTL_MS } = {}) {
    if (
      typeof now !== 'function'
      || typeof randomId !== 'function'
      || !Number.isSafeInteger(ttlMs)
      || ttlMs < 1
    ) {
      throw new TypeError('검색 결과 저장소 설정이 올바르지 않습니다.');
    }
    this.#now = now;
    this.#randomId = randomId;
    this.#ttlMs = ttlMs;
  }

  sweep() {
    const now = this.#now();
    for (const [searchId, entry] of this.#entries) {
      if (entry.expiresAt <= now) this.#entries.delete(searchId);
    }
  }

  create(results, privateHrefs) {
    if (
      !Array.isArray(results)
      || results.length < 1
      || results.length > 50
      || !Array.isArray(privateHrefs)
      || privateHrefs.length !== results.length
    ) {
      throw appError('CHROME_SEARCH_FAILED');
    }
    this.sweep();
    const searchId = this.#randomId();
    const stored = new Map();
    const publicResults = results.map((value, index) => {
      const result = publicSearchResult(value);
      const resultId = this.#randomId();
      const privateHref = validatePrivateSearchHref(privateHrefs[index], result.noteId);
      stored.set(resultId, privateHref);
      return Object.freeze({ resultId, ...result });
    });
    this.#entries.set(searchId, {
      expiresAt: this.#now() + this.#ttlMs,
      results: stored,
    });
    return Object.freeze({ searchId, results: Object.freeze(publicResults) });
  }

  get(searchId, resultId) {
    this.sweep();
    const value = this.#entries.get(searchId)?.results.get(resultId);
    if (!value) throw appError('SEARCH_RESULT_NOT_FOUND');
    return value;
  }
}

function createDisconnectSignal(request, response) {
  const controller = new AbortController();
  const abort = () => {
    if (!controller.signal.aborted) {
      controller.abort(new DOMException('The client disconnected.', 'AbortError'));
    }
  };
  const onResponseClose = () => {
    if (!response.writableEnded) abort();
  };
  request.once('aborted', abort);
  response.once('close', onResponseClose);
  return {
    signal: controller.signal,
    cleanup() {
      request.removeListener('aborted', abort);
      response.removeListener('close', onResponseClose);
    },
  };
}

function runStoreSweep(store) {
  try {
    const result = typeof store?.sweep === 'function' ? store.sweep() : undefined;
    if (result && typeof result.then === 'function') {
      Promise.resolve(result).catch(() => {});
    }
  } catch {
    // Expiry maintenance must never break an otherwise valid local request.
  }
}

function runStoreMaintenance(deps) {
  runStoreSweep(deps.sessionStore);
  runStoreSweep(deps.jobStore);
  runStoreSweep(deps.searchResultStore);
}

function parseContentLength(request) {
  const source = request.headers['content-length'];
  if (source === undefined) return undefined;
  if (typeof source !== 'string' || !/^(?:0|[1-9]\d*)$/u.test(source)) throw appError('INVALID_REQUEST');
  const value = Number(source);
  if (!Number.isSafeInteger(value)) throw appError('REQUEST_TOO_LARGE');
  return value;
}

function readBodyBytes(request, maxBytes, bodyTimeoutMs, declaredLength) {
  if (declaredLength !== undefined && declaredLength > maxBytes) {
    request.pause();
    throw appError('REQUEST_TOO_LARGE');
  }

  return new Promise((resolve, reject) => {
    const chunks = [];
    let received = 0;
    let settled = false;
    let timer;

    const cleanup = () => {
      clearTimeout(timer);
      request.removeListener('data', onData);
      request.removeListener('end', onEnd);
      request.removeListener('error', onError);
      request.removeListener('aborted', onAborted);
    };
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) {
        request.pause();
        reject(error);
      } else {
        resolve(value);
      }
    };
    const onData = (chunk) => {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      received += bytes.length;
      if (received > maxBytes) {
        finish(appError('REQUEST_TOO_LARGE'));
        return;
      }
      chunks.push(bytes);
    };
    const onEnd = () => {
      if (declaredLength !== undefined && received !== declaredLength) {
        finish(appError('INVALID_REQUEST'));
        return;
      }
      finish(null, Buffer.concat(chunks, received));
    };
    const onError = () => finish(appError('INVALID_REQUEST'));
    const onAborted = () => finish(appError('REQUEST_CANCELLED'));

    request.once('end', onEnd);
    request.once('error', onError);
    request.once('aborted', onAborted);
    timer = setTimeout(() => finish(appError('REQUEST_BODY_TIMEOUT')), bodyTimeoutMs);
    timer.unref?.();
    request.on('data', onData);
    if (request.readableEnded) queueMicrotask(onEnd);
  });
}

function bodyWithDeadline(body, bodyTimeoutMs) {
  return Object.freeze({
    [Symbol.asyncIterator]() {
      const iterator = body[Symbol.asyncIterator]();
      let pending;
      let timedOut = false;
      let timer;
      const deadline = new Promise((resolve, reject) => {
        timer = setTimeout(() => {
          timedOut = true;
          reject(appError('REQUEST_BODY_TIMEOUT'));
        }, bodyTimeoutMs);
        timer.unref?.();
      });

      return (async function* iterateWithDeadline() {
        try {
          while (true) {
            pending = Promise.resolve().then(() => iterator.next());
            pending.catch(() => {});
            const result = await Promise.race([pending, deadline]);
            pending = undefined;
            if (result.done) return;
            yield result.value;
          }
        } finally {
          clearTimeout(timer);
          if (!timedOut && typeof iterator.return === 'function') {
            await iterator.return();
          }
        }
      }());
    },
  });
}

async function readJsonBody(request, maxBytes, bodyTimeoutMs, { allowEmpty = false } = {}) {
  const contentType = request.headers['content-type'];
  const length = parseContentLength(request);
  const bytes = await readBodyBytes(request, maxBytes, bodyTimeoutMs, length);
  if (bytes.length === 0 && allowEmpty) return {};
  if (typeof contentType !== 'string' || !/^application\/json(?:\s*;\s*charset=utf-8)?$/iu.test(contentType)) {
    throw appError('INVALID_REQUEST');
  }
  let value;
  try {
    value = JSON.parse(bytes.toString('utf8'));
  } catch {
    throw appError('INVALID_REQUEST');
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw appError('INVALID_REQUEST');
  return value;
}

function requireKeys(record, keys) {
  const actual = Object.keys(record).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw appError('INVALID_REQUEST');
  }
}

function methodNotAllowed(response, allow) {
  sendAppError(response, appError('METHOD_NOT_ALLOWED'), { allow });
}

function publicNote(note) {
  return Object.freeze({
    noteId: note.noteId,
    title: note.title,
    durationMs: note.durationMs,
    width: note.width,
    height: note.height,
  });
}

function privateAnalysisContext(note, resolved) {
  if (
    typeof resolved?.fetchUrl !== 'string'
    || typeof note?.title !== 'string'
    || (note.description !== undefined && typeof note.description !== 'string')
    || (note.hashtags !== undefined && !Array.isArray(note.hashtags))
  ) {
    throw appError('UPSTREAM_SCHEMA_CHANGED');
  }

  let canonicalUrl;

  try {
    canonicalUrl = new URL(resolved.fetchUrl);
  } catch {
    throw appError('UPSTREAM_SCHEMA_CHANGED');
  }

  if (
    canonicalUrl.protocol !== 'https:'
    || canonicalUrl.hostname !== 'www.rednote.com'
    || canonicalUrl.port !== ''
    || canonicalUrl.username !== ''
    || canonicalUrl.password !== ''
    || canonicalUrl.search !== ''
    || canonicalUrl.hash !== ''
    || canonicalUrl.pathname !== `/explore/${note.noteId}`
    || canonicalUrl.href !== resolved.fetchUrl
  ) {
    throw appError('UPSTREAM_SCHEMA_CHANGED');
  }

  return Object.freeze({
    canonicalUrl: resolved.fetchUrl,
    title: note.title,
    ...(typeof note.description === 'string' ? { description: note.description } : {}),
    hashtags: Object.freeze([...(note.hashtags ?? [])]),
  });
}

async function resolveToSession(sourceUrl, deps, signal) {
  const resolved = await deps.client.resolve(sourceUrl, { signal });
  const html = deps.readResolvedHtml(resolved);
  const extracted = deps.extractVideoNote(html, resolved.noteId);
  const candidates = deps.selectMediaCandidates(extracted.streams);
  const note = publicNote(extracted);
  const analysisContext = privateAnalysisContext(extracted, resolved);
  const sessionId = deps.sessionStore.create({
    sourceUrl,
    note,
    analysisContext,
    candidates,
  });
  return Object.freeze({ sessionId, note });
}

async function handleApi(request, response, target, deps, signal) {
  const { pathname } = target;

  if (pathname === '/api/health') {
    if (request.method !== 'GET') {
      methodNotAllowed(response, 'GET');
      return;
    }
    sendJson(response, 200, { ok: true });
    return;
  }

  if (pathname === '/api/search') {
    if (request.method !== 'POST') {
      methodNotAllowed(response, 'POST');
      return;
    }
    if (target.search !== '') throw appError('INVALID_REQUEST');
    const body = await readJsonBody(request, deps.maxJsonBytes, deps.bodyTimeoutMs);
    requireKeys(body, ['query']);
    if (typeof body.query !== 'string') throw appError('INVALID_REQUEST');
    const found = await deps.chromeSearch.search(body.query, { signal });
    const privateHrefs = Array.isArray(found?.privateHrefs)
      ? found.privateHrefs
      : found?.results?.map((result) => result.canonicalUrl);
    const stored = deps.searchResultStore.create(found?.results, privateHrefs);
    sendJson(response, 200, { ok: true, ...stored });
    return;
  }

  const searchResolveMatch = /^\/api\/searches\/([^/]+)\/results\/([^/]+)\/resolve$/u.exec(pathname);
  if (searchResolveMatch) {
    if (request.method !== 'POST') {
      methodNotAllowed(response, 'POST');
      return;
    }
    if (target.search !== '') throw appError('INVALID_REQUEST');
    const body = await readJsonBody(request, deps.maxJsonBytes, deps.bodyTimeoutMs, { allowEmpty: true });
    requireKeys(body, []);
    const sourceUrl = deps.searchResultStore.get(searchResolveMatch[1], searchResolveMatch[2]);
    const resolved = await resolveToSession(sourceUrl, deps, signal);
    sendJson(response, 200, { ok: true, ...resolved });
    return;
  }

  if (pathname === '/api/resolve') {
    if (request.method !== 'POST') {
      methodNotAllowed(response, 'POST');
      return;
    }
    if (target.search !== '') throw appError('INVALID_REQUEST');
    const body = await readJsonBody(request, deps.maxJsonBytes, deps.bodyTimeoutMs);
    requireKeys(body, ['url']);
    if (typeof body.url !== 'string') throw appError('INVALID_REQUEST');
    const resolved = await resolveToSession(body.url, deps, signal);
    sendJson(response, 200, { ok: true, ...resolved });
    return;
  }

  if (pathname === '/api/jobs') {
    if (request.method !== 'POST') {
      methodNotAllowed(response, 'POST');
      return;
    }
    if (target.search !== '') throw appError('INVALID_REQUEST');
    const body = await readJsonBody(request, deps.maxJsonBytes, deps.bodyTimeoutMs);
    requireKeys(body, ['sessionId']);
    if (typeof body.sessionId !== 'string') throw appError('INVALID_REQUEST');
    const result = await deps.jobStore.runCreation(async () => {
      const session = deps.sessionStore.get(body.sessionId);
      if (!session) throw appError('SESSION_NOT_FOUND');
      deps.sessionStore.delete(body.sessionId);
      const output = await deps.allocateOutputPaths(session.note.noteId);
      let downloaded = false;
      try {
        const download = await deps.downloadVideo({
          candidates: session.candidates,
          output,
          signal,
          reResolve: async () => {
            const fresh = await deps.client.resolve(session.sourceUrl, { signal });
            const html = deps.readResolvedHtml(fresh);
            const note = deps.extractVideoNote(html, fresh.noteId);
            if (note.noteId !== session.note.noteId) throw appError('MEDIA_DOWNLOAD_FAILED');
            return deps.selectMediaCandidates(note.streams);
          },
        });
        downloaded = true;
        const jobId = deps.jobStore.create({
          note: session.note,
          analysisContext: session.analysisContext,
          output,
          video: {
            path: download.videoPath,
            fileName: path.basename(download.videoPath),
            completed: true,
          },
        });
        return { jobId };
      } catch (error) {
        if (!downloaded) await deps.cleanupOutputAllocation(output);
        throw error;
      }
    });
    sendJson(response, 200, {
      ok: true,
      jobId: result.jobId,
      mediaUrl: `/api/jobs/${result.jobId}/video`,
    });
    return;
  }

  const videoMatch = /^\/api\/jobs\/([^/]+)\/video$/u.exec(pathname);
  if (videoMatch) {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      methodNotAllowed(response, 'GET, HEAD');
      return;
    }
    if (target.search !== '') throw appError('INVALID_REQUEST');
    if (!JOB_ID.test(videoMatch[1])) throw appError('INVALID_REQUEST');
    const job = deps.jobStore.get(videoMatch[1]);
    await deps.serveFileRange(request, response, job.video.path);
    return;
  }

  const frameMatch = /^\/api\/jobs\/([^/]+)\/frames\/([^/]+)$/u.exec(pathname);
  if (frameMatch) {
    if (request.method !== 'PUT') {
      methodNotAllowed(response, 'PUT');
      return;
    }
    if (target.searchParams.size !== 1 || target.searchParams.getAll('timeMs').length !== 1) {
      throw appError('INVALID_REQUEST');
    }
    if (!JOB_ID.test(frameMatch[1]) || !FRAME_INDEX.test(frameMatch[2])) {
      throw appError('INVALID_REQUEST');
    }
    const timeSource = target.searchParams.get('timeMs');
    if (typeof timeSource !== 'string' || !/^(?:0|[1-9]\d*)(?:\.\d+)?$/u.test(timeSource)) {
      throw appError('INVALID_REQUEST');
    }
    const timeMs = Number(timeSource);
    if (!Number.isFinite(timeMs)) throw appError('INVALID_REQUEST');
    const index = Number(frameMatch[2]);
    const saved = await deps.jobStore.runOperation(frameMatch[1], async (job, recordFrame) => {
      const frame = await deps.frameStore.save({
        output: job.output,
        noteId: job.note.noteId,
        durationMs: job.note.durationMs,
        index,
        timeMs,
        contentType: request.headers['content-type'],
        contentLength: parseContentLength(request),
        body: bodyWithDeadline(request, deps.bodyTimeoutMs),
        signal,
      });
      return recordFrame(frame);
    });
    sendJson(response, 200, { ok: true, ...saved });
    return;
  }

  const completeMatch = /^\/api\/jobs\/([^/]+)\/complete$/u.exec(pathname);
  if (completeMatch) {
    if (request.method !== 'POST') {
      methodNotAllowed(response, 'POST');
      return;
    }
    if (target.search !== '') throw appError('INVALID_REQUEST');
    if (!JOB_ID.test(completeMatch[1])) throw appError('INVALID_REQUEST');
    const body = await readJsonBody(request, deps.maxJsonBytes, deps.bodyTimeoutMs, { allowEmpty: true });
    requireKeys(body, []);
    const job = deps.jobStore.get(completeMatch[1]);
    if (!job.video.completed || job.frames.length < 3 || job.frames.length > 5) {
      throw appError('FRAMES_INCOMPLETE');
    }
    sendJson(response, 200, {
      ok: true,
      outputDir: job.output.directoryPath,
      files: {
        video: job.video.fileName,
        frames: job.frames.map((frame) => frame.fileName),
      },
      note: {
        noteId: job.note.noteId,
        durationMs: job.note.durationMs,
        width: job.note.width,
        height: job.note.height,
      },
      frameMetadata: job.frames.map((frame) => ({
        index: frame.index,
        timeMs: frame.timeMs,
        fileName: frame.fileName,
      })),
    });
    return;
  }

  const threadsCopyMatch = /^\/api\/jobs\/([^/]+)\/threads-copy$/u.exec(pathname);
  if (threadsCopyMatch) {
    if (request.method !== 'POST') {
      methodNotAllowed(response, 'POST');
      return;
    }
    if (target.search !== '') throw appError('INVALID_REQUEST');
    if (!JOB_ID.test(threadsCopyMatch[1])) throw appError('INVALID_REQUEST');
    const body = await readJsonBody(request, deps.maxJsonBytes, deps.bodyTimeoutMs);
    requireKeys(body, ['regenerate']);
    if (typeof body.regenerate !== 'boolean') throw appError('INVALID_REQUEST');
    const result = await deps.jobStore.runThreadsCopy(
      threadsCopyMatch[1],
      { regenerate: body.regenerate },
      (job, generation) => deps.threadsCopyService.generate({
        job,
        generation,
        signal,
      }),
    );
    sendJson(response, 200, { ok: true, ...result });
    return;
  }

  sendJson(response, 404, { error: 'Not Found' });
}

async function handleRequest(request, response, deps) {
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
    response.setHeader(name, value);
  }

  let target;

  try {
    target = new URL(request.url ?? '/', 'http://localhost');
  } catch {
    sendBadRequest(request, response);
    return;
  }

  const { pathname } = target;
  const api = pathname === '/api' || pathname.startsWith('/api/');
  const disconnect = createDisconnectSignal(request, response);

  try {
    runStoreMaintenance(deps);
    validateRequestSource(request);
    validateRequestTarget(request, target);

    if (api) {
      validateApiAuthentication(request, pathname, deps.sidecarKey);
      await handleApi(request, response, target, deps, disconnect.signal);
      return;
    }

    const asset = request.method === 'GET' ? STATIC_FILES.get(pathname) : undefined;

    if (asset) {
      try {
        const body = await deps.loadFile(asset.file);
        response.writeHead(200, {
          'cache-control': 'no-store',
          'content-length': body.byteLength,
          'content-type': asset.type,
        });
        response.end(body);
        return;
      } catch {
        sendText(response, 500, 'Internal Server Error');
        return;
      }
    }

    sendText(response, 404, 'Not Found');
  } catch (error) {
    if (response.headersSent) {
      if (!response.writableEnded) response.destroy();
      return;
    }
    if (error instanceof AppError) {
      if (error.code === 'REQUEST_TOO_LARGE' || error.code === 'REQUEST_BODY_TIMEOUT') {
        response.setHeader('connection', 'close');
        response.once('finish', () => request.destroy());
      }
      sendAppError(response, error);
      return;
    }
    if (api) {
      sendAppError(response, appError('INTERNAL_ERROR'));
      return;
    }
    sendText(response, error?.code === 'INVALID_ORIGIN' ? 403 : 500, 'Internal Server Error');
  } finally {
    disconnect.cleanup();
  }
}

export function createServer(deps = {}) {
  const maxJsonBytes = deps.maxJsonBytes ?? DEFAULT_MAX_JSON_BYTES;
  const bodyTimeoutMs = deps.bodyTimeoutMs ?? DEFAULT_BODY_TIMEOUT_MS;
  const sidecarKey = deps.sidecarKey ?? process.env.REDNOTE_SIDECAR_KEY ?? '';
  if (!Number.isSafeInteger(maxJsonBytes) || maxJsonBytes < 1 || maxJsonBytes > 64 * 1024) {
    throw new TypeError('서버 설정이 올바르지 않습니다.');
  }
  if (!Number.isSafeInteger(bodyTimeoutMs) || bodyTimeoutMs < 10 || bodyTimeoutMs > 120_000) {
    throw new TypeError('서버 설정이 올바르지 않습니다.');
  }
  if (typeof sidecarKey !== 'string' || sidecarKey.length > 512) {
    throw new TypeError('서버 설정이 올바르지 않습니다.');
  }
  const resolvedDeps = Object.freeze({
    loadFile: deps.readFile ?? readFile,
    client: deps.client ?? createRedNoteClient(),
    readResolvedHtml: deps.readResolvedHtml ?? defaultReadResolvedHtml,
    extractVideoNote: deps.extractVideoNote ?? defaultExtractVideoNote,
    selectMediaCandidates: deps.selectMediaCandidates ?? defaultSelectMediaCandidates,
    chromeSearch: deps.chromeSearch ?? createChromeSearch(),
    searchResultStore: deps.searchResultStore ?? new SearchResultStore(),
    sessionStore: deps.sessionStore ?? new SessionStore(),
    jobStore: deps.jobStore ?? new JobStore(),
    frameStore: deps.frameStore ?? new FrameStore(),
    allocateOutputPaths: deps.allocateOutputPaths ?? defaultAllocateOutputPaths,
    cleanupOutputAllocation: deps.cleanupOutputAllocation ?? defaultCleanupOutputAllocation,
    downloadVideo: deps.downloadVideo ?? defaultDownloadVideo,
    threadsCopyService: deps.threadsCopyService ?? new ThreadsCopyService(),
    serveFileRange: deps.serveFileRange ?? defaultServeFileRange,
    maxJsonBytes,
    bodyTimeoutMs,
    sidecarKey,
  });

  return createHttpServer((request, response) => {
    void handleRequest(request, response, resolvedDeps).catch(() => {
      if (!response.writableEnded && !response.destroyed) {
        sendInternalError(request, response);
      }
    });
  });
}

export function startServer({ host = '127.0.0.1', port = 4310 } = {}) {
  if (host !== '127.0.0.1' && host !== '::1' && host !== 'localhost') {
    throw new TypeError('서버는 로컬 인터페이스에서만 실행할 수 있습니다.');
  }

  const server = createServer();
  server.listen(port, host);
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const server = startServer();

  server.once('listening', () => {
    const address = server.address();
    const port = typeof address === 'object' && address ? address.port : 4310;
    console.log(`RedNote 로컬 앱: http://127.0.0.1:${port}`);
  });
}
