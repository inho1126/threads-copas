import { request as httpsRequest } from 'node:https';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';

import { AppError } from '../errors.mjs';
import {
  cleanupOutputAllocation,
  createOutputPartWriteStream,
  ensureOutputDestinationAbsent,
  inspectOutputPart,
  isOutputAllocation,
  publishOutputPart,
} from '../files/output-paths.mjs';
import { validateNetworkUrl as defaultValidateNetworkUrl } from '../security/network-policy.mjs';

export const DEFAULT_MAX_MEDIA_BYTES = 512 * 1024 * 1024;

const DEFAULT_TIMEOUT_MS = 30_000;
const EXPIRED_STATUSES = new Set([401, 403, 404, 410]);
const REQUEST_HEADERS = Object.freeze({
  Accept: 'video/mp4,video/*;q=0.9,application/octet-stream;q=0.8,*/*;q=0.5',
  Referer: 'https://www.rednote.com/',
  'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
});

class MediaLimitError extends Error {}

const FILESYSTEM_ERROR_CODES = new Set([
  'EACCES',
  'EBADF',
  'EDQUOT',
  'EFBIG',
  'EIO',
  'EISDIR',
  'EMFILE',
  'ENFILE',
  'ENOSPC',
  'EROFS',
]);

class ByteLimitTransform extends Transform {
  constructor(maxBytes) {
    super();
    this.maxBytes = maxBytes;
    this.bytesWritten = 0;
  }

  _transform(chunk, encoding, callback) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, encoding);
    this.bytesWritten += bytes.length;

    if (this.bytesWritten > this.maxBytes) {
      callback(new MediaLimitError());
      return;
    }

    callback(null, bytes);
  }
}

function appError(code) {
  return new AppError(code);
}

function safeDestroy(stream, error) {
  try {
    stream?.destroy?.(error);
  } catch {
    // Cleanup must not replace the stable public error.
  }
}

function destroyResponse(response) {
  const body = response?.body;

  if (body && body !== response) {
    safeDestroy(body);

    if (typeof body.destroy !== 'function' && typeof body.cancel === 'function') {
      Promise.resolve(body.cancel()).catch(() => {});
    }
  }

  safeDestroy(response);
}

function getHeader(headers, name) {
  if (headers === null || typeof headers !== 'object') {
    return undefined;
  }

  const direct = headers[name];
  const value = direct ?? headers[name.toLowerCase()];
  return Array.isArray(value) ? value[0] : value;
}

function parseContentLength(headers) {
  const source = getHeader(headers, 'content-length');

  if (source === undefined) {
    return null;
  }

  if (typeof source !== 'string' || !/^(?:0|[1-9][0-9]*)$/u.test(source)) {
    throw appError('MEDIA_TRUNCATED');
  }

  const length = Number(source);

  if (!Number.isSafeInteger(length)) {
    throw appError('MEDIA_TOO_LARGE');
  }

  return length;
}

function isCompleteFullStartRange(headers, contentLength) {
  const source = getHeader(headers, 'content-range');

  if (typeof source !== 'string') {
    return false;
  }

  const match = /^bytes 0-([0-9]+)\/([0-9]+)$/u.exec(source);

  if (!match) {
    return false;
  }

  const end = Number(match[1]);
  const total = Number(match[2]);
  return Number.isSafeInteger(end)
    && Number.isSafeInteger(total)
    && total > 0
    && end + 1 === total
    && (contentLength === null || contentLength === total);
}

function normalizeCandidateList(value) {
  const candidates = Array.isArray(value) ? value : value?.candidates;

  if (!Array.isArray(candidates)) {
    throw new TypeError('영상 후보 목록이 올바르지 않습니다.');
  }

  const urls = [];

  for (const candidate of candidates) {
    if (
      candidate === null
      || typeof candidate !== 'object'
      || !Array.isArray(candidate.urls)
      || candidate.urls.length === 0
      || candidate.urls.some((url) => typeof url !== 'string' || url.length === 0)
    ) {
      throw new TypeError('영상 후보 목록이 올바르지 않습니다.');
    }

    urls.push(...candidate.urls);
  }

  if (urls.length === 0) {
    throw new TypeError('영상 후보 목록이 올바르지 않습니다.');
  }

  return urls;
}

