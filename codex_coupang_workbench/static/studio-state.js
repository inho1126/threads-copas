const STORAGE_KEY = 'coupang-rednote-studio:v1';
const STEPS = Object.freeze(['account', 'product', 'copy', 'rednote', 'media', 'publish']);

function emptyState() {
  return {
    activeStep: 'account',
    jobId: '',
    profiles: [],
    selectedProfileKey: '',
    productKeyword: '',
    productResults: [],
    selectedProduct: null,
    variants: [],
    selectedVariantId: '',
    commentText: '',
    rednoteSourceQuery: '',
    rednoteQuery: '',
    rednoteQueryGeneration: 0,
    rednoteSearchId: '',
    rednoteResults: [],
    selectedRednoteResultId: '',
    assets: [],
    mediaMode: '',
    selectedAssetIds: [],
    preview: null,
    records: [],
  };
}

function serializableState(value) {
  const clean = emptyState();
  if (!value || typeof value !== 'object' || Array.isArray(value)) return clean;
  const stringFields = [
    'activeStep', 'jobId', 'selectedProfileKey', 'productKeyword', 'selectedVariantId',
    'commentText', 'rednoteSourceQuery', 'rednoteQuery', 'rednoteSearchId', 'selectedRednoteResultId', 'mediaMode',
  ];
  const arrayFields = ['profiles', 'productResults', 'variants', 'rednoteResults', 'assets', 'selectedAssetIds', 'records'];
  for (const key of stringFields) {
    if (typeof value[key] === 'string') clean[key] = value[key];
  }
  for (const key of arrayFields) {
    if (Array.isArray(value[key])) clean[key] = value[key];
  }
  if (Number.isInteger(value.rednoteQueryGeneration) && value.rednoteQueryGeneration >= 0) {
    clean.rednoteQueryGeneration = value.rednoteQueryGeneration;
  }
  if (value.selectedProduct === null || (typeof value.selectedProduct === 'object' && !Array.isArray(value.selectedProduct))) {
    clean.selectedProduct = value.selectedProduct;
  }
  if (value.preview === null || (typeof value.preview === 'object' && !Array.isArray(value.preview))) {
    clean.preview = value.preview;
  }
  if (!STEPS.includes(clean.activeStep)) clean.activeStep = 'account';
  return clean;
}

function readStoredState(storage) {
  try {
    return serializableState(JSON.parse(storage?.getItem?.(STORAGE_KEY) || '{}'));
  } catch {
    return emptyState();
  }
}

function writeStoredState(storage, state) {
  try {
    storage?.setItem?.(STORAGE_KEY, JSON.stringify(serializableState(state)));
  } catch {
    // The studio still works when private browsing blocks localStorage.
  }
}

export function availableStudioSteps(state) {
  const available = new Set(['account']);
  const profileConnected = isSelectedProfileConnected(state);
  if (profileConnected) available.add('product');
  if (profileConnected && state.selectedProduct) available.add('copy');
  if (profileConnected && state.selectedVariantId) available.add('rednote');
  if (profileConnected && (state.selectedRednoteResultId || state.assets.length)) available.add('media');
  if (profileConnected && state.preview && state.mediaMode && state.selectedAssetIds.length) available.add('publish');
  return available;
}

export function isSelectedProfileConnected(state) {
  const profileKey = String(state?.selectedProfileKey || '');
  return Boolean(profileKey && state?.profiles?.some((profile) => (
    profile?.profile_key === profileKey && profile?.is_connected === true
  )));
}

export function isPublishPayloadLocked(preview) {
  return preview?.job?.publish_locked === true;
}

export function createWorkflowRequestCoordinator() {
  let revision = 0;
  const active = new Map();

  return Object.freeze({
    get revision() {
      return revision;
    },
    begin(key) {
      const cleanKey = String(key || '').trim();
      if (!cleanKey) throw new TypeError('workflow request key is required');
      active.get(cleanKey)?.abort();
      const controller = new AbortController();
      const requestRevision = revision;
      active.set(cleanKey, controller);
      return Object.freeze({
        signal: controller.signal,
        revision: requestRevision,
        isCurrent() {
          return revision === requestRevision
            && active.get(cleanKey) === controller
            && !controller.signal.aborted;
        },
        finish() {
          if (active.get(cleanKey) === controller) active.delete(cleanKey);
        },
      });
    },
    invalidate() {
      revision += 1;
      for (const controller of active.values()) controller.abort();
      active.clear();
      return revision;
    },
  });
}

