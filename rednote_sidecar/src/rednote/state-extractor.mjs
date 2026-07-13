import { AppError } from '../errors.mjs';
import { selectMediaCandidates } from './media-selector.mjs';

const SCRIPT_OPEN = '<script';
const SCRIPT_CLOSE = '</script';
const STATE_MARKER = 'window.__INITIAL_STATE__';
const IDENTIFIER_CHARACTER = /[A-Za-z0-9_$]/;
const CONTROL_CHARACTER = /\p{Cc}/u;
const MAX_TITLE_CHARACTERS = 300;
const MAX_DESCRIPTION_CHARACTERS = 6_000;
const MAX_HASHTAGS = 20;
const MAX_HASHTAG_CHARACTERS = 80;
const STREAM_FIELDS = [
  'masterUrl',
  'format',
  'videoCodec',
  'audioCodec',
  'width',
  'height',
  'duration',
  'size',
  'videoBitrate',
  'audioDuration',
  'audioBitrate',
  'audioChannels',
  'audioSampleRate',
  'drmType',
];

function schemaError() {
  return new AppError('UPSTREAM_SCHEMA_CHANGED');
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasOwn(record, key) {
  return isRecord(record) && Object.hasOwn(record, key);
}

function isUnsafeAnalysisCharacter(character) {
  if (character === '\t' || character === '\n') {
    return false;
  }

  const codePoint = character.codePointAt(0);

  return (
    CONTROL_CHARACTER.test(character)
    || codePoint === 0x061c
    || codePoint === 0x200e
    || codePoint === 0x200f
    || (codePoint >= 0x202a && codePoint <= 0x202e)
    || (codePoint >= 0x2066 && codePoint <= 0x2069)
  );
}

function readBoundedText(value, maximumCharacters) {
  if (typeof value !== 'string') {
    return undefined;
  }

  const characters = [];

  for (const character of value) {
    if (isUnsafeAnalysisCharacter(character)) {
      continue;
    }

    characters.push(character);

    if (characters.length === maximumCharacters) {
      break;
    }
  }

  return characters.join('');
}

function readDescription(note) {
  if (hasOwn(note, 'desc') && typeof note.desc === 'string') {
    return readBoundedText(note.desc, MAX_DESCRIPTION_CHARACTERS);
  }

  return hasOwn(note, 'description')
    ? readBoundedText(note.description, MAX_DESCRIPTION_CHARACTERS)
    : undefined;
}

function readHashtags(note) {
  if (!hasOwn(note, 'tagList') || !Array.isArray(note.tagList)) {
    return [];
  }

  const hashtags = [];
  const seen = new Set();

  for (const tag of note.tagList) {
    const hashtag = hasOwn(tag, 'name')
      ? readBoundedText(tag.name, MAX_HASHTAG_CHARACTERS)?.trim()
      : undefined;

    if (!hashtag || seen.has(hashtag)) {
      continue;
    }

    seen.add(hashtag);
    hashtags.push(hashtag);

    if (hashtags.length === MAX_HASHTAGS) {
      break;
    }
  }

  return hashtags;
}

function isWhitespace(character) {
  return character !== undefined && /\s/.test(character);
}

function findAsciiCaseInsensitive(source, needle, fromIndex) {
  let candidate = source.indexOf('<', fromIndex);

  while (candidate !== -1) {
    let matches = true;

    for (let offset = 0; offset < needle.length; offset += 1) {
      let code = source.charCodeAt(candidate + offset);

      if (code >= 65 && code <= 90) {
        code += 32;
      }

      if (code !== needle.charCodeAt(offset)) {
        matches = false;
        break;
      }
    }

    if (matches) {
      return candidate;
    }

    candidate = source.indexOf('<', candidate + 1);
  }

  return -1;
}

function findStateObject(html) {
  if (typeof html !== 'string') {
    throw schemaError();
  }

  let cursor = 0;

  while (cursor < html.length) {
    const openStart = findAsciiCaseInsensitive(html, SCRIPT_OPEN, cursor);

    if (openStart === -1) {
      break;
    }

    const afterOpenName = openStart + SCRIPT_OPEN.length;

    if (!isWhitespace(html[afterOpenName]) && html[afterOpenName] !== '>') {
      cursor = afterOpenName;
      continue;
    }

    const bodyStart = html.indexOf('>', afterOpenName);

    if (bodyStart === -1) {
      break;
    }

    let closeStart = findAsciiCaseInsensitive(html, SCRIPT_CLOSE, bodyStart + 1);
    let closeEnd = -1;

    while (closeStart !== -1) {
      let end = closeStart + SCRIPT_CLOSE.length;

      while (isWhitespace(html[end])) {
        end += 1;
      }

      if (html[end] === '>') {
        closeEnd = end + 1;
        break;
      }

      closeStart = findAsciiCaseInsensitive(html, SCRIPT_CLOSE, end);
    }

    if (closeStart === -1) {
      break;
    }

    let markerCursor = bodyStart + 1;

    while (markerCursor < closeStart) {
      const markerStart = html.indexOf(STATE_MARKER, markerCursor);

      if (markerStart === -1 || markerStart >= closeStart) {
        break;
      }

      const before = html[markerStart - 1];
      let assignmentEnd = markerStart + STATE_MARKER.length;

      while (isWhitespace(html[assignmentEnd])) {
        assignmentEnd += 1;
      }

      if (
        IDENTIFIER_CHARACTER.test(before ?? '')
        || before === '.'
        || html[assignmentEnd] !== '='
      ) {
        markerCursor = markerStart + STATE_MARKER.length;
        continue;
      }

      let start = assignmentEnd + 1;

      while (isWhitespace(html[start])) {
        start += 1;
      }

      if (html[start] !== '{') {
        markerCursor = markerStart + STATE_MARKER.length;
        continue;
      }

      let depth = 0;
      let inString = false;
      let escaped = false;

      for (let index = start; index < closeStart; index += 1) {
        const character = html[index];

        if (inString) {
          if (escaped) {
            escaped = false;
          } else if (character === '\\') {
            escaped = true;
          } else if (character === '"') {
            inString = false;
          }
          continue;
        }

        if (character === '"') {
          inString = true;
        } else if (character === '{') {
          depth += 1;
        } else if (character === '}') {
          depth -= 1;

          if (depth === 0) {
            return html.slice(start, index + 1);
          }
        }
      }

      throw schemaError();
    }

    cursor = closeEnd;
  }

  throw schemaError();
}

function normalizeBareUndefined(source) {
  let normalized = '';
  let inString = false;
  let escaped = false;

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];

    if (inString) {
      normalized += character;

      if (escaped) {
        escaped = false;
      } else if (character === '\\') {
        escaped = true;
      } else if (character === '"') {
        inString = false;
      }
      continue;
    }

    if (character === '"') {
      inString = true;
      normalized += character;
      continue;
    }

    if (source.startsWith('undefined', index)) {
      const before = source[index - 1];
      const after = source[index + 'undefined'.length];
      const isBare = !IDENTIFIER_CHARACTER.test(before ?? '')
        && !IDENTIFIER_CHARACTER.test(after ?? '');

      if (isBare) {
        normalized += 'null';
        index += 'undefined'.length - 1;
        continue;
      }
    }

    normalized += character;
  }

  return normalized;
}

