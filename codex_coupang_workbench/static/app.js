import { StudioMediaCapture } from './studio-media.js';
import {
  applyPublishRetryPresentation,
  availableStudioSteps,
  createStudioStore,
  createWorkflowRequestCoordinator,
  isPublishPayloadLocked,
  isSelectedProfileConnected,
  publishRetryPresentation,
  resolvePreviewProfile,
  safeThreadsOAuthUrl,
  tabIndexForNavigationKey,
} from './studio-state.js';

const APP_BASE_PATH = window.location.pathname.startsWith('/threads-copas') ? '/threads-copas' : '';
const STEP_META = Object.freeze({
  account: ['STEP 01', '먼저 발행 계정을 골라주세요'],
  product: ['STEP 02', '콘텐츠로 만들 상품을 찾아보세요'],
  copy: ['STEP 03', '가장 눈에 들어오는 문구를 골라주세요'],
  rednote: ['STEP 04', '상품 맥락에 맞는 RedNote 영상을 찾아보세요'],
  media: ['STEP 05', '영상 또는 대표 이미지를 구성하세요'],
  publish: ['STEP 06', '게시되는 모든 내용을 마지막으로 확인하세요'],
});

const store = createStudioStore();
const mediaCapture = new StudioMediaCapture();
const workflowRequests = createWorkflowRequestCoordinator();
const busyOwners = new WeakMap();
const STALE_WORKFLOW_RESPONSE = Symbol('stale workflow response');
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(`${APP_BASE_PATH}${path}`, {
    cache: "no-store",
    ...options,
    headers,
  });
  if (!response.ok) {
    let detail = '';
    try {
      const payload = await response.json();
      detail = formatApiError(payload?.detail || payload?.error, '');
    } catch {
      detail = '';
    }
    const error = new Error(detail || `요청을 처리하지 못했습니다. (${response.status})`);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return {};
  return response.json();
}

