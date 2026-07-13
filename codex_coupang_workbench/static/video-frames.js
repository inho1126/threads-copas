import {
  analyzeFrame,
  buildSampleTimes,
  selectRepresentativeFrames,
} from './frame-selector.js';

const OPAQUE_ID_PATTERN = /^(?:[a-f0-9]{32,}|[A-Za-z0-9_-]{22,})$/u;
const MAX_FRAME_INDEX = 5;
const DEFAULT_ANALYSIS_EDGE = 320;
const DEFAULT_SEEK_TIMEOUT_MS = 8_000;
const DEFAULT_METADATA_TIMEOUT_MS = 12_000;

export const API_ENDPOINTS = Object.freeze({
  resolve: '/api/resolve',
  jobs: '/api/jobs',
});

function requireFiniteNumber(value, name) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new TypeError(`${name} must be a finite number`);
  }
}

function requirePositiveInteger(value, name) {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new RangeError(`${name} must be a positive integer`);
  }
}

export function requireOpaqueId(value, name = 'jobId') {
  if (typeof value !== 'string' || !OPAQUE_ID_PATTERN.test(value)) {
    throw new TypeError(`${name} must be an opaque URL-safe ID`);
  }
  return value;
}

function requireEventTarget(target, name) {
  if (
    target === null
    || typeof target !== 'object'
    || typeof target.addEventListener !== 'function'
    || typeof target.removeEventListener !== 'function'
  ) {
    throw new TypeError(`${name} must support media events`);
  }
}

function requireTimeout(timeoutMs) {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > 60_000) {
    throw new RangeError('timeoutMs must be an integer between 1 and 60000');
  }
}

function abortError() {
  return new DOMException('작업이 중단되었습니다.', 'AbortError');
}

function requireSignal(signal) {
  if (signal !== undefined && !(signal instanceof AbortSignal)) {
    throw new TypeError('signal must be an AbortSignal');
  }
  if (signal?.aborted) throw abortError();
}

function waitForMediaEvent(target, {
  successEvent,
  timeoutMs,
  signal,
  start,
  timeoutMessage,
  errorMessage,
}) {
  requireEventTarget(target, 'video');
  requireTimeout(timeoutMs);
  try {
    requireSignal(signal);
  } catch (error) {
    return Promise.reject(error);
  }

  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      target.removeEventListener(successEvent, onSuccess);
      target.removeEventListener('error', onError);
      signal?.removeEventListener('abort', onAbort);
      callback(value);
    };
    const onSuccess = () => finish(resolve);
    const onError = () => finish(reject, new Error(errorMessage));
    const onAbort = () => finish(reject, abortError());
    const timer = setTimeout(() => finish(reject, new Error(timeoutMessage)), timeoutMs);

    target.addEventListener(successEvent, onSuccess);
    target.addEventListener('error', onError);
    signal?.addEventListener('abort', onAbort, { once: true });
    try {
      start();
    } catch (error) {
      finish(reject, error);
    }
  });
}

export function validateMediaUrl(mediaUrl, jobId) {
  requireOpaqueId(jobId);
  const expected = `/api/jobs/${jobId}/video`;
  if (mediaUrl !== expected) {
    throw new TypeError('mediaUrl must be the same-origin video URL for this job');
  }
  return mediaUrl;
}

export function buildFrameUploadUrl(jobId, index, timeSeconds) {
  requireOpaqueId(jobId);
  if (!Number.isSafeInteger(index) || index < 1 || index > MAX_FRAME_INDEX) {
    throw new RangeError('index must be an integer between 1 and 5');
  }
  requireFiniteNumber(timeSeconds, 'time');
  if (timeSeconds < 0) throw new RangeError('time must not be negative');
  const timeMs = Math.round(timeSeconds * 1_000);
  if (!Number.isSafeInteger(timeMs)) throw new RangeError('time is too large');
  return `/api/jobs/${jobId}/frames/${index}?timeMs=${timeMs}`;
}

export function buildCompleteUrl(jobId) {
  return `/api/jobs/${requireOpaqueId(jobId)}/complete`;
}

export function getAnalysisDimensions(width, height, maxEdge = DEFAULT_ANALYSIS_EDGE) {
  requirePositiveInteger(width, 'width');
  requirePositiveInteger(height, 'height');
  requirePositiveInteger(maxEdge, 'maxEdge');
  if (maxEdge > 1_024) throw new RangeError('maxEdge must not exceed 1024');
  const scale = Math.min(1, maxEdge / Math.max(width, height));
  return Object.freeze({
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  });
}

function readVideoDimensions(video) {
  const { videoWidth, videoHeight } = video ?? {};
  requirePositiveInteger(videoWidth, 'videoWidth');
  requirePositiveInteger(videoHeight, 'videoHeight');
  return { width: videoWidth, height: videoHeight };
}

function getCanvasContext(canvas, options) {
  if (canvas === null || typeof canvas !== 'object' || typeof canvas.getContext !== 'function') {
    throw new TypeError('canvas must provide a 2D context');
  }
  const context = canvas.getContext('2d', options);
  if (!context || typeof context.drawImage !== 'function') {
    throw new Error('브라우저에서 영상 프레임을 그릴 수 없습니다.');
  }
  return context;
}

