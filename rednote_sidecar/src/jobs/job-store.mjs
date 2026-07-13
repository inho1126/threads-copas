import { randomBytes as cryptoRandomBytes } from 'node:crypto';
import path from 'node:path';

import { AppError } from '../errors.mjs';
import { isOutputAllocation } from '../files/output-paths.mjs';

export const DEFAULT_JOB_TTL_MS = 30 * 60 * 1_000;
export const DEFAULT_MAX_ACTIVE_OPERATIONS = 2;

const MIN_RANDOM_BYTES = 16;
const MAX_ID_ATTEMPTS = 16;
const JOB_ID = /^(?:[a-f0-9]{32,}|[A-Za-z0-9_-]{22,})$/u;
const NOTE_ID = /^[a-f0-9]{24}$/u;
const CONTROL_CHARACTER = /\p{Cc}/u;
const MAX_TITLE_CHARACTERS = 300;
const MAX_DESCRIPTION_CHARACTERS = 6_000;
const MAX_HASHTAGS = 20;
const MAX_HASHTAG_CHARACTERS = 80;
const MAX_RESULT_DEPTH = 16;
const MAX_RESULT_VALUES = 10_000;

function configurationError() {
  return new TypeError('작업 저장소 설정이 올바르지 않습니다.');
}

function dataError() {
  return new TypeError('작업 데이터가 올바르지 않습니다.');
}

function isPlainRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false;
  }

  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasUnsafeTextCharacter(value) {
  for (const character of value) {
    if (character === '\t' || character === '\n') continue;

    const codePoint = character.codePointAt(0);

    if (
      CONTROL_CHARACTER.test(character)
      || codePoint === 0x061c
      || codePoint === 0x200e
      || codePoint === 0x200f
      || (codePoint >= 0x202a && codePoint <= 0x202e)
      || (codePoint >= 0x2066 && codePoint <= 0x2069)
    ) {
      return true;
    }
  }

  return false;
}

function isBoundedText(value, maximumCharacters, { allowEmpty = true } = {}) {
  if (
    typeof value !== 'string'
    || (!allowEmpty && value.length === 0)
    || hasUnsafeTextCharacter(value)
  ) {
    return false;
  }

  let count = 0;

  for (const _character of value) {
    count += 1;
    if (count > maximumCharacters) return false;
  }

  return true;
}

function hasExactKeys(value, required, optional = []) {
  if (!isPlainRecord(value)) return false;
  const keys = Object.keys(value).sort();
  const allowed = new Set([...required, ...optional]);

  return required.every((key) => Object.hasOwn(value, key))
    && keys.every((key) => allowed.has(key));
}

function isCanonicalNoteUrl(value, noteId) {
  if (typeof value !== 'string') return false;

  try {
    const url = new URL(value);
    return url.protocol === 'https:'
      && url.hostname === 'www.rednote.com'
      && url.port === ''
      && url.username === ''
      && url.password === ''
      && url.search === ''
      && url.hash === ''
      && url.pathname === `/explore/${noteId}`
      && url.href === value;
  } catch {
    return false;
  }
}

function copyAnalysisContext(value, note) {
  if (value === undefined) return undefined;

  if (
    !hasExactKeys(value, ['canonicalUrl', 'title', 'hashtags'], ['description'])
    || !isCanonicalNoteUrl(value.canonicalUrl, note.noteId)
    || !isBoundedText(value.title, MAX_TITLE_CHARACTERS)
    || value.title !== note.title
    || !Array.isArray(value.hashtags)
    || value.hashtags.length > MAX_HASHTAGS
    || (Object.hasOwn(value, 'description')
      && !isBoundedText(value.description, MAX_DESCRIPTION_CHARACTERS))
  ) {
    throw dataError();
  }

  const hashtags = [];
  const seen = new Set();

  for (const hashtag of value.hashtags) {
    if (
      !isBoundedText(hashtag, MAX_HASHTAG_CHARACTERS, { allowEmpty: false })
      || hashtag.trim() !== hashtag
      || seen.has(hashtag)
    ) {
      throw dataError();
    }

    seen.add(hashtag);
    hashtags.push(hashtag);
  }

  return Object.freeze({
    canonicalUrl: value.canonicalUrl,
    title: value.title,
    ...(Object.hasOwn(value, 'description') ? { description: value.description } : {}),
    hashtags: Object.freeze(hashtags),
  });
}

function copySerializable(value, state = { count: 0, seen: new Set() }, depth = 0) {
  state.count += 1;

  if (state.count > MAX_RESULT_VALUES || depth > MAX_RESULT_DEPTH) {
    throw dataError();
  }

  if (
    value === null
    || typeof value === 'string'
    || typeof value === 'boolean'
    || (typeof value === 'number' && Number.isFinite(value))
  ) {
    return value;
  }

  if (value === undefined || typeof value !== 'object' || state.seen.has(value)) {
    throw dataError();
  }

  state.seen.add(value);

  try {
    if (Array.isArray(value)) {
      return Object.freeze(value.map((item) => copySerializable(item, state, depth + 1)));
    }

    if (!isPlainRecord(value) || Object.getOwnPropertySymbols(value).length !== 0) {
      throw dataError();
    }

    const copy = {};

    for (const key of Object.keys(value)) {
      Object.defineProperty(copy, key, {
        value: copySerializable(value[key], state, depth + 1),
        enumerable: true,
        configurable: false,
        writable: false,
      });
    }

    return Object.freeze(copy);
  } finally {
    state.seen.delete(value);
  }
}