function formatApiError(detail, fallback = '') {
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (typeof detail !== 'object') return fallback;
  if (detail.code === 'PUBLISH_OUTCOME_UNKNOWN') {
    return detail.message || '게시 결과를 확인할 수 없어 자동 재시도를 중단했습니다.';
  }
  if (detail.threads_post_id && !detail.threads_reply_id) {
    return 'Threads 본문은 발행됐지만 댓글 발행에 실패했습니다.';
  }
  return detail.message || detail.error || fallback;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function safeImageUrl(value) {
  try {
    const parsed = new URL(String(value || ''));
    return parsed.protocol === 'https:' ? parsed.href : '';
  } catch {
    return '';
  }
}

function safeExternalUrl(value) {
  return safeImageUrl(value);
}

function safeMediaUrl(value) {
  const raw = String(value || '');
  if (raw.startsWith('/api/')) return `${APP_BASE_PATH}${raw}`;
  if (APP_BASE_PATH && raw.startsWith(`${APP_BASE_PATH}/api/`)) return raw;
  return safeImageUrl(raw);
}

function showMessage(selector, message = '') {
  const element = $(selector);
  if (element) element.textContent = message;
}

function showToast(message) {
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = message;
  $('#toast-region').append(toast);
  window.setTimeout(() => toast.remove(), 3200);
}

async function withBusy(button, busyLabel, action) {
  const original = button?.textContent || '';
  const owner = Symbol('busy owner');
  if (button) {
    busyOwners.set(button, owner);
    button.disabled = true;
    button.textContent = busyLabel;
  }
  try {
    return await action();
  } finally {
    if (button && busyOwners.get(button) === owner) {
      busyOwners.delete(button);
      button.disabled = false;
      button.textContent = original;
    }
  }
}

function invalidateWorkflowRequests() {
  workflowRequests.invalidate();
  mediaCapture.abort();
}

async function runWorkflowRequest(key, action) {
  const request = workflowRequests.begin(key);
  try {
    const response = await action(request);
    return request.isCurrent() ? response : STALE_WORKFLOW_RESPONSE;
  } catch (error) {
    if (!request.isCurrent() || error?.name === 'AbortError') return STALE_WORKFLOW_RESPONSE;
    throw error;
  } finally {
    request.finish();
  }
}

function selectedProfile() {
  const state = store.getState();
  return state.profiles.find((profile) => profile.profile_key === state.selectedProfileKey) || null;
}

function selectedVariant() {
  const state = store.getState();
  return state.variants.find((variant) => variant.id === state.selectedVariantId) || null;
}

function draftVariants() {
  return store.getState().variants;
}

function saveSelectedThreadVariant() {
  return selectedVariant();
}

function selectedRedNoteResult() {
  const state = store.getState();
  return state.rednoteResults.find((result) => result.result_id === state.selectedRednoteResultId) || null;
}

function focusStudioDestination(step, target) {
  const element = target === 'panel'
    ? $(`[data-studio-panel="${step}"]`)
    : $(`[data-studio-step="${step}"]`);
  if (!element) return;
  if (target === 'panel') element.tabIndex = -1;
  element.focus();
}

function activeStep(step, { focusTarget = '' } = {}) {
  const state = store.getState();
  const available = availableStudioSteps(state);
  if (!available.has(step) && step !== state.activeStep) {
    showToast('이전 단계의 선택을 먼저 완료해주세요.');
    return;
  }
  store.setActiveStep(step);
  if (focusTarget) focusStudioDestination(step, focusTarget);
  if (step === 'rednote') {
    prefillRedNoteSourceQuery();
  }
  if (step === 'publish' && state.jobId && !state.preview) {
    void loadStudioPreview();
  }
}

function renderNavigation(state) {
  const available = availableStudioSteps(state);
  const completed = completedSteps(state);
  $$('[data-studio-step]').forEach((button) => {
    const step = button.dataset.studioStep;
    const isActive = step === state.activeStep;
    button.classList.toggle('is-active', isActive);
    button.classList.toggle('is-complete', completed.has(step));
    button.setAttribute('aria-selected', String(isActive));
    button.tabIndex = isActive ? 0 : -1;
    button.disabled = !available.has(step) && !isActive;
  });
  $$('[data-studio-panel]').forEach((panel) => {
    const isActive = panel.dataset.studioPanel === state.activeStep;
    panel.hidden = !isActive;
    panel.classList.toggle('is-active', isActive);
  });
  const [label, title] = STEP_META[state.activeStep];
  $('#current-step-label').textContent = label;
  $('#current-step-title').textContent = title;
  $('#current-job-badge').textContent = state.jobId
    ? `작업 ${state.jobId.slice(0, 8)}`
    : '아직 저장된 작업 없음';
}

function completedSteps(state) {
  const steps = new Set();
  if (isSelectedProfileConnected(state)) steps.add('account');
  if (state.selectedProduct) steps.add('product');
  if (state.selectedVariantId) steps.add('copy');
  if (state.selectedRednoteResultId || state.assets.length) steps.add('rednote');
  if (state.mediaMode && state.selectedAssetIds.length) steps.add('media');
  if (state.preview?.job?.publish_stage === 'published') steps.add('publish');
  return steps;
}

function renderSummary(state) {
  const profile = selectedProfile();
  const variant = selectedVariant();
  const rednote = selectedRedNoteResult();
  $('#summary-profile').textContent = profile?.display_name || '선택 전';
  $('#summary-product').textContent = state.selectedProduct?.product_name || '선택 전';
  $('#summary-copy').textContent = variant?.label || variant?.persona_label || '선택 전';
  $('#summary-rednote').textContent = rednote?.title || (state.assets.length ? '영상 저장 완료' : '선택 전');
  $('#summary-media').textContent = state.mediaMode === 'video'
    ? '영상 1개'
    : state.mediaMode === 'images'
      ? `이미지 ${state.selectedAssetIds.length}장`
      : state.mediaMode === 'mixed'
        ? '영상 1개 · 이미지 1장'
      : '선택 전';
  const count = completedSteps(state).size;
  $('#summary-progress-bar').style.width = `${Math.round((count / 6) * 100)}%`;
  $('#summary-progress-label').textContent = `${count} / 6 단계 완료`;
}

function renderState(state) {
  renderNavigation(state);
  renderSummary(state);
  renderProfiles(state);
  renderProducts(state);
  renderVariants(state);
  renderRedNoteResults(state);
  renderMediaAssets(state);
  renderFinalPreview(state);
  renderHistory(state);
  const publishPayloadLocked = isPublishPayloadLocked(state.preview);
  [
    '#generate-copy-button',
    '#regenerate-copy-button',
    '#save-copy-button',
    '#regenerate-query-button',
    '#rednote-search-button',
    '#download-rednote-button',
    '#save-media-selection-button',
  ].forEach((selector) => {
    $(selector).disabled = publishPayloadLocked;
  });
  $$('[data-variant-id], [data-rednote-result-id], [data-asset-id]').forEach((control) => {
    if (publishPayloadLocked) control.disabled = true;
  });
  const profileSelect = $('#threads-profile-select');
  if (profileSelect.value !== state.selectedProfileKey) profileSelect.value = state.selectedProfileKey;
  const redNoteQueryInput = $('#rednote-query');
  const sourceQuery = state.rednoteSourceQuery || coupangRedNoteSourceQuery(state);
  if (redNoteQueryInput.value !== sourceQuery) redNoteQueryInput.value = sourceQuery;
  redNoteQueryInput.disabled = publishPayloadLocked;
  applyPublishRetryPresentation($('#retry-reply-button'), state.preview);
  $('#publish-confirmation').checked = false;
  $('#studio-publish-button').disabled = true;
}

function rejectPublishPayloadEdit(messageSelector) {
  if (!isPublishPayloadLocked(store.getState().preview)) return false;
  showMessage(messageSelector, '게시가 시작된 작업의 본문과 미디어는 변경할 수 없습니다.');
  return true;
}

async function checkHealth() {
  const backend = $('#service-backend-status');
  const rednote = $('#service-rednote-status');
  try {
    const health = await api('/api/health');
    backend.className = 'status-chip is-ok';
    backend.lastChild.textContent = 'Studio 준비됨';
    const rednoteReady = health.rednote === 'ok';
    rednote.className = `status-chip ${rednoteReady ? 'is-ok' : 'is-error'}`;
    rednote.lastChild.textContent = rednoteReady ? 'RedNote 준비됨' : 'RedNote 확인 필요';
  } catch {
    backend.className = 'status-chip is-error';
    backend.lastChild.textContent = 'Studio 오프라인';
    rednote.className = 'status-chip is-error';
    rednote.lastChild.textContent = 'RedNote 확인 불가';
  }
}

async function loadSettings() {
  const settings = await api('/api/settings');
  const form = $('#settings-form');
  for (const [key, value] of Object.entries(settings)) {
    if (form.elements[key]) form.elements[key].value = value;
  }
  renderChannelOptions(settings.coupang_channel_ids || '');
}

async function saveSettings(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  await withBusy(button, '저장 중', async () => {
    try {
      const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
      const saved = await api('/api/settings', { method: 'PUT', body: JSON.stringify(payload) });
      renderChannelOptions(saved.coupang_channel_ids || '');
      showMessage('#settings-message', '저장했습니다.');
    } catch (error) {
      showMessage('#settings-message', error.message);
    }
  });
}

function renderChannelOptions(value) {
  const channels = [...new Set(String(value || '').split(/[\s,]+/u).map((item) => item.trim()).filter(Boolean))];
  $('#coupang-channel-select').innerHTML = channels.length
    ? channels.map((channel) => `<option value="${escapeHtml(channel)}">${escapeHtml(channel)}</option>`).join('')
    : '<option value="">기본 Sub ID</option>';
}

async function loadProfiles() {
  try {
    const profiles = await api('/api/threads/profiles');
    const current = store.getState();
    const selectedProfileStillConnected = profiles.some((profile) => (
      profile.profile_key === current.selectedProfileKey && profile.is_connected
    ));
    const selectionWasInvalidated = Boolean(current.selectedProfileKey && !selectedProfileStillConnected);
    const selectedProfileKey = selectedProfileStillConnected
      ? current.selectedProfileKey
      : (current.jobId ? '' : (profiles.find((profile) => profile.is_connected)?.profile_key || ''));
    if (selectionWasInvalidated) invalidateWorkflowRequests();
    store.patch({
      profiles,
      selectedProfileKey,
      ...(selectionWasInvalidated ? {
        jobId: '',
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
      } : {}),
    });
    const threads = $('#service-threads-status');
    threads.className = profiles.some((profile) => profile.is_connected) ? 'status-chip is-ok' : 'status-chip';
    threads.lastChild.textContent = profiles.some((profile) => profile.is_connected)
      ? 'Threads 연결됨'
      : 'Threads 계정 필요';
  } catch (error) {
    const threads = $('#service-threads-status');
    threads.className = 'status-chip is-error';
    threads.lastChild.textContent = 'Threads 오류';
    showMessage('#threads-profile-message', error.message);
  }
}

function renderProfiles(state) {
  const select = $('#threads-profile-select');
  select.innerHTML = state.profiles.length
    ? '<option value="">계정을 선택하세요</option>' + state.profiles.map((profile) => (
      `<option value="${escapeHtml(profile.profile_key)}"${profile.is_connected ? '' : ' disabled aria-disabled="true"'}>${escapeHtml(profile.display_name || profile.username || profile.profile_key)}${profile.is_connected ? '' : ' · 연결 필요'}</option>`
    )).join('')
    : '<option value="">연결된 계정 없음</option>';
  select.value = state.selectedProfileKey;
  $('#threads-profiles-list').innerHTML = state.profiles.length
    ? state.profiles.map((profile) => `
      <div class="account-row">
        <div><strong>${escapeHtml(profile.display_name || profile.profile_key)}</strong><small>${escapeHtml(profile.username ? `@${profile.username}` : profile.profile_key)} · ${profile.is_connected ? '연결됨' : '연결 필요'}</small></div>
        <div class="account-actions">
          ${profile.is_connected ? `<button class="button button-quiet" type="button" data-profile-action="refresh" data-profile-key="${escapeHtml(profile.profile_key)}">토큰 갱신</button><button class="button button-quiet" type="button" data-profile-action="disconnect" data-profile-key="${escapeHtml(profile.profile_key)}">연결 해제</button>` : ''}
        </div>
      </div>`).join('')
    : '<div class="empty-state"><strong>현재 Threads 계정을 가져와주세요.</strong></div>';
}

async function importProfile(button) {
  await withBusy(button, '인증 여는 중', async () => {
    let popup = null;
    try {
      popup = window.open('about:blank', '_blank');
      if (popup) popup.opener = null;
      const response = await api('/api/threads/auth/import/start');
      const authUrl = safeThreadsOAuthUrl(response.auth_url);
      if (!authUrl) throw new Error('안전한 Threads 인증 주소를 확인하지 못했습니다.');
      if (popup) popup.location.href = authUrl;
      else window.location.href = authUrl;
      showMessage('#threads-profile-message', '인증 후 이 화면으로 돌아오면 계정이 자동으로 표시됩니다.');
    } catch (error) {
      if (popup && !popup.closed) popup.close();
      showMessage('#threads-profile-message', error.message);
    }
  });
}

async function runProfileAction(action, profileKey, button) {
  const path = `/api/threads/profiles/${encodeURIComponent(profileKey)}/${action === 'refresh' ? 'refresh' : 'disconnect'}`;
  await withBusy(button, '처리 중', async () => {
    try {
      await api(path, { method: 'POST', body: '{}' });
      await loadProfiles();
    } catch (error) {
      showMessage('#threads-profile-message', error.message);
    }
  });
}

async function searchProducts(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  const keyword = $('#product-search-input').value.trim();
  if (!keyword) return;
  invalidateWorkflowRequests();
  await withBusy(button, '검색 중', async () => {
    showMessage('#product-message', '쿠팡 상품을 찾고 있습니다.');
    try {
      const response = await runWorkflowRequest('product-search', (request) => (
        api('/api/coupang/products/search', {
          method: 'POST',
          signal: request.signal,
          body: JSON.stringify({ keyword, sub_id: $('#coupang-channel-select').value, limit: 10 }),
        })
      ));
      if (response === STALE_WORKFLOW_RESPONSE) return;
      store.patch({
        productKeyword: keyword,
        productResults: response.products,
        selectedProduct: null,
        jobId: '',
        variants: [],
        selectedVariantId: '',
        commentText: '',
        rednoteSourceQuery: keyword,
        rednoteQuery: '',
        rednoteQueryGeneration: 0,
        rednoteSearchId: '',
        rednoteResults: [],
        selectedRednoteResultId: '',
        assets: [],
        mediaMode: '',
        selectedAssetIds: [],
        preview: null,
      });
      showMessage('#product-message', response.products.length ? `${response.products.length}개를 찾았습니다.` : '검색 결과가 없습니다.');
    } catch (error) {
      showMessage('#product-message', error.message);
    }
  });
}

function renderProducts(state) {
  $('#product-results').innerHTML = state.productResults.length
    ? state.productResults.map((product) => {
      const selected = state.selectedProduct?.product_id === product.product_id;
      const image = safeImageUrl(product.image_url);
      return `
        <article class="product-card${selected ? ' is-selected' : ''}">
          ${image ? `<img src="${escapeHtml(image)}" alt="" loading="lazy" referrerpolicy="no-referrer" />` : '<div></div>'}
          <button class="select-card-button" type="button" data-product-id="${escapeHtml(product.product_id)}" aria-pressed="${selected}">
            <span class="product-card-body"><span class="card-title">${escapeHtml(product.product_name)}</span><span class="card-price">${Number(product.price || 0).toLocaleString('ko-KR')}원</span><span class="card-meta">${product.is_rocket ? '로켓배송 · ' : ''}선택하여 다음 단계로</span></span>
          </button>
        </article>`;
    }).join('')
    : '<div class="empty-state"><strong>검색 결과가 여기에 표시됩니다.</strong><span>정확한 상품명을 입력하면 선택지가 더 좋아집니다.</span></div>';
}

function selectProduct(productId) {
  const product = store.getState().productResults.find((candidate) => candidate.product_id === productId);
  if (!product) return;
  selectWorkflowProduct(product);
}

function selectWorkflowProduct(product) {
  invalidateWorkflowRequests();
  store.patch({
    jobId: '',
    selectedProduct: product,
    variants: [],
    selectedVariantId: '',
    commentText: '',
    rednoteQuery: '',
    rednoteQueryGeneration: 0,
    rednoteSearchId: '',
    rednoteResults: [],
    selectedRednoteResultId: '',
    assets: [],
    mediaMode: '',
    selectedAssetIds: [],
    preview: null,
  });
  showMessage('#product-message', `"${product.product_name}"을 선택했습니다.`);
}

async function useManualProduct(button) {
  const productUrl = $('#manual-product-url').value.trim();
  const productName = $('#product-name-fallback').value.trim();
  if (!productUrl) {
    showMessage('#product-message', '상품 URL을 입력해주세요.');
    return;
  }
  invalidateWorkflowRequests();
  await withBusy(button, '상품 확인 중', async () => {
    try {
      const preview = await runWorkflowRequest('manual-product', (request) => (
        api('/api/coupang/product-preview', {
          method: 'POST',
          signal: request.signal,
          body: JSON.stringify({ product_url: productUrl, product_name: productName, sub_id: $('#coupang-channel-select').value }),
        })
      ));
      if (preview === STALE_WORKFLOW_RESPONSE) return;
      const product = {
        product_id: preview.product_id,
        product_name: preview.product_name || productName,
        product_url: preview.original_url || productUrl,
        partner_url: preview.partner_url || '',
        image_url: preview.image_url || '',
        facts: preview.facts || [],
        price: 0,
        is_rocket: false,
      };
      const productResults = [
        product,
        ...store.getState().productResults.filter((candidate) => candidate.product_url !== product.product_url),
      ];
      store.patch({ productResults });
      selectWorkflowProduct(product);
      showMessage('#product-message', `"${product.product_name}"을 선택했습니다.`);
    } catch (error) {
      showMessage('#product-message', error.message);
    }
  });
}

async function useChromeProductContext(button) {
  const productUrl = $('#manual-product-url').value.trim();
  if (!productUrl) {
    showMessage('#product-message', 'Chrome에서 확인할 쿠팡 URL을 입력해주세요.');
    return;
  }
  await withBusy(button, 'Chrome 확인 중', async () => {
    try {
      const context = await api('/api/coupang/chrome-product-context', {
        method: 'POST',
        body: JSON.stringify({ product_url: productUrl }),
      });
      $('#product-name-fallback').value = context.product_name || '';
      showMessage('#product-message', context.product_name
        ? `Chrome에서 "${context.product_name}"을 확인했습니다.`
        : 'Chrome에서 상품명을 확인하지 못했습니다. 직접 입력해주세요.');
    } catch (error) {
      showMessage('#product-message', error.message);
    }
  });
}

function createCodexThreadsPrompt(product = store.getState().selectedProduct) {
  if (!product) return '';
  const facts = Array.isArray(product.facts) && product.facts.length
    ? product.facts.slice(0, 6).map((fact) => `- ${fact}`).join('\n')
    : '- 자동 수집된 상세 정보 없음';
  return [
    '실제 사용 맥락이 자연스럽게 이어지는 Threads 본문을 작성해줘.',
    '독자가 겪어봤을 법한 구체적인 불편이나 순간으로 시작해.',
    '해결 방법을 바로 밝히지 않는 방식으로 궁금증을 남겨.',
    '문장 연결과 인과관계를 우선해.',
    '억지 반전, 랜덤 비유, 과장된 결과를 만들지 마.',
    '실제로 사용했다는 1인칭 후기나 가족의 반응을 지어내지 마.',
    '각 줄은 자연스러운 한국어로 의미를 완결해.',
    '링크, 광고 고지, 해시태그, 정확한 상품명은 본문에 넣지 마.',
    '1~2개 짧은 문장, 공백과 문장부호를 포함해 90자 이내의 자연스러운 한국어로 작성해.',
    '가능하면 45~75자로 끝내고 같은 맥락을 반복하지 마.',
    '~요, ~습니다, ~세요 같은 높임말 종결 없이 짧은 반말 구어체로 써.',
    '',
    `내부 참고용 상품명: ${product.product_name || ''}`,
    `쿠팡 URL: ${product.partner_url || product.product_url || ''}`,
    '상품 정보:',
    facts,
  ].join('\n');
}

async function generateCopy({ regenerate = false } = {}) {
  const state = store.getState();
  if (rejectPublishPayloadEdit('#copy-message')) return;
  const product = state.selectedProduct;
  if (!product) {
    showMessage('#copy-message', '상품을 먼저 선택해주세요.');
    return;
  }
  if (!isSelectedProfileConnected(state)) {
    showMessage('#copy-message', '연결된 Threads 계정을 먼저 선택해주세요.');
    return;
  }
  invalidateWorkflowRequests();
  const button = regenerate ? $('#regenerate-copy-button') : $('#generate-copy-button');
  await withBusy(button, '문구 만드는 중', async () => {
    showMessage('#copy-message', 'Codex가 페르소나별 문구를 만들고 있습니다.');
    try {
      const currentVariant = selectedVariant();
      const configuredPrompt = $('#settings-form').elements.codex_threads_prompt?.value.trim() || '';
      const payload = {
        job_id: regenerate ? state.jobId : '',
        profile_key: state.selectedProfileKey,
        product_url: product.product_url,
        partner_url: product.partner_url || '',
        product_name: product.product_name,
        coupang_channel_id: $('#coupang-channel-select').value,
        facts: product.facts || [],
        custom_persona: $('#custom-persona-input').value.trim(),
        codex_threads_prompt: configuredPrompt || createCodexThreadsPrompt(product),
        regenerate_persona_keys: regenerate && currentVariant ? [currentVariant.persona_key] : [],
      };
      const response = await runWorkflowRequest('draft', (request) => (
        api('/api/threads/draft', {
          method: 'POST',
          signal: request.signal,
          body: JSON.stringify(payload),
        })
      ));
      if (response === STALE_WORKFLOW_RESPONSE) return;
      store.patch({
        jobId: response.job.id,
        selectedProduct: { ...product, product_name: response.job.product_name, product_url: response.job.product_url },
        variants: response.variants,
        selectedVariantId: response.selected_variant_id,
        commentText: response.comment_text,
        rednoteQuery: '',
        rednoteQueryGeneration: 0,
        rednoteSearchId: '',
        rednoteResults: [],
        selectedRednoteResultId: '',
        assets: [],
        mediaMode: '',
        selectedAssetIds: [],
        preview: null,
      });
      showMessage('#copy-message', `${response.variants.length}개 버전을 만들었습니다.`);
    } catch (error) {
      showMessage('#copy-message', error.message);
    }
  });
}

function renderVariants(state) {
  $('#persona-grid').innerHTML = state.variants.length
    ? state.variants.map((variant) => {
      const selected = variant.id === state.selectedVariantId;
      return `<article class="persona-card${selected ? ' is-selected' : ''}"><button type="button" data-variant-id="${escapeHtml(variant.id)}" aria-pressed="${selected}"><span class="persona-label">${escapeHtml(variant.label || variant.persona_label || variant.persona_key)}<small>${variant.generation > 1 ? `${variant.generation}번째` : '첫 생성'}</small></span><span class="persona-copy">${escapeHtml(variant.text || variant.body || '')}</span></button></article>`;
    }).join('')
    : '<div class="empty-state"><strong>상품을 선택하면 6가지 문구를 만듭니다.</strong><span>짧고 직접적인 호기심 중심 문구로 구성됩니다.</span></div>';
  const variant = selectedVariant();
  $('#selected-copy-editor').hidden = !variant;
  $('#selected-copy-text').value = variant?.text || variant?.body || '';
}

async function selectThreadVariant(variantId) {
  const state = store.getState();
  if (rejectPublishPayloadEdit('#copy-message')) return;
  if (!state.jobId || !state.variants.some((variant) => variant.id === variantId)) return;
  saveSelectedThreadVariant();
  invalidateWorkflowRequests();
  try {
    const response = await runWorkflowRequest('copy-selection', (request) => (
      api(`/api/jobs/${encodeURIComponent(state.jobId)}/copy-selection`, {
        method: 'PATCH',
        signal: request.signal,
        body: JSON.stringify({ variant_id: variantId }),
      })
    ));
    if (response === STALE_WORKFLOW_RESPONSE) return;
    store.patch({
      selectedVariantId: response.selected_variant_id || variantId,
      variants: response.variants || state.variants,
      rednoteQuery: '',
      rednoteQueryGeneration: 0,
      rednoteSearchId: '',
      rednoteResults: [],
      selectedRednoteResultId: '',
      assets: [],
      mediaMode: '',
      selectedAssetIds: [],
      preview: null,
    });
  } catch (error) {
    showMessage('#copy-message', error.message);
  }
}

async function saveCopyEdit(button) {
  const state = store.getState();
  const variant = selectedVariant();
  const body = $('#selected-copy-text').value.trim();
  if (rejectPublishPayloadEdit('#copy-message')) return;
  if (!state.jobId || !variant || !body) {
    showMessage('#copy-message', '수정할 본문을 입력해주세요.');
    return;
  }
  await withBusy(button, '본문 저장 중', async () => {
    try {
      const response = await runWorkflowRequest('copy-edit', (request) => (
        api(`/api/jobs/${encodeURIComponent(state.jobId)}/copy-variants/${encodeURIComponent(variant.id)}`, {
          method: 'PATCH',
          signal: request.signal,
          body: JSON.stringify({ body }),
        })
      ));
      if (response === STALE_WORKFLOW_RESPONSE) return;
      store.patch({
        selectedVariantId: response.selected_variant_id || state.selectedVariantId,
        variants: response.variants || state.variants,
      });
      if (state.preview) await loadStudioPreview();
      showMessage('#copy-message', '수정한 본문을 저장했습니다.');
    } catch (error) {
      showMessage('#copy-message', error.message);
    }
  });
}

function coupangRedNoteSourceQuery(state = store.getState()) {
  return String(state.productKeyword || state.selectedProduct?.product_name || '').trim();
}

function prefillRedNoteSourceQuery() {
  const state = store.getState();
  if (state.rednoteSourceQuery.trim()) return;
  const sourceQuery = coupangRedNoteSourceQuery(state);
  if (sourceQuery) store.patch({ rednoteSourceQuery: sourceQuery });
}

function clearRedNoteSearchState(sourceQuery) {
  store.patch({
    rednoteSourceQuery: sourceQuery,
    rednoteQuery: '',
    rednoteQueryGeneration: 0,
    rednoteSearchId: '',
    rednoteResults: [],
    selectedRednoteResultId: '',
    assets: [],
    mediaMode: '',
    selectedAssetIds: [],
    preview: null,
  });
}

function resetRedNoteSourceQuery() {
  const state = store.getState();
  if (rejectPublishPayloadEdit('#rednote-message')) return;
  invalidateWorkflowRequests();
  const sourceQuery = coupangRedNoteSourceQuery(state);
  clearRedNoteSearchState(sourceQuery);
  showMessage('#rednote-message', sourceQuery ? '쿠팡 검색어를 다시 입력했습니다.' : '쿠팡 검색어를 먼저 입력해주세요.');
}

function updateRedNoteSourceQuery(event) {
  if (rejectPublishPayloadEdit('#rednote-message')) {
    event.currentTarget.value = store.getState().rednoteSourceQuery;
    return;
  }
  invalidateWorkflowRequests();
  clearRedNoteSearchState(event.currentTarget.value);
}

async function searchRedNote(button) {
  const state = store.getState();
  if (rejectPublishPayloadEdit('#rednote-message')) return;
  const sourceQuery = $('#rednote-query').value.trim();
  if (!state.jobId || !sourceQuery) {
    showMessage('#rednote-message', 'RedNote에서 찾을 검색어를 입력해주세요.');
    return;
  }
  invalidateWorkflowRequests();
  await withBusy(button, '중국어 번역·검색 중', async () => {
    showMessage('#rednote-message', '입력한 검색어를 중국어로 번역한 뒤 새 Chrome 탭에서 영상을 찾고 있습니다.');
    try {
      const response = await runWorkflowRequest('rednote-search', async (request) => {
        const query = await api(`/api/jobs/${encodeURIComponent(state.jobId)}/rednote-query`, {
          method: 'POST',
          signal: request.signal,
          body: JSON.stringify({
            source_keyword: sourceQuery,
            product_facts: state.selectedProduct?.facts || [],
          }),
        });
        const search = await api(`/api/jobs/${encodeURIComponent(state.jobId)}/rednote-search`, {
          method: 'POST',
          signal: request.signal,
          body: '{}',
        });
        return { query, search };
      });
      if (response === STALE_WORKFLOW_RESPONSE) return;
      const { query, search } = response;
      store.patch({
        rednoteSourceQuery: sourceQuery,
        rednoteQuery: query.query,
        rednoteQueryGeneration: query.generation,
        rednoteSearchId: search.search_id,
        rednoteResults: search.results,
        selectedRednoteResultId: '',
        assets: [],
        mediaMode: '',
        selectedAssetIds: [],
        preview: null,
      });
      showMessage(
        '#rednote-message',
        search.results.length
          ? `“${sourceQuery}” → “${query.query}” · ${search.results.length}개 영상을 찾았습니다.`
          : `“${sourceQuery}” → “${query.query}” · 영상 결과가 없습니다.`,
      );
    } catch (error) {
      showMessage('#rednote-message', error.message);
    }
  });
}

function renderRedNoteResults(state) {
  $('#rednote-results').innerHTML = state.rednoteResults.length
    ? state.rednoteResults.map((result) => {
      const selected = result.result_id === state.selectedRednoteResultId;
      const image = safeImageUrl(result.thumbnail_url);
      return `<article class="rednote-card${selected ? ' is-selected' : ''}"><button class="select-card-button" type="button" data-rednote-result-id="${escapeHtml(result.result_id)}" aria-pressed="${selected}">${image ? `<img src="${escapeHtml(image)}" alt="" loading="lazy" referrerpolicy="no-referrer" />` : '<div class="empty-state"><span>미리보기 없음</span></div>'}<span class="rednote-card-body"><span class="card-title">${escapeHtml(result.title || '제목 없음')}</span><span class="card-description">${escapeHtml(result.description || '')}</span><span class="card-meta">${escapeHtml(result.creator || 'RedNote')}</span></span></button></article>`;
    }).join('')
    : '<div class="empty-state"><strong>Chrome에서 찾은 영상만 여기에 표시됩니다.</strong><span>로그인이 풀렸다면 Chrome에서 RedNote에 먼저 로그인해주세요.</span></div>';
}

function chooseRedNoteResult(resultId) {
  if (rejectPublishPayloadEdit('#rednote-message')) return;
  if (!store.getState().rednoteResults.some((result) => result.result_id === resultId)) return;
  invalidateWorkflowRequests();
  store.patch({ selectedRednoteResultId: resultId, assets: [], mediaMode: '', selectedAssetIds: [], preview: null });
}

async function downloadRedNote(button) {
  const state = store.getState();
  if (rejectPublishPayloadEdit('#rednote-message')) return;
  const result = selectedRedNoteResult();
  if (!result) {
    showMessage('#rednote-message', '다운로드할 영상을 선택해주세요.');
    return;
  }
  invalidateWorkflowRequests();
  await withBusy(button, '영상 다운로드 중', async () => {
    try {
      const completed = await runWorkflowRequest('rednote-download', async (request) => {
        await api(`/api/jobs/${encodeURIComponent(state.jobId)}/rednote-download`, {
          method: 'POST',
          signal: request.signal,
          body: JSON.stringify({ search_id: state.rednoteSearchId, result_id: result.result_id, note_id: result.note_id }),
        });
        if (!request.isCurrent()) return null;
        $('#representative-status').textContent = '대표 장면 분석 중';
        showMessage('#rednote-message', '영상을 받았습니다. 중요한 장면을 고르고 있습니다.');
        return mediaCapture.start({
          jobId: state.jobId,
          basePath: APP_BASE_PATH,
          onProgress(progress) {
            if (!request.isCurrent()) return;
            $('#representative-status').textContent = progress.phase === 'analyzing'
              ? `장면 분석 ${progress.completed}/${progress.total}`
              : `JPG 저장 ${progress.completed}/${progress.total}`;
          },
        });
      });
      if (completed === STALE_WORKFLOW_RESPONSE) return;
      const assets = completed.assets || [];
      store.patch({ assets, mediaMode: 'mixed', selectedAssetIds: defaultMixedAssetIds(assets) });
      $('#representative-status').textContent = `대표 장면 ${completed.frame_count}장 저장됨`;
      activeStep('media');
    } catch (error) {
      $('#representative-status').textContent = '대표 장면 실패';
      showMessage('#rednote-message', error.name === 'AbortError' ? '이전 작업을 중단했습니다.' : error.message);
    }
  });
}

function renderMediaAssets(state) {
  $('#media-assets').innerHTML = state.assets.length
    ? state.assets.map((asset) => {
      const selected = state.selectedAssetIds.includes(asset.id);
      const isVideo = asset.asset_type === 'video';
      const disabled = !state.mediaMode;
      const media = isVideo
        ? `<video src="${escapeHtml(safeMediaUrl(asset.url))}" muted playsinline preload="metadata"></video>`
        : `<img src="${escapeHtml(safeMediaUrl(asset.url))}" alt="${Math.round((asset.timestamp_ms || 0) / 1000)}초 대표 장면" loading="lazy" />`;
      return `<label class="media-card${selected ? ' is-selected' : ''}"><input type="checkbox" data-asset-id="${escapeHtml(asset.id)}" ${selected ? 'checked' : ''} ${disabled ? 'disabled' : ''} />${media}<span class="media-card-body"><span class="card-title">${isVideo ? '게시 영상' : `대표 이미지 ${Math.round((asset.timestamp_ms || 0) / 1000)}초`}</span><span class="card-meta">${isVideo ? '영상 1개 선택' : 'JPG 이미지 1장 선택'}</span></span></label>`;
    }).join('')
    : '<div class="empty-state"><strong>영상을 선택하면 자동으로 대표 장면을 고릅니다.</strong><span>서로 다른 장면을 3~5장 JPG로 저장합니다.</span></div>';
}

function defaultMixedAssetIds(assets) {
  return [
    assets.find((asset) => asset.asset_type === 'video'),
    assets.find((asset) => asset.asset_type === 'frame'),
  ].filter(Boolean).map((asset) => asset.id);
}

function toggleMediaAsset(assetId, checked) {
  const state = store.getState();
  if (rejectPublishPayloadEdit('#media-message')) return;
  const asset = state.assets.find((candidate) => candidate.id === assetId);
  if (!asset) return;
  let ids = checked
    ? [...state.selectedAssetIds.filter((id) => state.assets.find((candidate) => candidate.id === id)?.asset_type !== asset.asset_type), assetId]
    : state.selectedAssetIds.filter((id) => id !== assetId);
  ids = [...new Set(ids)];
  invalidateWorkflowRequests();
  store.patch({ selectedAssetIds: ids, preview: null });
}

async function saveMediaSelection(button) {
  const state = store.getState();
  if (rejectPublishPayloadEdit('#media-message')) return;
  if (state.mediaMode !== 'mixed' || state.selectedAssetIds.length !== 2) {
    showMessage('#media-message', '영상 1개와 JPG 이미지 1장을 각각 선택해주세요.');
    return;
  }
  invalidateWorkflowRequests();
  await withBusy(button, '선택 저장 중', async () => {
    try {
      const response = await runWorkflowRequest('media-selection', (request) => (
        api(`/api/jobs/${encodeURIComponent(state.jobId)}/media-selection`, {
          method: 'PATCH',
          signal: request.signal,
          body: JSON.stringify({ mode: state.mediaMode, asset_ids: state.selectedAssetIds }),
        })
      ));
      if (response === STALE_WORKFLOW_RESPONSE) return;
      store.patch({ assets: response.assets, selectedAssetIds: response.selected_asset_ids, mediaMode: response.media_mode });
      await loadStudioPreview();
      activeStep('publish');
    } catch (error) {
      showMessage('#media-message', error.message);
    }
  });
}

async function loadStudioPreview() {
  const state = store.getState();
  if (!state.jobId) return;
  try {
    const preview = await runWorkflowRequest('studio-preview', (request) => (
      api(`/api/jobs/${encodeURIComponent(state.jobId)}/studio-preview`, { signal: request.signal })
    ));
    if (preview === STALE_WORKFLOW_RESPONSE) return;
    store.patch({ preview });
  } catch (error) {
    showMessage('#publish-message', error.message);
  }
}

function renderFinalPreview(state) {
  const container = $('#final-preview');
  const preview = state.preview;
  if (!preview) {
    container.innerHTML = '<div class="empty-state"><strong>앞 단계를 완료하면 최종 미리보기가 열립니다.</strong></div>';
    return;
  }
  const job = preview.job || {};
  const variant = preview.selected_copy_variant || selectedVariant() || {};
  const assets = preview.selected_rednote_assets || state.assets.filter((asset) => state.selectedAssetIds.includes(asset.id));
  const profile = resolvePreviewProfile(state.profiles, preview, state.selectedProfileKey);
  const body = preview.text || variant.body || variant.text || '';
  const comment = preview.comment_text || state.commentText || '';
  const media = assets.map((asset) => asset.asset_type === 'video'
    ? `<video src="${escapeHtml(safeMediaUrl(asset.url || `/api/jobs/${state.jobId}/rednote-video`))}" controls muted playsinline preload="metadata"></video>`
    : `<img src="${escapeHtml(safeMediaUrl(asset.url || `/api/jobs/${state.jobId}/rednote-assets/${asset.id}`))}" alt="게시할 대표 장면" />`).join('');
  container.innerHTML = `
    <div class="preview-account"><span class="eyebrow">게시 계정</span><strong>${escapeHtml(profile.display_name || profile.username || profile.profile_key || state.selectedProfileKey)}</strong></div>
    <div class="preview-post"><span class="eyebrow">Threads 본문</span><p>${escapeHtml(body)}</p></div>
    <div class="preview-media">${media}</div>
    <div class="preview-comment"><span class="eyebrow">잠금된 첫 댓글</span><p>${escapeHtml(comment || job.product_url || '')}</p></div>`;
}

async function publishStudio(button, retry = false) {
  const state = store.getState();
  if (!state.jobId || !state.preview) return;
  const retryPresentation = publishRetryPresentation(state.preview);
  if (retry && !retryPresentation.visible) return;
  if (!retry && !$('#publish-confirmation').checked) {
    showMessage('#publish-message', '게시 전 최종 확인에 체크해주세요.');
    button.disabled = true;
    return;
  }
  await withBusy(button, retry ? retryPresentation.busyLabel : 'Threads 게시 중', async () => {
    try {
      const response = await runWorkflowRequest('threads-publish', (request) => (
        api(`/api/jobs/${encodeURIComponent(state.jobId)}/${retry ? 'threads-media-retry' : 'threads-media-publish'}`, {
          method: 'POST',
          signal: request.signal,
          body: '{}',
        })
      ));
      if (response === STALE_WORKFLOW_RESPONSE) return;
      store.patch({ preview: { ...state.preview, ...response, job: response.job || state.preview.job } });
      showMessage('#publish-message', response.partial
        ? '본문은 게시됐고 댓글만 재시도가 필요합니다.'
        : 'Threads 게시가 완료됐습니다.');
      await loadHistory();
    } catch (error) {
      showMessage('#publish-message', error.message);
      await loadStudioPreview();
    }
  });
  applyPublishRetryPresentation($('#retry-reply-button'), store.getState().preview);
  if (!retry) button.disabled = !$('#publish-confirmation').checked;
}

async function loadHistory({ refreshInsights = false } = {}) {
  try {
    const records = await api(
      `/api/threads/publish-records${refreshInsights ? '?refresh_insights=true' : ''}`,
    );
    store.patch({ records });
  } catch {
    // History should not block the creation workflow.
  }
}

async function refreshRecordInsights(jobId) {
  if (!jobId) return;
  await api(`/api/threads/publish-records/${encodeURIComponent(jobId)}/insights`, {
    method: 'POST',
    body: '{}',
  });
  await loadHistory();
}

function renderHistory(state) {
  $('#publish-history-list').innerHTML = state.records.length
    ? state.records.map((record) => {
      const permalink = safeExternalUrl(record.threads_permalink);
      const metrics = `조회수 ${Number(record.threads_views || 0).toLocaleString('ko-KR')} · 좋아요 ${Number(record.threads_likes || 0).toLocaleString('ko-KR')}`;
      const publishedAt = formatKstDateTime(record.threads_published_at || record.updated_at || '');
      const insightsAt = formatKstDateTime(record.threads_insights_at || '');
      return `
        <article class="history-row">
          <div><strong>${escapeHtml(record.product_name || '상품명 없음')}</strong><small>${escapeHtml(record.display_name || record.profile_key || '')} · ${escapeHtml(publishedAt)}</small><small>${escapeHtml(metrics)}${insightsAt ? ` · 지표 ${escapeHtml(insightsAt)}` : ''}</small></div>
          <div class="history-actions"><button class="button button-quiet" type="button" data-refresh-record-insights="${escapeHtml(record.job_id || '')}">지표 갱신</button>${permalink ? `<a class="button button-secondary" href="${escapeHtml(permalink)}" target="_blank" rel="noopener noreferrer">Threads 열기</a>` : ''}<span class="panel-hint">${escapeHtml(record.publish_stage || (record.threads_post_id ? '게시 완료' : '저장됨'))}</span></div>
        </article>`;
    }).join('')
    : '<div class="empty-state"><strong>아직 게시 기록이 없습니다.</strong></div>';
}

function formatKstDateTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('ko-KR', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: "Asia/Seoul",
  }).format(date);
}

