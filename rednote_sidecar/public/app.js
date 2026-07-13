import {
  advanceWorkflow,
  createWorkflowState,
  extractApiError,
  failCopyWorkflow,
  failWorkflow,
  getWorkflowControls,
  resetWorkflow,
  restoreWorkflowAfterPageShow,
} from './workflow-state.js';
import {
  buildThreadsCopyUrl,
  formatThreadCardCopy,
  validateThreadsCopyResponse,
} from './threads-copy.js';
import {
  API_ENDPOINTS,
  analyzeVideoFrames,
  buildCompleteUrl,
  buildFrameUploadUrl,
  captureJpegAtTime,
  releaseMediaResources,
  requireOpaqueId,
  validateMediaUrl,
  waitForVideoMetadata,
} from './video-frames.js';

const form = document.querySelector('#download-form');
const urlInput = document.querySelector('#post-url');
const saveButton = document.querySelector('#save-button');
const statusMessage = document.querySelector('#status-message');
const progress = document.querySelector('#workflow-progress');
const errorMessage = document.querySelector('#error-message');
const resultSection = document.querySelector('#save-result');
const outputDirectory = document.querySelector('#output-directory');
const savedFiles = document.querySelector('#saved-files');
const threadCopyResult = document.querySelector('#thread-copy-result');
const threadCopyMeta = document.querySelector('#thread-copy-meta');
const threadCopyCards = document.querySelector('#thread-copy-cards');
const regenerateButton = document.querySelector('#regenerate-button');
const clipboardStatus = document.querySelector('#clipboard-status');
const video = document.querySelector('#work-video');
const analysisCanvas = document.querySelector('#analysis-canvas');
const captureCanvas = document.querySelector('#capture-canvas');

const requiredElements = [
  form,
  urlInput,
  saveButton,
  statusMessage,
  progress,
  errorMessage,
  resultSection,
  outputDirectory,
  savedFiles,
  threadCopyResult,
  threadCopyMeta,
  threadCopyCards,
  regenerateButton,
  clipboardStatus,
  video,
  analysisCanvas,
  captureCanvas,
];

if (requiredElements.some((element) => element === null)) {
  throw new Error('앱 화면을 초기화할 수 없습니다.');
}

let workflow = createWorkflowState();
let activeController = null;
let pageIsLeaving = false;
const lifecycleAborts = new WeakSet();

function setProgress(value) {
  const safeValue = Math.min(100, Math.max(0, Math.round(value)));
  progress.value = safeValue;
  progress.textContent = `${safeValue}%`;
  progress.setAttribute('aria-valuetext', `${safeValue}%`);
}

function appendTextElement(parent, tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  element.textContent = text;
  parent.append(element);
  return element;
}

async function copyStyleToClipboard(style) {
  try {
    if (typeof navigator.clipboard?.writeText !== 'function') {
      throw new Error('Clipboard API unavailable');
    }
    await navigator.clipboard.writeText(formatThreadCardCopy(style));
    clipboardStatus.textContent = `${style.label} 문구를 클립보드에 복사했습니다.`;
  } catch {
    clipboardStatus.textContent = '클립보드에 복사하지 못했습니다. 브라우저 권한을 확인해주세요.';
  }
}

function renderCopyCards(copyResult) {
  threadCopyCards.replaceChildren();

  if (copyResult === null) {
    threadCopyMeta.textContent = workflow.phase === 'generatingCopy'
      ? '대표 장면과 공개 게시물 텍스트를 바탕으로 생성 중입니다.'
      : '아직 생성된 문구 파일이 없습니다. 다시 생성할 수 있습니다.';
    return;
  }

  threadCopyMeta.textContent = [
    `${copyResult.generation}번째 생성`,
    `저장 파일: ${copyResult.files.text}, ${copyResult.files.json}`,
  ].join(' · ');

  for (const style of copyResult.styles) {
    const card = document.createElement('article');
    card.className = 'thread-copy-card';
    appendTextElement(card, 'h4', '', style.label);
    appendTextElement(card, 'p', 'copy-subheading', '첫 문장 후보 3개');

    const hooks = document.createElement('ol');
    hooks.className = 'hook-list';
    for (const hook of style.hooks) appendTextElement(hooks, 'li', '', hook);
    card.append(hooks);

    appendTextElement(card, 'p', 'copy-subheading', '완성형 본문');
    appendTextElement(card, 'p', 'thread-body', style.body);

    const copyButton = appendTextElement(card, 'button', 'copy-button', '이 카드 전체 복사');
    copyButton.type = 'button';
    copyButton.setAttribute('aria-label', `${style.label} 문구 전체 복사`);
    copyButton.addEventListener('click', () => {
      void copyStyleToClipboard(style);
    });
    threadCopyCards.append(card);
  }
}