function copyNote(note) {
  if (
    note === null
    || typeof note !== 'object'
    || !NOTE_ID.test(note.noteId)
    || typeof note.title !== 'string'
    || !Number.isFinite(note.durationMs)
    || note.durationMs <= 0
    || !Number.isFinite(note.width)
    || note.width <= 0
    || !Number.isFinite(note.height)
    || note.height <= 0
  ) {
    throw dataError();
  }

  return Object.freeze({
    noteId: note.noteId,
    title: note.title,
    durationMs: note.durationMs,
    width: note.width,
    height: note.height,
  });
}

function copyFrame(frame) {
  return Object.freeze({
    index: frame.index,
    timeMs: frame.timeMs,
    fileName: frame.fileName,
  });
}

function validId(value) {
  return typeof value === 'string' && JOB_ID.test(value);
}

export class JobStore {
  #activeOperations = 0;
  #entries = new Map();
  #maxActive;
  #now;
  #randomBytes;
  #randomId;
  #ttlMs;

  constructor({
    now = Date.now,
    randomBytes = cryptoRandomBytes,
    randomId,
    ttlMs = DEFAULT_JOB_TTL_MS,
    maxActive = DEFAULT_MAX_ACTIVE_OPERATIONS,
  } = {}) {
    if (
      typeof now !== 'function'
      || typeof randomBytes !== 'function'
      || (randomId !== undefined && typeof randomId !== 'function')
      || !Number.isSafeInteger(ttlMs)
      || ttlMs < 1
      || !Number.isSafeInteger(maxActive)
      || maxActive < 1
      || maxActive > DEFAULT_MAX_ACTIVE_OPERATIONS
    ) {
      throw configurationError();
    }

    this.#now = now;
    this.#randomBytes = randomBytes;
    this.#randomId = randomId;
    this.#ttlMs = ttlMs;
    this.#maxActive = maxActive;
  }

  get size() {
    return this.#entries.size;
  }

  get activeOperations() {
    return this.#activeOperations;
  }