async function restoreJob() {
  const state = store.getState();
  if (!state.jobId) return;
  try {
    const preview = await runWorkflowRequest('restore-job', (request) => (
      api(`/api/jobs/${encodeURIComponent(state.jobId)}/studio-preview`, { signal: request.signal })
    ));
    if (preview === STALE_WORKFLOW_RESPONSE) return;
    const job = preview.job || {};
    const variants = preview.copy_variants || [];
    const assets = (preview.rednote_assets || []).map((asset) => ({
      ...asset,
      url: asset.url || (asset.asset_type === 'video'
        ? `${APP_BASE_PATH}/api/jobs/${state.jobId}/rednote-video`
        : `${APP_BASE_PATH}/api/jobs/${state.jobId}/rednote-assets/${asset.id}`),
    }));
    store.patch({
      preview,
      selectedProfileKey: job.selected_profile_key || state.selectedProfileKey,
      selectedProduct: { product_name: job.product_name, product_url: job.product_url, image_url: job.image_url || '', facts: [] },
      variants,
      selectedVariantId: job.selected_copy_variant_id || preview.selected_copy_variant?.id || '',
      rednoteSourceQuery: state.rednoteSourceQuery || state.productKeyword || job.product_name || '',
      rednoteQuery: job.rednote_query || '',
      rednoteQueryGeneration: job.rednote_query_generation || 0,
      assets,
      mediaMode: job.media_mode || '',
      selectedAssetIds: (preview.selected_rednote_assets || []).map((asset) => asset.id),
    });
  } catch (error) {
    if (error.status === 404 || error.status === 410) {
      store.resetWorkflow();
      return;
    }
    showMessage('#publish-message', '저장된 작업을 불러오지 못했습니다. 연결이 복구되면 다시 시도합니다.');
  }
}