export function publishRetryPresentation(preview) {
  const job = preview?.job || {};
  const failed = (preview?.publish_stage || job.publish_stage) === 'failed';
  const retryable = preview?.retryable !== false && job.retryable !== false;
  if (!failed || !retryable) {
    return { visible: false, kind: '', label: '', busyLabel: '' };
  }
  const postId = String(preview?.threads_post_id || job.threads_post_id || '').trim();
  const replyId = String(preview?.threads_reply_id || job.threads_reply_id || '').trim();
  const replyOnly = preview?.partial === true || Boolean(postId && !replyId);
  return replyOnly
    ? { visible: true, kind: 'reply', label: '댓글 다시 게시', busyLabel: '댓글 재시도 중' }
    : { visible: true, kind: 'body', label: '본문 다시 게시', busyLabel: '본문 재시도 중' };
}

export function applyPublishRetryPresentation(control, preview) {
  const presentation = publishRetryPresentation(preview);
  control.hidden = !presentation.visible;
  control.textContent = presentation.label;
  control.dataset.retryKind = presentation.kind;
  return presentation;
}

export function resolvePreviewProfile(profiles, preview, fallbackProfileKey = '') {
  const previewProfile = preview?.profile && typeof preview.profile === 'object'
    ? preview.profile
    : {};
  const profileKey = String(
    previewProfile.profile_key
    || preview?.job?.selected_profile_key
    || preview?.profile_key
    || fallbackProfileKey
    || '',
  );
  const savedProfile = Array.isArray(profiles)
    ? profiles.find((profile) => profile?.profile_key === profileKey)
    : null;
  return { ...(savedProfile || {}), ...previewProfile, profile_key: profileKey };
}

export function safeThreadsOAuthUrl(value) {
  try {
    const parsed = new URL(String(value || ''));
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.port) return '';
    const hostname = parsed.hostname.toLowerCase();
    const isThreadsHost = new Set([
      'threads.net',
      'www.threads.net',
      'threads.com',
      'www.threads.com',
    ]).has(hostname);
    const isMetaHost = new Set(['facebook.com', 'www.facebook.com']).has(hostname);
    const validThreadsPath = /^\/oauth\/authorize\/?$/u.test(parsed.pathname);
    const validMetaPath = /^(?:\/v\d+(?:\.\d+)?)?\/dialog\/oauth\/?$/u.test(parsed.pathname);
    return ((isThreadsHost && validThreadsPath) || (isMetaHost && validMetaPath))
      ? parsed.href
      : '';
  } catch {
    return '';
  }
}

export function tabIndexForNavigationKey(key, currentIndex, length) {
  if (!Number.isInteger(currentIndex) || !Number.isInteger(length) || length <= 0) return null;
  if (key === 'Home') return 0;
  if (key === 'End') return length - 1;
  if (key === 'ArrowLeft' || key === 'ArrowUp') return Math.max(0, currentIndex - 1);
  if (key === 'ArrowRight' || key === 'ArrowDown') return Math.min(length - 1, currentIndex + 1);
  return null;
}

export function createStudioStore(storage = globalThis.localStorage) {
  let state = readStoredState(storage);
  const listeners = new Set();

  function notify() {
    writeStoredState(storage, state);
    for (const listener of listeners) listener(state);
  }

  return Object.freeze({
    getState() {
      return state;
    },
    patch(update) {
      if (!update || typeof update !== 'object' || Array.isArray(update)) {
        throw new TypeError('state update must be an object');
      }
      state = serializableState({ ...state, ...update });
      notify();
      return state;
    },
    setActiveStep(step) {
      if (!STEPS.includes(step)) throw new TypeError('unknown studio step');
      state = { ...state, activeStep: step };
      notify();
    },
    resetWorkflow() {
      const { profiles, selectedProfileKey, records } = state;
      state = { ...emptyState(), profiles, selectedProfileKey, records };
      notify();
    },
    subscribe(listener) {
      if (typeof listener !== 'function') throw new TypeError('listener must be a function');
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  });
}
