import { request as httpsRequest } from 'node:https';

import { AppError } from '../errors.mjs';
import {
  MAX_REDIRECTS,
  validateNetworkUrl as defaultValidateNetworkUrl,
  validateRedirect as defaultValidateRedirect,
} from '../security/network-policy.mjs';
import { parseRedNoteUrl } from './url-policy.mjs';

export const MAX_HTML_BYTES = 8 * 1024 * 1024;

const DEFAULT_TIMEOUT_MS = 10_000;
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const LOGIN_PATH = /\/(?:login|signin)(?:\/|$)/iu;
const LOGIN_HTML = /(?:<title[^>]*>\s*(?:log\s*in|sign\s*in|登录|登入)|<(?:form|div)[^>]+(?:id|class)=["'][^"']*login)/iu;
const CHALLENGE_HTML = /(?:<title[^>]*>[^<]*(?:access\s+verification|security\s+verification|安全验证|访问验证)|<(?:form|div)[^>]+(?:id|class)=["'][^"']*(?:captcha|challenge|verification))/iu;
const HTML_CONTENT_TYPE = /^text\/html(?:\s*;|$)/iu;
const CLIENT_HEADERS = Object.freeze({
  Accept: 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
  'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
});
const RESOLVED_HTML = new WeakMap();

function appError(code) {
  return new AppError(code);
}

export function readResolvedHtml(result) {
  if (
    result === null
    || (typeof result !== 'object' && typeof result !== 'function')
    || !RESOLVED_HTML.has(result)
  ) {
    throw new TypeError('유효한 RedNote 해석 결과가 아닙니다.');
  }

  return RESOLVED_HTML.get(result);
}

function safeDestroy(response) {
  try {
    response?.destroy?.();
  } catch {
    // The stable application error remains the source of truth.
  }
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

function createOperationSignal(callerSignal, timeoutMs) {
  if (callerSignal !== undefined && !(callerSignal instanceof AbortSignal)) {
    throw new TypeError('AbortSignal이 필요합니다.');
  }

  const controller = new AbortController();
  let abortSource = null;
  const abortFromCaller = () => {
    if (!controller.signal.aborted) {
      abortSource = 'caller';
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
      abortSource = 'timeout';
      controller.abort(new DOMException('The operation timed out.', 'TimeoutError'));
    }
  }, timeoutMs);

  return {
    signal: controller.signal,
    source: () => abortSource,
    cleanup() {
      clearTimeout(timer);
      callerSignal?.removeEventListener('abort', abortFromCaller);
    },
  };
}

function abortError(source) {
  return appError(source === 'caller' ? 'REQUEST_CANCELLED' : 'UPSTREAM_TIMEOUT');
}

function waitWithAbort(value, context) {
  if (context.signal.aborted) {
    return Promise.reject(abortError(context.source()));
  }

  return new Promise((resolve, reject) => {
    const onAbort = () => reject(abortError(context.source()));
    context.signal.addEventListener('abort', onAbort, { once: true });

    Promise.resolve(value).then(
      (result) => {
        context.signal.removeEventListener('abort', onAbort);
        resolve(result);
      },
      (error) => {
        context.signal.removeEventListener('abort', onAbort);
        reject(context.signal.aborted ? abortError(context.source()) : error);
      },
    );
  });
}

function validateTarget(target) {
  if (
    target === null
    || typeof target !== 'object'
    || typeof target.href !== 'string'
    || typeof target.lookup !== 'function'
  ) {
    throw appError('UPSTREAM_SCHEMA_CHANGED');
  }

  return target;
}

function getHeader(headers, name) {
  if (headers === null || typeof headers !== 'object') {
    return undefined;
  }

  const value = headers[name];
  return Array.isArray(value) ? value[0] : value;
}

function parseContentLength(headers) {
  const value = getHeader(headers, 'content-length');

  if (value === undefined) {
    return null;
  }

  if (typeof value !== 'string' || !/^\d+$/u.test(value)) {
    throw appError('UPSTREAM_SCHEMA_CHANGED');
  }

  const length = Number(value);

  if (!Number.isSafeInteger(length)) {
    throw appError('UPSTREAM_SCHEMA_CHANGED');
  }

  return length;
}

async function readHtml(response, { context, maxHtmlBytes }) {
  let declaredLength;

  try {
    declaredLength = parseContentLength(response.headers);
  } catch (error) {
    safeDestroy(response);
    throw error;
  }

  if (declaredLength !== null && declaredLength > maxHtmlBytes) {
    safeDestroy(response);
    throw appError('UPSTREAM_SCHEMA_CHANGED');
  }

  const chunks = [];
  let received = 0;
  const abortResponse = () => safeDestroy(response);
  context.signal.addEventListener('abort', abortResponse, { once: true });

  try {
    for await (const chunk of response) {
      if (context.signal.aborted) {
        throw abortError(context.source());
      }

      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      received += bytes.length;

      if (received > maxHtmlBytes) {
        safeDestroy(response);
        throw appError('UPSTREAM_SCHEMA_CHANGED');
      }

      chunks.push(bytes);
    }
  } catch (error) {
    safeDestroy(response);

    if (error instanceof AppError) {
      throw error;
    }

    if (context.signal.aborted) {
      throw abortError(context.source());
    }

    throw appError('UPSTREAM_SCHEMA_CHANGED');
  } finally {
    context.signal.removeEventListener('abort', abortResponse);
  }

  if (declaredLength !== null && received !== declaredLength) {
    safeDestroy(response);
    throw appError('UPSTREAM_SCHEMA_CHANGED');
  }

  return Buffer.concat(chunks, received).toString('utf8');
}

function classifyHtml(html, href) {
  let pathname = '';

  try {
    pathname = new URL(href).pathname;
  } catch {
    throw appError('UPSTREAM_SCHEMA_CHANGED');
  }

  if (LOGIN_PATH.test(pathname) || LOGIN_HTML.test(html)) {
    throw appError('LOGIN_REQUIRED');
  }

  if (CHALLENGE_HTML.test(html)) {
    throw appError('UPSTREAM_CHALLENGE');
  }
}

function mapTransportFailure(error, context) {
  if (error instanceof AppError) {
    return error;
  }

  if (context.signal.aborted) {
    return abortError(context.source());
  }

  return appError('UPSTREAM_SCHEMA_CHANGED');
}

export function createRedNoteClient({
  request = defaultRequest,
  validateNetworkUrl = defaultValidateNetworkUrl,
  validateRedirect = defaultValidateRedirect,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  maxHtmlBytes = MAX_HTML_BYTES,
} = {}) {
  if (
    typeof request !== 'function'
    || typeof validateNetworkUrl !== 'function'
    || typeof validateRedirect !== 'function'
    || !Number.isSafeInteger(timeoutMs)
    || timeoutMs < 1
    || !Number.isSafeInteger(maxHtmlBytes)
    || maxHtmlBytes < 1
    || maxHtmlBytes > MAX_HTML_BYTES
  ) {
    throw new TypeError('RedNote 클라이언트 설정이 올바르지 않습니다.');
  }

  return Object.freeze({
    async resolve(input, { signal: callerSignal } = {}) {
      let parsed;

      try {
        parsed = parseRedNoteUrl(input);
      } catch {
        throw appError('INVALID_REDNOTE_URL');
      }

      const canonicalUrl = new URL(`https://www.rednote.com/explore/${parsed.noteId}`);
      const context = createOperationSignal(callerSignal, timeoutMs);

      try {
        if (context.signal.aborted) {
          throw abortError(context.source());
        }

        let target;

        try {
          target = validateTarget(await waitWithAbort(
            validateNetworkUrl(parsed.fetchUrl, { kind: 'page' }),
            context,
          ));
        } catch (error) {
          throw mapTransportFailure(error, context);
        }

        for (let redirectCount = 0; ; redirectCount += 1) {
          let response;

          try {
            response = await waitWithAbort(request({
              href: target.href,
              lookup: target.lookup,
              headers: { ...CLIENT_HEADERS },
              signal: context.signal,
            }), context);
          } catch (error) {
            throw mapTransportFailure(error, context);
          }

          const statusCode = response?.statusCode;

          if (!Number.isInteger(statusCode)) {
            safeDestroy(response);
            throw appError('UPSTREAM_SCHEMA_CHANGED');
          }

          if (REDIRECT_STATUSES.has(statusCode)) {
            const location = getHeader(response.headers, 'location');
            safeDestroy(response);

            if (redirectCount >= MAX_REDIRECTS || typeof location !== 'string') {
              throw appError('UPSTREAM_SCHEMA_CHANGED');
            }

            let redirectUrl;

            try {
              redirectUrl = new URL(location, target.href).href;
              target = validateTarget(await waitWithAbort(validateRedirect(redirectUrl, {
                kind: 'page',
                redirectCount: redirectCount + 1,
              }), context));
            } catch (error) {
              throw mapTransportFailure(error, context);
            }

            continue;
          }

          if (statusCode === 404) {
            safeDestroy(response);
            throw appError('POST_NOT_FOUND');
          }

          if (statusCode === 401 || statusCode === 403) {
            safeDestroy(response);
            throw appError('LOGIN_REQUIRED');
          }

          if (statusCode < 200 || statusCode > 299) {
            safeDestroy(response);
            throw appError('UPSTREAM_SCHEMA_CHANGED');
          }

          const contentType = getHeader(response.headers, 'content-type');

          if (typeof contentType !== 'string' || !HTML_CONTENT_TYPE.test(contentType)) {
            safeDestroy(response);
            throw appError('UPSTREAM_SCHEMA_CHANGED');
          }

          const html = await readHtml(response, { context, maxHtmlBytes });
          classifyHtml(html, target.href);

          const result = Object.freeze({
            noteId: parsed.noteId,
            fetchUrl: canonicalUrl.href,
          });
          RESOLVED_HTML.set(result, html);
          return result;
        }
      } finally {
        context.cleanup();
      }
    },
  });
}