function bindEvents() {
  $('#settings-form').addEventListener('submit', saveSettings);
  $('#threads-profile-select').addEventListener('change', (event) => {
    const state = store.getState();
    const selectedProfileKey = event.target.value;
    if (selectedProfileKey === state.selectedProfileKey) return;
    const profile = state.profiles.find((candidate) => candidate.profile_key === selectedProfileKey);
    if (selectedProfileKey && profile?.is_connected !== true) {
      event.target.value = state.selectedProfileKey;
      showMessage('#threads-profile-message', '연결된 Threads 계정만 선택할 수 있습니다.');
      return;
    }
    invalidateWorkflowRequests();
    store.patch({
      selectedProfileKey,
      jobId: '',
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
    });
  });
  $('#threads-import-button').addEventListener('click', (event) => void importProfile(event.currentTarget));
  $('#threads-profiles-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-profile-action]');
    if (button) void runProfileAction(button.dataset.profileAction, button.dataset.profileKey, button);
  });
  $('#product-search-form').addEventListener('submit', searchProducts);
  $('#product-results').addEventListener('click', (event) => {
    const button = event.target.closest('[data-product-id]');
    if (button) selectProduct(button.dataset.productId);
  });
  $('#manual-product-button').addEventListener('click', (event) => void useManualProduct(event.currentTarget));
  $('#coupang-chrome-preview-button').addEventListener('click', (event) => void useChromeProductContext(event.currentTarget));
  $('#generate-copy-button').addEventListener('click', () => void generateCopy());
  $('#regenerate-copy-button').addEventListener('click', () => void generateCopy({ regenerate: true }));
  $('#save-copy-button').addEventListener('click', (event) => void saveCopyEdit(event.currentTarget));
  $('#persona-grid').addEventListener('click', (event) => {
    const button = event.target.closest('[data-variant-id]');
    if (button) void selectThreadVariant(button.dataset.variantId);
  });
  $('#rednote-query').addEventListener('input', updateRedNoteSourceQuery);
  $('#regenerate-query-button').addEventListener('click', resetRedNoteSourceQuery);
  $('#rednote-search-button').addEventListener('click', (event) => void searchRedNote(event.currentTarget));
  $('#rednote-results').addEventListener('click', (event) => {
    const button = event.target.closest('[data-rednote-result-id]');
    if (button) chooseRedNoteResult(button.dataset.rednoteResultId);
  });
  $('#download-rednote-button').addEventListener('click', (event) => void downloadRedNote(event.currentTarget));
  $('#media-assets').addEventListener('change', (event) => {
    const input = event.target.closest('[data-asset-id]');
    if (input) toggleMediaAsset(input.dataset.assetId, input.checked);
  });
  $('#save-media-selection-button').addEventListener('click', (event) => void saveMediaSelection(event.currentTarget));
  $('#publish-confirmation').addEventListener('change', (event) => {
    $('#studio-publish-button').disabled = !event.target.checked;
  });
  $('#studio-publish-button').addEventListener('click', (event) => void publishStudio(event.currentTarget));
  $('#retry-reply-button').addEventListener('click', (event) => void publishStudio(event.currentTarget, true));
  $('#refresh-history-button').addEventListener('click', () => void loadHistory({ refreshInsights: true }));
  $('#publish-history-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-refresh-record-insights]');
    if (button) void refreshRecordInsights(button.dataset.refreshRecordInsights);
  });
  $('#new-workflow-button').addEventListener('click', () => {
    invalidateWorkflowRequests();
    store.resetWorkflow();
    showToast('새 작업을 시작합니다.');
  });
  $$('[data-studio-step]').forEach((button) => {
    button.addEventListener('click', () => activeStep(button.dataset.studioStep));
    button.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      const enabledTabs = $$('[data-studio-step]').filter((candidate) => !candidate.disabled);
      const currentIndex = enabledTabs.indexOf(event.currentTarget);
      if (currentIndex < 0) return;
      const nextIndex = tabIndexForNavigationKey(event.key, currentIndex, enabledTabs.length);
      if (nextIndex === null) return;
      const nextTab = enabledTabs[nextIndex];
      event.preventDefault();
      activeStep(nextTab.dataset.studioStep, { focusTarget: 'tab' });
    });
  });
  $$('[data-next-step]').forEach((button) => button.addEventListener('click', () => (
    activeStep(button.dataset.nextStep, { focusTarget: 'panel' })
  )));
  $$('[data-prev-step]').forEach((button) => button.addEventListener('click', () => (
    activeStep(button.dataset.prevStep, { focusTarget: 'panel' })
  )));
  window.addEventListener('focus', () => void loadProfiles());
  window.addEventListener('beforeunload', invalidateWorkflowRequests);
}

async function init() {
  bindEvents();
  store.subscribe(renderState);
  renderState(store.getState());
  await Promise.allSettled([checkHealth(), loadSettings(), loadProfiles(), loadHistory()]);
  await restoreJob();
  renderState(store.getState());
}

void init();
