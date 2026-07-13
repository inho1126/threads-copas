import { randomBytes as cryptoRandomBytes } from 'node:crypto';

export const DEFAULT_SESSION_TTL_MS = 5 * 60 * 1_000;

const MIN_RANDOM_BYTES = 16;
const MAX_ID_ATTEMPTS = 16;
const SESSION_ID = /^(?:[a-f0-9]{32,}|[A-Za-z0-9_-]{22,})$/u;
const NOTE_ID = /^[a-f0-9]{24}$/u;

function configurationError() {
  return new TypeError('세션 저장소 설정이 올바르지 않습니다.');
}

function sessionDataError() {
  return new TypeError('세션 데이터가 올바르지 않습니다.');
}

function isPlainRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false;
  }

  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function cloneAndFreeze(value) {
  if (
    value === null
    || typeof value === 'string'
    || typeof value === 'boolean'
    || (typeof value === 'number' && Number.isFinite(value))
    || value === undefined
  ) {
    return value;
  }

  if (Array.isArray(value)) {
    return Object.freeze(value.map(cloneAndFreeze));
  }

  if (!isPlainRecord(value)) {
    throw sessionDataError();
  }

  const copy = {};

  for (const key of Object.keys(value)) {
    Object.defineProperty(copy, key, {
      value: cloneAndFreeze(value[key]),
      enumerable: true,
      configurable: false,
      writable: false,
    });
  }

  return Object.freeze(copy);
}

function copyPublicNote(note) {
  if (
    !isPlainRecord(note)
    || typeof note.noteId !== 'string'
    || !NOTE_ID.test(note.noteId)
    || typeof note.title !== 'string'
    || !Number.isFinite(note.durationMs)
    || note.durationMs <= 0
    || !Number.isFinite(note.width)
    || note.width <= 0
    || !Number.isFinite(note.height)
    || note.height <= 0
  ) {
    throw sessionDataError();
  }

  return Object.freeze({
    noteId: note.noteId,
    title: note.title,
    durationMs: note.durationMs,
    width: note.width,
    height: note.height,
  });
}

function isValidSessionId(value) {
  return typeof value === 'string' && SESSION_ID.test(value);
}

export class SessionStore {
  #entries = new Map();
  #now;
  #randomBytes;
  #randomId;
  #ttlMs;

  constructor({
    now = Date.now,
    randomBytes = cryptoRandomBytes,
    randomId,
    ttlMs = DEFAULT_SESSION_TTL_MS,
  } = {}) {
    if (
      typeof now !== 'function'
      || typeof randomBytes !== 'function'
      || (randomId !== undefined && typeof randomId !== 'function')
      || !Number.isSafeInteger(ttlMs)
      || ttlMs < 1
    ) {
      throw configurationError();
    }

    this.#now = now;
    this.#randomBytes = randomBytes;
    this.#randomId = randomId;
    this.#ttlMs = ttlMs;
  }

  get size() {
    return this.#entries.size;
  }

  #time() {
    const value = this.#now();

    if (!Number.isSafeInteger(value) || value < 0) {
      throw configurationError();
    }

    return value;
  }

  #nextId() {
    const candidate = this.#randomId === undefined
      ? this.#idFromRandomBytes()
      : this.#randomId();

    if (!isValidSessionId(candidate)) {
      throw configurationError();
    }

    return candidate;
  }

  #idFromRandomBytes() {
    const bytes = this.#randomBytes(MIN_RANDOM_BYTES);

    if (!(bytes instanceof Uint8Array) || bytes.byteLength < MIN_RANDOM_BYTES) {
      throw configurationError();
    }

    return Buffer.from(bytes).toString('base64url');
  }

  #sweepAt(now) {
    let removed = 0;

    for (const [sessionId, entry] of this.#entries) {
      if (entry.expiresAt <= now) {
        this.#entries.delete(sessionId);
        removed += 1;
      }
    }

    return removed;
  }

  create(resolution) {
    const now = this.#time();
    this.#sweepAt(now);

    if (!isPlainRecord(resolution)) {
      throw sessionDataError();
    }

    const value = cloneAndFreeze(resolution);
    const publicNote = copyPublicNote(value.note);

    for (let attempt = 0; attempt < MAX_ID_ATTEMPTS; attempt += 1) {
      const sessionId = this.#nextId();

      if (this.#entries.has(sessionId)) {
        continue;
      }

      this.#entries.set(sessionId, Object.freeze({
        expiresAt: now + this.#ttlMs,
        publicNote,
        value,
      }));
      return sessionId;
    }

    throw new Error('고유한 세션 ID를 생성하지 못했습니다.');
  }

  #activeEntry(sessionId) {
    if (!isValidSessionId(sessionId)) {
      return undefined;
    }

    const entry = this.#entries.get(sessionId);

    if (!entry) {
      return undefined;
    }

    if (entry.expiresAt <= this.#time()) {
      this.#entries.delete(sessionId);
      return undefined;
    }

    return entry;
  }

  get(sessionId) {
    return this.#activeEntry(sessionId)?.value;
  }

  getPublic(sessionId) {
    const entry = this.#activeEntry(sessionId);

    if (!entry) {
      return undefined;
    }

    return Object.freeze({
      sessionId,
      note: entry.publicNote,
    });
  }

  delete(sessionId) {
    return isValidSessionId(sessionId) && this.#entries.delete(sessionId);
  }

  sweep() {
    return this.#sweepAt(this.#time());
  }
}