function renderWorkflow() {
  document.body.dataset.phase = workflow.phase;
  statusMessage.textContent = workflow.message;
  setProgress(workflow.progress);

  const busy = activeController !== null;
  const controls = getWorkflowControls(busy);
  form.setAttribute('aria-busy', String(busy));
  urlInput.disabled = controls.inputDisabled;
  saveButton.disabled = controls.buttonDisabled;
  saveButton.textContent = controls.buttonLabel;
  errorMessage.hidden = workflow.error === null;
  errorMessage.textContent = workflow.error ?? '';

  const result = workflow.result;
  resultSection.hidden = result === null;
  outputDirectory.textContent = result?.outputDir ?? '';
  savedFiles.replaceChildren();
  if (result) {
    const fileNames = [result.files.video, ...result.files.frames];
    for (const fileName of fileNames) appendTextElement(savedFiles, 'li', '', fileName);
  }

  threadCopyResult.hidden = result === null;
  regenerateButton.disabled = busy || !['complete', 'copyError'].includes(workflow.phase);
  regenerateButton.textContent = workflow.phase === 'generatingCopy'
    ? '문구 생성 중…'
    : '문구 다시 생성';
  renderCopyCards(workflow.copyResult);
}

function userFacingError(message) {
  const error = new Error(message);
  error.name = 'UserFacingError';
  return error;
}

async function requestJson(url, options, signal) {
  let response;
  try {
    response = await fetch(url, { ...options, signal });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw userFacingError(extractApiError(0, null));
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    if (response.ok) throw userFacingError('서버 응답 형식을 확인할 수 없습니다.');
  }
  if (!response.ok || payload?.ok !== true) {
    throw userFacingError(extractApiError(response.status, payload));
  }
  return payload;
}

function postJson(url, body, signal) {
  return requestJson(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }, signal);
}

function isSafeFileName(value) {
  return typeof value === 'string'
    && value.length > 0
    && value.length <= 255
    && !/[\\/\u0000-\u001f\u007f]/u.test(value);
}

function readCompletion(payload) {
  const { outputDir, files } = payload;
  if (
    typeof outputDir !== 'string'
    || outputDir.length === 0
    || outputDir.length > 4_096
    || /[\u0000-\u001f\u007f]/u.test(outputDir)
    || files === null
    || typeof files !== 'object'
    || !isSafeFileName(files.video)
    || !Array.isArray(files.frames)
    || files.frames.length < 3
    || files.frames.length > 5
    || files.frames.some((name) => !isSafeFileName(name))
  ) {
    throw userFacingError('서버가 저장 결과를 올바르게 보내지 않았습니다.');
  }
  return Object.freeze({
    outputDir,
    files: Object.freeze({
      video: files.video,
      frames: Object.freeze([...files.frames]),
    }),
  });
}

function messageForBrowserError(error) {
  if (error?.name === 'UserFacingError') return error.message;
  if (error?.name === 'AbortError') return '작업이 중단되었습니다. 다시 시도해주세요.';
  if (
    typeof error?.message === 'string'
    && error.message.length <= 160
    && /[가-힣]/u.test(error.message)
    && !/[<>]/u.test(error.message)
  ) {
    return error.message;
  }
  return '브라우저에서 작업을 처리하지 못했습니다. 다시 시도해주세요.';
}

async function uploadFrame(jobId, index, selectedFrame, signal) {
  const jpeg = await captureJpegAtTime(video, captureCanvas, selectedFrame.time, { signal });
  return requestJson(buildFrameUploadUrl(jobId, index, selectedFrame.time), {
    method: 'PUT',
    headers: { 'Content-Type': 'image/jpeg' },
    body: jpeg,
  }, signal);
}

async function generateThreadsCopy(jobId, regenerate, signal) {
  const payload = await postJson(buildThreadsCopyUrl(jobId), { regenerate }, signal);
  return validateThreadsCopyResponse(payload);
}