function parseState(html) {
  try {
    const source = findStateObject(html);
    const state = JSON.parse(normalizeBareUndefined(source));

    if (!isRecord(state)) {
      throw schemaError();
    }

    return state;
  } catch (error) {
    if (error instanceof AppError) {
      throw error;
    }

    throw schemaError();
  }
}

function copyStream(stream, inheritedDrmType) {
  const safe = {};

  for (const field of STREAM_FIELDS) {
    if (!hasOwn(stream, field)) {
      continue;
    }

    const value = stream[field];

    if (typeof value === 'string' || typeof value === 'number') {
      safe[field] = value;
    }
  }

  if (hasOwn(stream, 'backupUrls') && Array.isArray(stream.backupUrls)) {
    safe.backupUrls = stream.backupUrls.filter((url) => typeof url === 'string');
  }

  if (!hasOwn(stream, 'drmType') && Number.isFinite(inheritedDrmType)) {
    safe.drmType = inheritedDrmType;
  }

  return safe;
}

function readPositiveNumber(record, key) {
  const value = hasOwn(record, key) ? record[key] : undefined;

  if (!Number.isFinite(value) || value <= 0) {
    throw schemaError();
  }

  return value;
}

function readPlaybackMetrics(metadata, streams) {
  try {
    const [candidate] = selectMediaCandidates(streams);

    return {
      durationMs: candidate.durationMs,
      width: candidate.width,
      height: candidate.height,
    };
  } catch (error) {
    if (!(error instanceof AppError) || error.code !== 'NO_COMPATIBLE_MEDIA') {
      throw error;
    }
  }

  return {
    durationMs: readPositiveNumber(metadata, 'duration'),
    width: readPositiveNumber(metadata, 'width'),
    height: readPositiveNumber(metadata, 'height'),
  };
}

export function extractVideoNote(html, noteId) {
  const state = parseState(html);
  const noteState = hasOwn(state, 'note') ? state.note : undefined;
  const detailMap = hasOwn(noteState, 'noteDetailMap') ? noteState.noteDetailMap : undefined;

  if (!isRecord(noteState) || !isRecord(detailMap)) {
    throw schemaError();
  }

  if (typeof noteId !== 'string' || !Object.hasOwn(detailMap, noteId)) {
    throw new AppError('POST_NOT_FOUND');
  }

  const entry = detailMap[noteId];

  if (!isRecord(entry)) {
    throw schemaError();
  }

  if (!Object.hasOwn(entry, 'note') || entry.note === null || entry.note === undefined) {
    throw new AppError('POST_NOT_FOUND');
  }

  const note = entry.note;

  if (!isRecord(note) || !hasOwn(note, 'type') || typeof note.type !== 'string') {
    throw schemaError();
  }

  if (note.type !== 'video') {
    throw new AppError('NOT_A_VIDEO');
  }

  const video = hasOwn(note, 'video') ? note.video : undefined;
  const media = hasOwn(video, 'media') ? video.media : undefined;
  const metadata = hasOwn(media, 'video') ? media.video : undefined;
  const stream = hasOwn(media, 'stream') ? media.stream : undefined;
  const h264 = hasOwn(stream, 'h264') ? stream.h264 : undefined;

  if (
    !hasOwn(note, 'title')
    || typeof note.title !== 'string'
    || !isRecord(video)
    || !isRecord(media)
    || !isRecord(metadata)
    || !isRecord(stream)
    || !Array.isArray(h264)
    || h264.some((item) => !isRecord(item))
  ) {
    throw schemaError();
  }

  const inheritedDrmType = hasOwn(metadata, 'drmType') ? metadata.drmType : undefined;
  const streams = h264.map((item) => copyStream(item, inheritedDrmType));
  const metrics = readPlaybackMetrics(metadata, streams);
  const description = readDescription(note);

  return {
    noteId,
    type: 'video',
    title: readBoundedText(note.title, MAX_TITLE_CHARACTERS),
    ...(description === undefined ? {} : { description }),
    hashtags: readHashtags(note),
    ...metrics,
    streams,
  };
}
