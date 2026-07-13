import {
  analyzeVideoFrames,
  captureJpegAtTime,
  releaseMediaResources,
  waitForVideoMetadata,
} from './video-frames.js';

const JOB_ID_PATTERN = /^[A-Za-z0-9_-]{22,128}$/u;

function requireJobId(jobId) {
  if (typeof jobId !== 'string' || !JOB_ID_PATTERN.test(jobId)) {
    throw new TypeError('jobId must be an opaque URL-safe identifier');
  }
  return jobId;
}

function requireBasePath(basePath) {
  if (basePath === '' || basePath === '/threads-copas') return basePath;
  throw new TypeError('basePath must be an approved application mount path');
}

function abortError() {
  return new DOMException('작업이 중단되었습니다.', 'AbortError');
}

async function requireJsonResponse(response, fallbackMessage) {
  if (!response?.ok) {
    let detail = '';
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === 'string' ? payload.detail : '';
    } catch {}
    throw new Error(detail || fallbackMessage);
  }
  const payload = await response.json();
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error(fallbackMessage);
  }
  return payload;
}

function createHiddenVideo(documentRef) {
  const video = documentRef.createElement('video');
  video.preload = 'auto';
  video.muted = true;
  video.playsInline = true;
  video.setAttribute('aria-hidden', 'true');
  video.style.display = 'none';
  documentRef.body.append(video);
  return video;
}

function createCanvas(documentRef) {
  return documentRef.createElement('canvas');
}

export async function captureRepresentativeJpegs({
  jobId,
  basePath = globalThis.location?.pathname?.startsWith('/threads-copas') ? '/threads-copas' : '',
  signal,
  fetchImpl = globalThis.fetch,
  documentRef = globalThis.document,
  onProgress = () => {},
} = {}) {
  requireJobId(jobId);
  requireBasePath(basePath);
  if (!(signal instanceof AbortSignal)) throw new TypeError('signal must be an AbortSignal');
  if (signal.aborted) throw abortError();
  if (typeof fetchImpl !== 'function') throw new TypeError('fetchImpl must be a function');
  if (!documentRef?.body || typeof documentRef.createElement !== 'function') {
    throw new TypeError('documentRef must provide DOM element creation');
  }
  if (typeof onProgress !== 'function') throw new TypeError('onProgress must be a function');

  const encodedJobId = encodeURIComponent(jobId);
  const video = createHiddenVideo(documentRef);
  const analysisCanvas = createCanvas(documentRef);
  const captureCanvas = createCanvas(documentRef);

  try {
    video.src = `${basePath}/api/jobs/${encodedJobId}/rednote-video`;
    video.load();
    await waitForVideoMetadata(video, { signal });
    const selectedFrames = await analyzeVideoFrames(video, analysisCanvas, {
      signal,
      maxSamples: 24,
      onProgress(completed, total) {
        onProgress({ phase: 'analyzing', completed, total });
      },
    });
    if (!Array.isArray(selectedFrames) || selectedFrames.length < 3 || selectedFrames.length > 5) {
      throw new Error('대표 장면을 3~5장으로 선정하지 못했습니다.');
    }

    for (const [offset, frame] of selectedFrames.entries()) {
      if (signal.aborted) throw abortError();
      const jpeg = await captureJpegAtTime(video, captureCanvas, frame.time, { signal });
      const index = offset + 1;
      const timeMs = Math.round(frame.time * 1_000);
      const response = await fetchImpl(
        `${basePath}/api/jobs/${encodedJobId}/rednote-frames/${index}?time_ms=${timeMs}`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'image/jpeg' },
          body: jpeg,
          signal,
        },
      );
      await requireJsonResponse(response, '대표 장면을 저장하지 못했습니다.');
      onProgress({ phase: 'uploading', completed: index, total: selectedFrames.length });
    }

    const completed = await fetchImpl(`${basePath}/api/jobs/${encodedJobId}/rednote-complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
      signal,
    });
    return requireJsonResponse(completed, '영상과 대표 장면 저장을 완료하지 못했습니다.');
  } finally {
    releaseMediaResources(video, [analysisCanvas, captureCanvas]);
    video.remove();
  }
}

export class StudioMediaCapture {
  #controller = null;

  abort() {
    this.#controller?.abort();
    this.#controller = null;
  }

  async start(options = {}) {
    this.abort();
    const controller = new AbortController();
    this.#controller = controller;
    try {
      return await captureRepresentativeJpegs({
        ...options,
        signal: controller.signal,
      });
    } finally {
      if (this.#controller === controller) this.#controller = null;
    }
  }
}