  #time() {
    const value = this.#now();
    if (!Number.isSafeInteger(value) || value < 0) throw configurationError();
    return value;
  }

  #nextId() {
    let value;

    if (this.#randomId === undefined) {
      const bytes = this.#randomBytes(MIN_RANDOM_BYTES);
      if (!(bytes instanceof Uint8Array) || bytes.byteLength < MIN_RANDOM_BYTES) {
        throw configurationError();
      }
      value = Buffer.from(bytes).toString('base64url');
    } else {
      value = this.#randomId();
    }

    if (!validId(value)) throw configurationError();
    return value;
  }

  #sweepAt(now) {
    let removed = 0;

    for (const [jobId, entry] of this.#entries) {
      if (entry.expiresAt <= now && entry.activeOperations === 0) {
        this.#entries.delete(jobId);
        removed += 1;
      }
    }

    return removed;
  }

  create(value) {
    const now = this.#time();
    this.#sweepAt(now);

    if (value === null || typeof value !== 'object' || !isOutputAllocation(value.output)) {
      throw dataError();
    }

    const note = copyNote(value.note);
    const analysisContext = copyAnalysisContext(value.analysisContext, note);
    const video = value.video;

    if (
      video === null
      || typeof video !== 'object'
      || video.completed !== true
      || video.path !== value.output.videoPath
      || video.fileName !== path.basename(value.output.videoPath)
    ) {
      throw dataError();
    }

    for (let attempt = 0; attempt < MAX_ID_ATTEMPTS; attempt += 1) {
      const jobId = this.#nextId();
      if (this.#entries.has(jobId)) continue;
      this.#entries.set(jobId, {
        activeOperations: 0,
        analysisContext,
        createdAt: now,
        expiresAt: now + this.#ttlMs,
        frames: new Map(),
        note,
        output: value.output,
        threadsCopyInProgress: false,
        threadsCopyResult: undefined,
        video: Object.freeze({
          completed: true,
          fileName: video.fileName,
          path: video.path,
        }),
      });
      return jobId;
    }

    throw new Error('고유한 작업 ID를 생성하지 못했습니다.');
  }

  #entry(jobId) {
    if (!validId(jobId)) throw new AppError('JOB_NOT_FOUND');
    const entry = this.#entries.get(jobId);
    if (!entry) throw new AppError('JOB_NOT_FOUND');
    if (entry.expiresAt <= this.#time()) throw new AppError('JOB_EXPIRED');
    return entry;
  }

  #entryForThreadsCopy(jobId) {
    if (!validId(jobId)) throw new AppError('JOB_NOT_FOUND');
    const entry = this.#entries.get(jobId);
    if (!entry) throw new AppError('JOB_NOT_FOUND');
    if (entry.threadsCopyInProgress) throw new AppError('THREADS_COPY_IN_PROGRESS');
    if (entry.expiresAt <= this.#time()) throw new AppError('JOB_EXPIRED');
    return entry;
  }

  get(jobId) {
    const entry = this.#entry(jobId);
    return this.#copyJob(jobId, entry);
  }

  #copyJob(jobId, entry) {
    return Object.freeze({
      jobId,
      note: entry.note,
      ...(entry.analysisContext === undefined ? {} : { analysisContext: entry.analysisContext }),
      output: entry.output,
      video: entry.video,
      frames: Object.freeze([...entry.frames.values()].sort((a, b) => a.index - b.index).map(copyFrame)),
      createdAt: entry.createdAt,
      expiresAt: entry.expiresAt,
    });
  }

  getPublic(jobId) {
    const entry = this.#entry(jobId);
    const frameCount = entry.frames.size;
    return Object.freeze({
      jobId,
      note: entry.note,
      videoCompleted: entry.video.completed,
      frameCount,
      complete: entry.video.completed && frameCount >= 3 && frameCount <= 5,
    });
  }

  recordFrame(jobId, frame) {
    const entry = this.#entry(jobId);
    return this.#recordFrame(entry, frame);
  }

  #recordFrame(entry, frame) {
    if (entry.threadsCopyInProgress || entry.threadsCopyResult !== undefined) {
      throw new AppError('FRAMES_SEALED');
    }

    if (
      frame === null
      || typeof frame !== 'object'
      || !Number.isInteger(frame.index)
      || frame.index < 1
      || frame.index > 5
      || !Number.isFinite(frame.timeMs)
      || typeof frame.fileName !== 'string'
      || path.basename(frame.fileName) !== frame.fileName
      || !/^rednote-[a-f0-9]{24}-.+\.jpg$/u.test(frame.fileName)
      || entry.frames.has(frame.index)
    ) {
      throw dataError();
    }
    const saved = copyFrame(frame);
    entry.frames.set(frame.index, saved);
    return saved;
  }

  delete(jobId) {
    if (!validId(jobId)) return false;
    const entry = this.#entries.get(jobId);
    if (!entry || entry.activeOperations > 0) return false;
    return this.#entries.delete(jobId);
  }

  sweep() {
    return this.#sweepAt(this.#time());
  }

  async runCreation(operation) {
    if (typeof operation !== 'function') throw configurationError();
    this.sweep();
    if (this.#activeOperations >= this.#maxActive) {
      throw new AppError('JOB_CONCURRENCY_LIMIT');
    }
    this.#activeOperations += 1;
    try {
      return await operation();
    } finally {
      this.#activeOperations -= 1;
    }
  }

  async runOperation(jobId, operation) {
    if (typeof operation !== 'function') throw configurationError();
    const entry = this.#entry(jobId);
    if (entry.threadsCopyInProgress || entry.threadsCopyResult !== undefined) {
      throw new AppError('FRAMES_SEALED');
    }
    entry.activeOperations += 1;
    let active = true;
    const recordFrame = (frame) => {
      if (!active) throw dataError();
      return this.#recordFrame(entry, frame);
    };

    try {
      return await operation(this.#copyJob(jobId, entry), recordFrame);
    } finally {
      active = false;
      entry.activeOperations -= 1;
    }
  }

  runThreadsCopy(jobId, options, operation) {
    if (
      !hasExactKeys(options, ['regenerate'])
      || typeof options.regenerate !== 'boolean'
      || typeof operation !== 'function'
    ) {
      throw configurationError();
    }

    return this.#runThreadsCopy(jobId, options.regenerate, operation);
  }

  async #runThreadsCopy(jobId, regenerate, operation) {
    const entry = this.#entryForThreadsCopy(jobId);

    if (entry.activeOperations > 0) {
      throw new AppError('FRAMES_IN_PROGRESS');
    }

    if (!entry.video.completed || entry.frames.size < 3 || entry.frames.size > 5) {
      throw new AppError('THREADS_COPY_INCOMPLETE');
    }

    if (!regenerate && entry.threadsCopyResult !== undefined) {
      return entry.threadsCopyResult;
    }

    entry.threadsCopyInProgress = true;
    entry.activeOperations += 1;
    const generation = (entry.threadsCopyResult?.generation ?? 0) + 1;

    try {
      const result = copySerializable(
        await operation(this.#copyJob(jobId, entry), generation),
      );

      if (
        !isPlainRecord(result)
        || !Number.isSafeInteger(result.generation)
        || result.generation < generation
      ) {
        throw dataError();
      }

      entry.threadsCopyResult = result;
      return result;
    } finally {
      entry.activeOperations -= 1;
      entry.threadsCopyInProgress = false;
    }
  }
}