export function readAnalysisFrame(video, canvas, dimensions) {
  readVideoDimensions(video);
  const width = dimensions?.width;
  const height = dimensions?.height;
  requirePositiveInteger(width, 'width');
  requirePositiveInteger(height, 'height');
  canvas.width = width;
  canvas.height = height;
  const context = getCanvasContext(canvas, { alpha: false, willReadFrequently: true });
  if (typeof context.getImageData !== 'function') {
    throw new Error('브라우저에서 영상 프레임을 읽을 수 없습니다.');
  }
  context.drawImage(video, 0, 0, width, height);
  const rgba = context.getImageData(0, 0, width, height)?.data;
  if (
    (!(rgba instanceof Uint8Array) && !(rgba instanceof Uint8ClampedArray))
    || rgba.length !== width * height * 4
  ) {
    throw new Error('영상 프레임의 픽셀 데이터가 올바르지 않습니다.');
  }
  return rgba;
}

function validateLoadedVideo(video) {
  readVideoDimensions(video);
  requireFiniteNumber(video.duration, 'duration');
  if (video.duration <= 0) throw new RangeError('duration must be positive');
}

export async function waitForVideoMetadata(video, {
  signal,
  timeoutMs = DEFAULT_METADATA_TIMEOUT_MS,
} = {}) {
  requireEventTarget(video, 'video');
  requireTimeout(timeoutMs);
  requireSignal(signal);
  if (video.readyState >= 1) {
    validateLoadedVideo(video);
    return;
  }
  await waitForMediaEvent(video, {
    successEvent: 'loadedmetadata',
    timeoutMs,
    signal,
    start() {},
    timeoutMessage: '영상 정보를 불러오는 시간이 초과되었습니다.',
    errorMessage: '다운로드한 영상을 브라우저에서 열 수 없습니다.',
  });
  validateLoadedVideo(video);
}

export async function seekVideoFrame(video, timeSeconds, {
  signal,
  timeoutMs = DEFAULT_SEEK_TIMEOUT_MS,
} = {}) {
  requireEventTarget(video, 'video');
  requireTimeout(timeoutMs);
  requireSignal(signal);
  requireFiniteNumber(video.duration, 'duration');
  requireFiniteNumber(timeSeconds, 'time');
  if (video.duration <= 0 || timeSeconds < 0 || timeSeconds >= video.duration) {
    throw new RangeError('time must be within the video duration');
  }
  if (Math.abs(Number(video.currentTime) - timeSeconds) <= 0.0005 && video.readyState >= 2) return;
  await waitForMediaEvent(video, {
    successEvent: 'seeked',
    timeoutMs,
    signal,
    start() { video.currentTime = timeSeconds; },
    timeoutMessage: '대표 장면을 찾는 시간이 초과되었습니다.',
    errorMessage: '영상에서 대표 장면을 찾을 수 없습니다.',
  });
}

export function canvasToJpegBlob(canvas, { signal } = {}) {
  if (canvas === null || typeof canvas !== 'object' || typeof canvas.toBlob !== 'function') {
    return Promise.reject(new TypeError('canvas must support JPG encoding'));
  }
  try {
    requireSignal(signal);
  } catch (error) {
    return Promise.reject(error);
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener('abort', onAbort);
      callback(value);
    };
    const onAbort = () => finish(reject, abortError());
    signal?.addEventListener('abort', onAbort, { once: true });
    try {
      canvas.toBlob((blob) => {
        if (!(blob instanceof Blob) || blob.size === 0 || blob.type !== 'image/jpeg') {
          finish(reject, new Error('대표 장면을 JPG로 변환할 수 없습니다.'));
          return;
        }
        finish(resolve, blob);
      }, 'image/jpeg', 0.92);
    } catch (error) {
      finish(reject, error);
    }
  });
}

export async function analyzeVideoFrames(video, canvas, {
  signal,
  maxSamples = 24,
  seekTimeoutMs = DEFAULT_SEEK_TIMEOUT_MS,
  onProgress = () => {},
} = {}) {
  validateLoadedVideo(video);
  if (typeof onProgress !== 'function') throw new TypeError('onProgress must be a function');
  const dimensions = getAnalysisDimensions(video.videoWidth, video.videoHeight);
  const times = buildSampleTimes(video.duration, maxSamples);
  const candidates = [];

  for (const [index, time] of times.entries()) {
    await seekVideoFrame(video, time, { signal, timeoutMs: seekTimeoutMs });
    const rgba = readAnalysisFrame(video, canvas, dimensions);
    candidates.push(Object.freeze({
      time,
      rgba: new Uint8ClampedArray(rgba),
      analysis: analyzeFrame(rgba, dimensions.width, dimensions.height),
    }));
    onProgress(index + 1, times.length);
  }
  return selectRepresentativeFrames(candidates);
}

export async function captureJpegAtTime(video, canvas, timeSeconds, {
  signal,
  seekTimeoutMs = DEFAULT_SEEK_TIMEOUT_MS,
} = {}) {
  await seekVideoFrame(video, timeSeconds, { signal, timeoutMs: seekTimeoutMs });
  const { width, height } = readVideoDimensions(video);
  canvas.width = width;
  canvas.height = height;
  const context = getCanvasContext(canvas, { alpha: false });
  context.drawImage(video, 0, 0, width, height);
  return canvasToJpegBlob(canvas, { signal });
}

export function releaseMediaResources(video, canvases = []) {
  try { video?.pause?.(); } catch {}
  try { video?.removeAttribute?.('src'); } catch {}
  try { video?.load?.(); } catch {}
  for (const canvas of canvases) {
    if (canvas && typeof canvas === 'object') {
      canvas.width = 0;
      canvas.height = 0;
    }
  }
}