function createOperationSignal(callerSignal, timeoutMs) {
  const controller = new AbortController();
  let source = null;

  const abortFromCaller = () => {
    if (!controller.signal.aborted) {
      source = 'caller';
      controller.abort(callerSignal.reason);
    }
  };

  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener('abort', abortFromCaller, { once: true });
  }

  const timer = setTimeout(() => {
    if (!controller.signal.aborted) {
      source = 'timeout';
      controller.abort(new DOMException('The operation timed out.', 'TimeoutError'));
    }
  }, timeoutMs);

  return {
    signal: controller.signal,
    abortError: () => appError(source === 'caller' ? 'REQUEST_CANCELLED' : 'UPSTREAM_TIMEOUT'),
    cleanup() {
      clearTimeout(timer);
      callerSignal?.removeEventListener('abort', abortFromCaller);
    },
  };
}

function waitWithAbort(value, context) {
  if (context.signal.aborted) {
    return Promise.reject(context.abortError());
  }

  return new Promise((resolve, reject) => {
    const onAbort = () => reject(context.abortError());
    context.signal.addEventListener('abort', onAbort, { once: true });

    Promise.resolve(value).then(
      (result) => {
        context.signal.removeEventListener('abort', onAbort);
        resolve(result);
      },
      (error) => {
        context.signal.removeEventListener('abort', onAbort);
        reject(context.signal.aborted ? context.abortError() : error);
      },
    );
  });
}

function defaultRequest({ href, lookup, headers, signal }) {
  return new Promise((resolve, reject) => {
    const request = httpsRequest(href, {
      method: 'GET',
      lookup,
      headers,
      signal,
    }, resolve);

    request.once('error', reject);
    request.end();
  });
}

function responseBody(response) {
  const body = response?.body ?? response;

  if (body instanceof Readable || typeof body?.pipe === 'function') {
    return body;
  }

  if (typeof ReadableStream !== 'undefined' && body instanceof ReadableStream) {
    return Readable.fromWeb(body);
  }

  throw appError('MEDIA_TRUNCATED');
}

async function validateFtyp(output) {
  const { fileSize, head } = await inspectOutputPart(output, 64);

  for (let offset = 0; offset + 8 <= head.length && offset < 64;) {
      const shortSize = head.readUInt32BE(offset);
      const type = head.toString('ascii', offset + 4, offset + 8);
      let headerSize = 8;
      let boxSize = shortSize;

      if (shortSize === 0) {
        return false;
      }

      if (shortSize === 1) {
        headerSize = 16;

        if (offset + headerSize > head.length) {
          return false;
        }

        const extendedSize = head.readBigUInt64BE(offset + 8);

        if (extendedSize > BigInt(Number.MAX_SAFE_INTEGER)) {
          return false;
        }

        boxSize = Number(extendedSize);
      }

      if (
        boxSize < headerSize
        || boxSize > fileSize - offset
      ) {
        return false;
      }

      if (type === 'ftyp') {
        return boxSize >= headerSize + 8
          && (boxSize - headerSize - 8) % 4 === 0;
      }

      const nextOffset = offset + boxSize;

      if (nextOffset <= offset) {
        return false;
      }

      offset = nextOffset;
  }

  return false;
}

async function downloadAttempt({
  href,
  output,
  request,
  validateNetworkUrl,
  context,
  maxBytes,
  allowFullStart206,
}) {
  let response;
  let destination;

  try {
    let target;

    try {
      target = await waitWithAbort(validateNetworkUrl(href, { kind: 'media' }), context);
    } catch (error) {
      if (context.signal.aborted) throw context.abortError();
      return { kind: 'failure', error: appError('MEDIA_DOWNLOAD_FAILED') };
    }

    if (
      target === null
      || typeof target !== 'object'
      || target.href !== href
      || typeof target.lookup !== 'function'
    ) {
      return { kind: 'failure', error: appError('MEDIA_DOWNLOAD_FAILED') };
    }

    try {
      response = await waitWithAbort(request({
        href: target.href,
        lookup: target.lookup,
        headers: { ...REQUEST_HEADERS },
        signal: context.signal,
      }), context);
    } catch (error) {
      if (context.signal.aborted) throw context.abortError();
      return { kind: 'failure', error: appError('MEDIA_DOWNLOAD_FAILED') };
    }

    const statusCode = response?.statusCode;

    if (EXPIRED_STATUSES.has(statusCode)) {
      return { kind: 'expired', error: appError('MEDIA_UNAVAILABLE') };
    }

    let contentLength;

    try {
      contentLength = parseContentLength(response?.headers);
    } catch (error) {
      return { kind: 'failure', error };
    }

    const statusAccepted = statusCode === 200 || (
      statusCode === 206
      && allowFullStart206
      && isCompleteFullStartRange(response?.headers, contentLength)
    );

    if (!statusAccepted) {
      return { kind: 'failure', error: appError('MEDIA_DOWNLOAD_FAILED') };
    }

    if (contentLength !== null && contentLength > maxBytes) {
      return { kind: 'failure', error: appError('MEDIA_TOO_LARGE') };
    }

    const meter = new ByteLimitTransform(maxBytes);
    destination = await createOutputPartWriteStream(output);
    let writeError = null;
    destination.once('error', (error) => {
      writeError = error;
    });

    try {
      await pipeline(responseBody(response), meter, destination, { signal: context.signal });
    } catch (error) {
      if (context.signal.aborted) throw context.abortError();
      if (error instanceof MediaLimitError) {
        return { kind: 'failure', error: appError('MEDIA_TOO_LARGE') };
      }
      if (writeError !== null && FILESYSTEM_ERROR_CODES.has(error?.code)) {
        return { kind: 'failure', error: appError('OUTPUT_WRITE_FAILED') };
      }
      return { kind: 'failure', error: appError('MEDIA_TRUNCATED') };
    }

    if (contentLength !== null && meter.bytesWritten !== contentLength) {
      return { kind: 'failure', error: appError('MEDIA_TRUNCATED') };
    }

    if (!(await validateFtyp(output))) {
      return { kind: 'failure', error: appError('MEDIA_INVALID') };
    }

    await publishOutputPart(output);
    return { kind: 'success', bytesWritten: meter.bytesWritten };
  } finally {
    destroyResponse(response);
    safeDestroy(destination);
  }
}

