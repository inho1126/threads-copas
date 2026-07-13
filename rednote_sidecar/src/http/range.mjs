import { createReadStream as createFileReadStream } from 'node:fs';
import { stat as statFile } from 'node:fs/promises';
import { pipeline } from 'node:stream/promises';

import { AppError } from '../errors.mjs';
import { sendJson } from './json.mjs';

function rangeError(size) {
  const error = new AppError('RANGE_NOT_SATISFIABLE');
  Object.defineProperty(error, 'size', {
    value: size,
    enumerable: false,
    configurable: false,
    writable: false,
  });
  return error;
}

function parseOffset(source, size) {
  if (!/^\d+$/u.test(source)) throw rangeError(size);
  const value = Number(source);
  if (!Number.isSafeInteger(value)) throw rangeError(size);
  return value;
}

export function parseByteRange(header, size) {
  if (!Number.isSafeInteger(size) || size < 0) {
    throw new TypeError('영상 크기가 올바르지 않습니다.');
  }

  if (header === undefined || header === null) {
    return Object.freeze({
      partial: false,
      start: 0,
      end: size - 1,
      length: size,
    });
  }

  if (typeof header !== 'string') throw rangeError(size);
  const match = /^bytes=(\d*)-(\d*)$/u.exec(header);

  if (!match || (match[1] === '' && match[2] === '') || size === 0) {
    throw rangeError(size);
  }

  let start;
  let end;

  if (match[1] === '') {
    const suffixLength = parseOffset(match[2], size);
    if (suffixLength === 0) throw rangeError(size);
    const length = Math.min(suffixLength, size);
    start = size - length;
    end = size - 1;
  } else {
    start = parseOffset(match[1], size);
    end = match[2] === '' ? size - 1 : parseOffset(match[2], size);

    if (start >= size || end < start) throw rangeError(size);
    end = Math.min(end, size - 1);
  }

  return Object.freeze({
    partial: true,
    start,
    end,
    length: end - start + 1,
  });
}

export const parseRange = parseByteRange;

function destroyQuietly(stream, error) {
  try {
    stream?.destroy?.(error);
  } catch {
    // The request handler owns the stable public error response.
  }
}

export async function serveFileRange(request, response, filePath, {
  stat = statFile,
  createReadStream = createFileReadStream,
} = {}) {
  if (
    request === null
    || typeof request !== 'object'
    || response === null
    || typeof response !== 'object'
    || typeof filePath !== 'string'
    || typeof stat !== 'function'
    || typeof createReadStream !== 'function'
  ) {
    throw new TypeError('영상 응답 설정이 올바르지 않습니다.');
  }

  const metadata = await stat(filePath);

  if (!metadata?.isFile?.() || !Number.isSafeInteger(metadata.size) || metadata.size < 0) {
    throw new TypeError('영상 파일이 올바르지 않습니다.');
  }

  let selected;

  try {
    selected = parseByteRange(request.headers?.range, metadata.size);
  } catch (error) {
    if (!(error instanceof AppError) || error.code !== 'RANGE_NOT_SATISFIABLE') {
      throw error;
    }

    response.setHeader('accept-ranges', 'bytes');
    response.setHeader('content-range', `bytes */${metadata.size}`);
    sendJson(response, error.status, { ok: false, error: error.toJSON() });
    return Object.freeze({ statusCode: error.status, bytesSent: 0 });
  }

  const statusCode = selected.partial ? 206 : 200;
  const headers = {
    'accept-ranges': 'bytes',
    'content-length': selected.length,
    'content-type': 'video/mp4',
  };

  if (selected.partial) {
    headers['content-range'] = `bytes ${selected.start}-${selected.end}/${metadata.size}`;
  }

  response.writeHead(statusCode, headers);

  if (request.method === 'HEAD' || selected.length === 0) {
    response.end();
    return Object.freeze({ statusCode, bytesSent: 0 });
  }

  const source = createReadStream(filePath, {
    start: selected.start,
    end: selected.end,
  });

  try {
    await pipeline(source, response);
  } catch (error) {
    destroyQuietly(source, error);
    destroyQuietly(response, error);
    throw error;
  }

  return Object.freeze({ statusCode, bytesSent: selected.length });
}

export const serveRange = serveFileRange;
