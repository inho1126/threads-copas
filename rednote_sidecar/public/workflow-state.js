const PHASES = Object.freeze({
  idle: Object.freeze({ progress: 0, message: 'RedNote 공개 영상 주소를 입력해주세요.' }),
  resolving: Object.freeze({ progress: 10, message: '게시물 정보를 확인하고 있습니다.' }),
  downloading: Object.freeze({ progress: 30, message: '영상을 다운로드하고 있습니다.' }),
  analyzing: Object.freeze({ progress: 55, message: '대표 장면을 자동으로 고르고 있습니다.' }),
  savingFrames: Object.freeze({ progress: 80, message: '대표 장면을 JPG로 저장하고 있습니다.' }),
  mediaSaved: Object.freeze({ progress: 95, message: '영상과 대표 장면 저장이 완료되었습니다.' }),
  generatingCopy: Object.freeze({ progress: 98, message: 'Codex가 쓰레드용 문구를 만들고 있습니다.' }),
  complete: Object.freeze({ progress: 100, message: '저장과 쓰레드 문구 생성이 완료되었습니다.' }),
  copyError: Object.freeze({ progress: 100, message: '미디어는 저장됐지만 문구 생성은 완료하지 못했습니다.' }),
});

const NEXT_PHASES = Object.freeze({
  idle: Object.freeze(['resolving']),
  resolving: Object.freeze(['downloading']),
  downloading: Object.freeze(['analyzing']),
  analyzing: Object.freeze(['savingFrames']),
  savingFrames: Object.freeze(['mediaSaved']),
  mediaSaved: Object.freeze(['generatingCopy']),
  generatingCopy: Object.freeze(['complete']),
  complete: Object.freeze(['generatingCopy']),
  copyError: Object.freeze(['generatingCopy']),
  error: Object.freeze([]),
});

const ACTIVE_MEDIA_PHASES = new Set(['resolving', 'downloading', 'analyzing', 'savingFrames']);
const OPAQUE_ID_PATTERN = /^(?:[a-f0-9]{32,}|[A-Za-z0-9_-]{22,})$/u;

function freezeState({
  phase,
  progress,
  message,
  error = null,
  result = null,
  copyResult = null,
  jobId = null,
}) {
  return Object.freeze({ phase, progress, message, error, result, copyResult, jobId });
}

function requireState(state) {
  if (
    state === null
    || typeof state !== 'object'
    || (!(state.phase in PHASES) && state.phase !== 'error')
  ) {
    throw new TypeError('워크플로 상태가 올바르지 않습니다.');
  }
}

function requireMediaState(result, jobId) {
  if (
    result === null
    || typeof result !== 'object'
    || typeof jobId !== 'string'
    || !OPAQUE_ID_PATTERN.test(jobId)
  ) {
    throw new TypeError('미디어 저장 결과가 올바르지 않습니다.');
  }
}

export function createWorkflowState() {
  return freezeState({ phase: 'idle', ...PHASES.idle });
}

export function advanceWorkflow(state, nextPhase, {
  result,
  copyResult,
  jobId,
} = {}) {
  requireState(state);
  if (!NEXT_PHASES[state.phase]?.includes(nextPhase)) {
    throw new Error(`${state.phase}에서 ${nextPhase}(으)로 상태 전이를 할 수 없습니다.`);
  }

  if (nextPhase === 'mediaSaved') {
    requireMediaState(result, jobId);
    return freezeState({
      phase: nextPhase,
      ...PHASES[nextPhase],
      result,
      jobId,
    });
  }

  if (nextPhase === 'generatingCopy') {
    requireMediaState(state.result, state.jobId);
    return freezeState({
      phase: nextPhase,
      ...PHASES[nextPhase],
      result: state.result,
      copyResult: state.copyResult,
      jobId: state.jobId,
    });
  }

  if (nextPhase === 'complete') {
    requireMediaState(state.result, state.jobId);
    if (copyResult === null || typeof copyResult !== 'object') {
      throw new TypeError('쓰레드 문구 생성 결과가 올바르지 않습니다.');
    }
    return freezeState({
      phase: nextPhase,
      ...PHASES[nextPhase],
      result: state.result,
      copyResult,
      jobId: state.jobId,
    });
  }

  return freezeState({ phase: nextPhase, ...PHASES[nextPhase] });
}

export function failWorkflow(state, error) {
  requireState(state);
  if (typeof error !== 'string' || error.trim() === '') {
    throw new TypeError('오류 안내 문구가 올바르지 않습니다.');
  }
  return freezeState({
    phase: 'error',
    progress: state.progress,
    message: '미디어를 저장하지 못했습니다. 다시 시도할 수 있습니다.',
    error: error.trim(),
  });
}

export function failCopyWorkflow(state, error) {
  requireState(state);
  if (state.phase !== 'generatingCopy') {
    throw new Error(`${state.phase}에서는 문구 생성 실패로 전환할 수 없습니다.`);
  }
  if (typeof error !== 'string' || error.trim() === '') {
    throw new TypeError('오류 안내 문구가 올바르지 않습니다.');
  }
  requireMediaState(state.result, state.jobId);
  return freezeState({
    phase: 'copyError',
    ...PHASES.copyError,
    error: error.trim(),
    result: state.result,
    copyResult: state.copyResult,
    jobId: state.jobId,
  });
}

export function resetWorkflow() {
  return createWorkflowState();
}

export function getWorkflowControls(busy) {
  if (typeof busy !== 'boolean') throw new TypeError('busy must be a boolean');
  return Object.freeze({
    buttonLabel: busy ? '작업 취소' : '영상과 대표 장면 저장',
    buttonDisabled: false,
    inputDisabled: busy,
  });
}

export function restoreWorkflowAfterPageShow(state) {
  requireState(state);
  if (ACTIVE_MEDIA_PHASES.has(state.phase)) return createWorkflowState();
  if (state.phase === 'generatingCopy') {
    return failCopyWorkflow(state, '페이지 이동으로 문구 생성이 중단되었습니다. 다시 생성할 수 있습니다.');
  }
  return state;
}

function isSafeMessage(message) {
  return (
    typeof message === 'string'
    && message.trim().length > 0
    && message.length <= 240
    && !/[<>\u0000-\u001f\u007f]/u.test(message)
    && !/https?:|xsec_|token|signature/iu.test(message)
  );
}

export function extractApiError(status, payload) {
  if (status === 0) {
    return '서버에 연결할 수 없습니다. 로컬 앱이 실행 중인지 확인해주세요.';
  }
  const message = payload?.error?.message;
  if (isSafeMessage(message)) return message.trim();
  const code = Number.isInteger(status) && status >= 100 && status <= 599 ? ` (${status})` : '';
  return `서버 요청에 실패했습니다.${code}`;
}