async function runWorkflow(url, signal) {
  const resolved = await postJson(API_ENDPOINTS.resolve, { url }, signal);
  let sessionId;
  try {
    sessionId = requireOpaqueId(resolved.sessionId, 'sessionId');
  } catch {
    throw userFacingError('서버가 올바른 세션 ID를 보내지 않았습니다.');
  }

  workflow = advanceWorkflow(workflow, 'downloading');
  renderWorkflow();
  const created = await postJson(API_ENDPOINTS.jobs, { sessionId }, signal);
  let jobId;
  try {
    jobId = requireOpaqueId(created.jobId, 'jobId');
  } catch {
    throw userFacingError('서버가 올바른 작업 ID를 보내지 않았습니다.');
  }
  const mediaUrl = validateMediaUrl(created.mediaUrl, jobId);

  workflow = advanceWorkflow(workflow, 'analyzing');
  renderWorkflow();
  video.src = mediaUrl;
  video.load();
  await waitForVideoMetadata(video, { signal });
  const selectedFrames = await analyzeVideoFrames(video, analysisCanvas, {
    signal,
    onProgress(done, total) {
      setProgress(55 + (done / total) * 20);
    },
  });

  workflow = advanceWorkflow(workflow, 'savingFrames');
  renderWorkflow();
  for (const [offset, selectedFrame] of selectedFrames.entries()) {
    await uploadFrame(jobId, offset + 1, selectedFrame, signal);
    setProgress(80 + ((offset + 1) / selectedFrames.length) * 15);
  }

  const completed = await postJson(buildCompleteUrl(jobId), {}, signal);
  const result = readCompletion(completed);
  workflow = advanceWorkflow(workflow, 'mediaSaved', { result, jobId });
  renderWorkflow();

  releaseMediaResources(video, [analysisCanvas, captureCanvas]);
  workflow = advanceWorkflow(workflow, 'generatingCopy');
  renderWorkflow();
  const copyResult = await generateThreadsCopy(jobId, false, signal);
  workflow = advanceWorkflow(workflow, 'complete', { copyResult });
}

function handleWorkflowFailure(controller, error) {
  if ((pageIsLeaving || lifecycleAborts.has(controller)) && controller.signal.aborted) return;
  if (!controller.signal.aborted) controller.abort();
  const message = messageForBrowserError(error);
  workflow = workflow.phase === 'generatingCopy'
    ? failCopyWorkflow(workflow, message)
    : failWorkflow(workflow, message);
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (activeController !== null) {
    activeController.abort();
    return;
  }
  if (!form.reportValidity()) return;

  const url = urlInput.value.trim();
  workflow = resetWorkflow();
  clipboardStatus.textContent = '';
  activeController = new AbortController();
  const controller = activeController;
  workflow = advanceWorkflow(workflow, 'resolving');
  renderWorkflow();

  try {
    await runWorkflow(url, controller.signal);
  } catch (error) {
    handleWorkflowFailure(controller, error);
  } finally {
    releaseMediaResources(video, [analysisCanvas, captureCanvas]);
    if (activeController === controller) activeController = null;
    renderWorkflow();
  }
});

regenerateButton.addEventListener('click', async () => {
  if (activeController !== null || !['complete', 'copyError'].includes(workflow.phase)) return;

  activeController = new AbortController();
  const controller = activeController;
  workflow = advanceWorkflow(workflow, 'generatingCopy');
  clipboardStatus.textContent = '새 문구를 생성하고 있습니다.';
  renderWorkflow();

  try {
    const copyResult = await generateThreadsCopy(workflow.jobId, true, controller.signal);
    workflow = advanceWorkflow(workflow, 'complete', { copyResult });
    clipboardStatus.textContent = '새 문구를 생성하고 파일로 저장했습니다.';
  } catch (error) {
    handleWorkflowFailure(controller, error);
  } finally {
    if (activeController === controller) activeController = null;
    renderWorkflow();
  }
});

window.addEventListener('pagehide', () => {
  pageIsLeaving = true;
  if (activeController) {
    lifecycleAborts.add(activeController);
    activeController.abort();
  }
  releaseMediaResources(video, [analysisCanvas, captureCanvas]);
});

window.addEventListener('pageshow', () => {
  pageIsLeaving = false;
  if (activeController) {
    lifecycleAborts.add(activeController);
    activeController.abort();
    activeController = null;
  }
  workflow = restoreWorkflowAfterPageShow(workflow);
  renderWorkflow();
});

document.documentElement.dataset.appReady = 'true';
renderWorkflow();
