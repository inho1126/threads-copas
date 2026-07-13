const NOTE_PATH = /^\/(?:explore|search_result)\/([0-9a-f]{24})$/;
const INVALID_URL_MESSAGE = '유효한 RedNote 공개 영상 링크가 아닙니다.';
const FETCH_QUERY_KEYS = ['xsec_token', 'xsec_source', 'source'];

export function parseRedNoteUrl(input) {
  if (typeof input !== 'string') {
    throw new TypeError(INVALID_URL_MESSAGE);
  }

  let url;

  try {
    url = new URL(input);
  } catch {
    throw new TypeError(INVALID_URL_MESSAGE);
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
    throw new TypeError(INVALID_URL_MESSAGE);
  }

  const fetchUrl = new URL(`${url.origin}${url.pathname}`);

  for (const key of FETCH_QUERY_KEYS) {
    for (const value of url.searchParams.getAll(key)) {
      fetchUrl.searchParams.append(key, value);
    }
  }

  return {
    noteId: match[1],
    fetchUrl: fetchUrl.href,
  };
}
