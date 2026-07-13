const INVALID_URL = '[INVALID URL]';
const UNKNOWN_ERROR = 'Unknown error';
const REDACTED_QUERY = '?[REDACTED]';
const ABSOLUTE_HTTP_URL = /\bhttps?:\/\/[^\s<>]+/giu;
const TRAILING_PUNCTUATION = /[),.;!?\]]+$/u;
const XSEC_TOKEN_VALUE = /(\bxsec_token\b["']?\s*(?:=|:)\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s&,;)\]}]+)/giu;

export function redactUrl(value) {
  if (typeof value !== 'string') {
    return INVALID_URL;
  }

  let url;

  try {
    url = new URL(value);
  } catch {
    return INVALID_URL;
  }

  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return INVALID_URL;
  }

  const hasQuery = url.href.includes('?');
  return `${url.origin}${url.pathname}${hasQuery ? REDACTED_QUERY : ''}`;
}

function redactUrlLikeSubstring(value) {
  const punctuation = TRAILING_PUNCTUATION.exec(value)?.[0] ?? '';
  const urlLike = punctuation ? value.slice(0, -punctuation.length) : value;
  const queryIndex = urlLike.indexOf('?');

  if (queryIndex === -1) {
    return value;
  }

  const fragmentIndex = urlLike.indexOf('#', queryIndex + 1);
  const fragment = fragmentIndex === -1 ? '' : urlLike.slice(fragmentIndex);

  return `${urlLike.slice(0, queryIndex)}${REDACTED_QUERY}${fragment}${punctuation}`;
}

function toErrorText(value) {
  try {
    if (typeof value === 'string') {
      return value;
    }

    if (value === null || value === undefined) {
      return UNKNOWN_ERROR;
    }

    if (value instanceof Error || typeof value === 'object' || typeof value === 'function') {
      const message = value.message;

      if (typeof message === 'string') {
        return message;
      }
    }

    return String(value);
  } catch {
    return UNKNOWN_ERROR;
  }
}

export function safeErrorText(value) {
  return toErrorText(value)
    .replace(ABSOLUTE_HTTP_URL, redactUrlLikeSubstring)
    .replace(XSEC_TOKEN_VALUE, '$1[REDACTED]');
}
