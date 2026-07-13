import { AppError } from '../errors.mjs';
import { classifyUrl } from '../security/network-policy.mjs';

function hasOwn(record, key) {
  return record !== null
    && typeof record === 'object'
    && !Array.isArray(record)
    && Object.hasOwn(record, key);
}

function isPositiveNumber(value) {
  return Number.isFinite(value) && value > 0;
}

function hasExplicitPortOrCredentials(source) {
  const authorityMatch = /^[a-z]+:\/\/([^/?#]*)/i.exec(source);

  if (!authorityMatch) {
    return true;
  }

  const authority = authorityMatch[1];
  return authority.includes('@') || authority.includes(':');
}

function normalizeMediaUrl(source) {
  if (typeof source !== 'string' || hasExplicitPortOrCredentials(source)) {
    return null;
  }

  let url;

  try {
    url = new URL(source);
  } catch {
    return null;
  }

  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return null;
  }

  if (url.protocol === 'http:') {
    url.protocol = 'https:';
  }

  return classifyUrl(url) === 'media' ? url.href : null;
}

function collectUrls(stream) {
  const sources = [];

  if (hasOwn(stream, 'masterUrl')) {
    sources.push(stream.masterUrl);
  }

  if (hasOwn(stream, 'backupUrls') && Array.isArray(stream.backupUrls)) {
    sources.push(...stream.backupUrls);
  }

  const seen = new Set();
  const urls = [];

  for (const source of sources) {
    const normalized = normalizeMediaUrl(source);

    if (normalized && !seen.has(normalized)) {
      seen.add(normalized);
      urls.push(normalized);
    }
  }

  return urls;
}

function normalizeCandidate(stream) {
  if (
    !hasOwn(stream, 'format')
    || stream.format !== 'mp4'
    || !hasOwn(stream, 'videoCodec')
    || stream.videoCodec !== 'h264'
    || !hasOwn(stream, 'audioCodec')
    || stream.audioCodec !== 'aac'
    || !hasOwn(stream, 'drmType')
    || stream.drmType !== 0
    || !isPositiveNumber(stream.audioDuration)
    || !isPositiveNumber(stream.width)
    || !isPositiveNumber(stream.height)
    || !isPositiveNumber(stream.duration)
    || !isPositiveNumber(stream.size)
    || !isPositiveNumber(stream.videoBitrate)
  ) {
    return null;
  }

  const urls = collectUrls(stream);

  if (urls.length === 0) {
    return null;
  }

  return {
    urls,
    format: 'mp4',
    videoCodec: 'h264',
    audioCodec: 'aac',
    width: stream.width,
    height: stream.height,
    durationMs: stream.duration,
    size: stream.size,
    videoBitrate: stream.videoBitrate,
    provenance: 'public-playback-stream',
  };
}

export function selectMediaCandidates(streams) {
  if (!Array.isArray(streams)) {
    throw new AppError('UPSTREAM_SCHEMA_CHANGED');
  }

  const candidates = streams
    .map(normalizeCandidate)
    .filter((candidate) => candidate !== null)
    .sort((left, right) => {
      const pixelDifference = (right.width * right.height) - (left.width * left.height);
      return pixelDifference || right.videoBitrate - left.videoBitrate;
    });

  if (candidates.length === 0) {
    throw new AppError('NO_COMPATIBLE_MEDIA');
  }

  return candidates;
}