async function tryUrls(options, urls) {
  let allExpired = true;
  let lastError = appError('MEDIA_UNAVAILABLE');

  for (const href of urls) {
    if (options.context.signal.aborted) {
      throw options.context.abortError();
    }

    const result = await downloadAttempt({ ...options, href });

    if (result.kind === 'success') {
      return result;
    }

    lastError = result.error;

    if (result.kind !== 'expired') {
      allExpired = false;
    }
  }

  return { kind: 'failed', allExpired, error: lastError };
}

export async function downloadVideo({
  candidates,
  output,
  signal,
  reResolve,
  validateNetworkUrl = defaultValidateNetworkUrl,
  request = defaultRequest,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  maxBytes = DEFAULT_MAX_MEDIA_BYTES,
  allowFullStart206 = false,
} = {}) {
  if (
    !isOutputAllocation(output)
    || (signal !== undefined && !(signal instanceof AbortSignal))
    || (reResolve !== undefined && typeof reResolve !== 'function')
    || typeof validateNetworkUrl !== 'function'
    || typeof request !== 'function'
    || !Number.isSafeInteger(timeoutMs)
    || timeoutMs < 1
    || !Number.isSafeInteger(maxBytes)
    || maxBytes < 1
    || typeof allowFullStart206 !== 'boolean'
  ) {
    throw new TypeError('다운로드 작업 설정이 올바르지 않습니다.');
  }

  const initialUrls = normalizeCandidateList(candidates);
  const context = createOperationSignal(signal, timeoutMs);

  try {
    await ensureOutputDestinationAbsent(output);
    let result = await tryUrls({
      output,
      request,
      validateNetworkUrl,
      context,
      maxBytes,
      allowFullStart206,
    }, initialUrls);

    if (result.kind !== 'success' && result.allExpired && reResolve) {
      let freshCandidates;

      try {
        freshCandidates = await waitWithAbort(reResolve(), context);
      } catch (error) {
        if (context.signal.aborted) throw context.abortError();
        throw appError('MEDIA_DOWNLOAD_FAILED');
      }

      let freshUrls;

      try {
        freshUrls = normalizeCandidateList(freshCandidates);
      } catch {
        throw appError('MEDIA_DOWNLOAD_FAILED');
      }

      result = await tryUrls({
        output,
        request,
        validateNetworkUrl,
        context,
        maxBytes,
        allowFullStart206,
      }, freshUrls);
    }

    if (result.kind !== 'success') {
      throw result.allExpired ? appError('MEDIA_UNAVAILABLE') : result.error;
    }

    return Object.freeze({
      directoryPath: output.directoryPath,
      videoPath: output.videoPath,
      bytesWritten: result.bytesWritten,
    });
  } catch (error) {
    await cleanupOutputAllocation(output);

    if (error instanceof AppError) {
      throw error;
    }

    throw appError('MEDIA_DOWNLOAD_FAILED');
  } finally {
    context.cleanup();
  }
}

export const runDownloadJob = downloadVideo;
