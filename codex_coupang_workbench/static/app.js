const state = {
  profiles: [],
  records: [],
  draftJobId: "",
  productPreview: null,
};

const $ = (selector) => document.querySelector(selector);
const APP_BASE_PATH = window.location.pathname.startsWith("/threads-copas") ? "/threads-copas" : "";

async function api(path, options = {}) {
  const url = path.startsWith("/") ? `${APP_BASE_PATH}${path}` : path;
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.text();
    let message = detail || `HTTP ${response.status}`;
    try {
      message = JSON.parse(detail).detail || message;
    } catch (_error) {
      // Keep the raw response body when it is not JSON.
    }
    throw new Error(message);
  }
  return response.json();
}

function formToObject(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function checkHealth() {
  const dot = $("#status-dot");
  const text = $("#health-text");
  try {
    const health = await api("/api/health");
    dot.classList.toggle("ok", health.status === "ok");
    text.textContent = health.status === "ok" ? "backend ready" : "backend unknown";
  } catch (error) {
    dot.classList.remove("ok");
    text.textContent = "backend offline";
  }
}

async function loadSettings() {
  const settings = await api("/api/settings");
  applySettingsToForm(settings, $("#settings-form"));
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const message = $("#settings-message");
  message.textContent = "saving...";
  const settings = await api("/api/settings", {
    method: "PUT",
    body: JSON.stringify(formToObject(form)),
  });
  applySettingsToForm(settings, form);
  message.textContent = "saved";
  clearMessage(message);
}

function applySettingsToForm(settings, form) {
  for (const [key, value] of Object.entries(settings)) {
    if (form.elements[key]) {
      form.elements[key].value = value;
    }
  }
}

async function connectProfile(profileKey) {
  const authWindow = window.open("about:blank", "_blank");
  if (authWindow) {
    authWindow.opener = null;
  }
  try {
    const response = await api(`/api/threads/auth/start?profile_key=${encodeURIComponent(profileKey)}`);
    if (authWindow) {
      authWindow.location.href = response.auth_url;
      waitForProfileConnection(profileKey, authWindow);
    } else {
      window.location.href = response.auth_url;
    }
  } catch (error) {
    console.error(error);
    if (authWindow) {
      authWindow.close();
    }
    $("#threads-profile-message").textContent = "Threads 연결을 시작하지 못했습니다.";
  }
}

async function importCurrentProfile() {
  const authWindow = window.open("about:blank", "_blank");
  if (authWindow) {
    authWindow.opener = null;
  }
  const message = $("#threads-profile-message");
  try {
    message.textContent = "Threads 인증을 여는 중...";
    const response = await api("/api/threads/auth/import/start");
    if (authWindow) {
      authWindow.location.href = response.auth_url;
      waitForProfilesChange(authWindow);
    } else {
      window.location.href = response.auth_url;
    }
  } catch (error) {
    console.error(error);
    if (authWindow) {
      authWindow.close();
    }
    message.textContent = "Threads 계정을 가져오지 못했습니다.";
  }
}

async function refreshProfileToken(profileKey) {
  await api(`/api/threads/profiles/${encodeURIComponent(profileKey)}/refresh`, { method: "POST" });
  await refreshProfiles();
}

async function previewCoupangProduct() {
  const form = $("#threads-draft-form");
  const message = $("#coupang-preview-message");
  const productUrl = form.elements.product_url.value.trim();
  state.productPreview = null;
  form.elements.partner_url.value = "";
  renderProductPreview();
  if (!productUrl) {
    message.textContent = "쿠팡 URL을 입력하세요.";
    return;
  }
  setBusy(true);
  try {
    message.textContent = "상품 확인 중...";
    const preview = await api("/api/coupang/product-preview", {
      method: "POST",
      body: JSON.stringify({ product_url: productUrl }),
    });
    state.productPreview = preview;
    form.elements.partner_url.value = preview.partner_url || "";
    renderProductPreview();
    $("#selected-product-label").textContent = preview.product_name || "selected product";
    if (preview.needs_product_name) {
      message.textContent = "상품명만 직접 입력하면 생성할 수 있습니다.";
    } else {
      message.textContent = "상품 확인 완료";
      clearMessage(message);
    }
  } catch (error) {
    console.error(error);
    message.textContent = error.message || "상품을 확인하지 못했습니다.";
  } finally {
    setBusy(false);
  }
}

async function generateDraft(event) {
  event.preventDefault();
  const message = $("#threads-draft-message");
  const form = event.currentTarget;
  if (!$("#product-name-fallback").hidden && !form.elements.product_name.value.trim()) {
    message.textContent = "상품명을 입력하세요.";
    form.elements.product_name.focus();
    return;
  }
  setBusy(true);
  try {
    message.textContent = "generating...";
    const draft = await api("/api/threads/draft", {
      method: "POST",
      body: JSON.stringify(formToObject(form)),
    });
    state.draftJobId = draft.job.id;
    $("#threads-preview").value = draft.text;
    $("#threads-comment-preview").value = draft.comment_text || "";
    $("#selected-product-label").textContent = draft.job.product_name || "selected product";
    message.textContent = "draft ready";
  } catch (error) {
    console.error(error);
    message.textContent = error.message || "글 생성에 실패했습니다.";
  } finally {
    setBusy(false);
  }
}

async function publishDraft() {
  const message = $("#threads-publish-message");
  const profileKey = $("#threads-profile-select").value;
  const text = $("#threads-preview").value.trim();
  const commentText = $("#threads-comment-preview").value.trim();
  if (!profileKey) {
    message.textContent = "프로필을 선택하세요.";
    return;
  }
  if (!state.draftJobId) {
    message.textContent = "먼저 글을 생성하세요.";
    return;
  }
  if (!text) {
    message.textContent = "발행할 글이 비어 있습니다.";
    return;
  }
  setBusy(true);
  try {
    message.textContent = "publishing...";
    const published = await api("/api/threads/publish", {
      method: "POST",
      body: JSON.stringify({
        profile_key: profileKey,
        job_id: state.draftJobId,
        text,
        comment_text: commentText,
      }),
    });
    message.textContent = `published: ${published.threads_post_id}`;
    state.draftJobId = "";
    $("#threads-preview").value = "";
    $("#threads-comment-preview").value = "";
    $("#selected-product-label").textContent = "no product";
    await refreshRecords();
  } finally {
    setBusy(false);
  }
}

async function refreshProfiles() {
  state.profiles = await api("/api/threads/profiles");
  renderProfiles();
}

async function refreshRecords() {
  state.records = await api("/api/threads/publish-records");
  renderRecords();
}

async function refreshAll() {
  await Promise.all([refreshProfiles(), refreshRecords()]);
}

function renderProductPreview() {
  const container = $("#coupang-product-preview");
  const preview = state.productPreview;
  const fallback = $("#product-name-fallback");
  if (!preview) {
    container.hidden = true;
    container.innerHTML = "";
    fallback.hidden = true;
    $("#threads-draft-form").elements.partner_url.value = "";
    return;
  }
  const facts = Array.isArray(preview.facts) ? preview.facts.filter(Boolean) : [];
  fallback.hidden = !preview.needs_product_name;
  if (!preview.needs_product_name) {
    $("#threads-draft-form").elements.product_name.value = "";
  }
  container.hidden = false;
  container.innerHTML = `
    <div class="product-preview-thumb">
      ${
        preview.image_url
          ? `<img src="${escapeAttribute(preview.image_url)}" alt="${escapeAttribute(preview.product_name || "쿠팡 상품")}" />`
          : '<div class="product-preview-placeholder">이미지 없음</div>'
      }
    </div>
    <div class="product-preview-info">
      <strong>${escapeHtml(preview.product_name || "상품명 자동 확인 필요")}</strong>
      ${preview.product_id ? `<span class="link-text">상품 ID: ${escapeHtml(preview.product_id)}</span>` : ""}
      ${preview.item_id ? `<span class="link-text">Item ID: ${escapeHtml(preview.item_id)}</span>` : ""}
      ${facts.length ? `<span class="link-text">${facts.map(escapeHtml).join(" · ")}</span>` : ""}
      ${preview.partner_url ? `<span class="link-text">${escapeHtml(preview.partner_url)}</span>` : ""}
      ${
        preview.needs_product_name
          ? '<span class="link-text">쿠팡 API가 상품명을 정확히 반환하지 않아 상품명만 직접 확인합니다.</span>'
          : ""
      }
    </div>
  `;
}

function renderProfiles() {
  $("#threads-profile-count").textContent = `${state.profiles.length} profiles`;
  const list = $("#threads-profiles-list");
  if (state.profiles.length === 0) {
    list.innerHTML = '<div class="empty-cell">Import Current Account로 Threads 계정을 연결하세요.</div>';
  } else {
    list.innerHTML = state.profiles
      .map((profile) => {
        const username = profile.username ? `@${profile.username}` : profile.profile_key;
        const status = profile.is_connected ? "connected" : "not connected";
        return `
          <div class="profile-row">
            <div>
              <strong>${escapeHtml(profile.display_name)}</strong>
              <span class="link-text">${escapeHtml(username)} · ${escapeHtml(status)}</span>
              ${profile.expires_at ? `<span class="link-text">token until ${escapeHtml(profile.expires_at)}</span>` : ""}
            </div>
            <div class="job-actions">
              <button class="small-button" type="button" data-action="connect" data-key="${escapeAttribute(profile.profile_key)}">Connect</button>
              ${
                profile.is_connected
                  ? `<button class="small-button" type="button" data-action="refresh-token" data-key="${escapeAttribute(profile.profile_key)}">Refresh Token</button>`
                  : ""
              }
            </div>
          </div>
        `;
      })
      .join("");
  }
  renderProfileOptions();
}

function renderProfileOptions() {
  const select = $("#threads-profile-select");
  const current = select.value;
  if (state.profiles.length === 0) {
    select.innerHTML = '<option value="">프로필 없음</option>';
    return;
  }
  select.innerHTML = state.profiles
    .map((profile) => {
      const suffix = profile.is_connected ? "" : " · 연결 필요";
      return `<option value="${escapeAttribute(profile.profile_key)}">${escapeHtml(profile.display_name)}${suffix}</option>`;
    })
    .join("");
  if (current && state.profiles.some((profile) => profile.profile_key === current)) {
    select.value = current;
  }
}

function renderRecords() {
  $("#record-count").textContent = `${state.records.length} records`;
  const body = $("#records-body");
  if (state.records.length === 0) {
    body.innerHTML = '<tr><td colspan="4" class="empty-cell">No publish records yet.</td></tr>';
    return;
  }
  body.innerHTML = state.records
    .map((record) => {
      const profileName = record.display_name || record.profile_key || "";
      const username = record.username ? `@${record.username}` : "";
      return `
        <tr>
          <td>${escapeHtml(record.threads_published_at || "")}</td>
          <td>
            <strong>${escapeHtml(record.product_name || "상품명 없음")}</strong>
            <span class="link-text">${escapeHtml(record.product_url || "")}</span>
          </td>
          <td>
            <strong>${escapeHtml(profileName)}</strong>
            ${username ? `<span class="link-text">${escapeHtml(username)}</span>` : ""}
          </td>
          <td><span class="link-text">${escapeHtml(record.threads_post_id || "")}</span></td>
        </tr>
      `;
    })
    .join("");
}

function setBusy(isBusy) {
  document.querySelectorAll("button").forEach((button) => {
    button.disabled = isBusy;
  });
}

function clearMessage(element) {
  setTimeout(() => {
    element.textContent = "";
  }, 1800);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

function bindEvents() {
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#threads-import-button").addEventListener("click", importCurrentProfile);
  $("#coupang-preview-button").addEventListener("click", previewCoupangProduct);
  $("#threads-draft-form").elements.product_url.addEventListener("input", () => {
    state.productPreview = null;
    state.draftJobId = "";
    $("#selected-product-label").textContent = "no product";
    $("#product-name-fallback").hidden = true;
    $("#threads-draft-form").elements.product_name.value = "";
    $("#threads-draft-form").elements.partner_url.value = "";
    renderProductPreview();
  });
  $("#threads-draft-form").addEventListener("submit", generateDraft);
  $("#threads-publish-button").addEventListener("click", publishDraft);
  $("#refresh-button").addEventListener("click", refreshAll);
  $("#threads-profiles-list").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    if (button.dataset.action === "connect") {
      await connectProfile(button.dataset.key);
    }
    if (button.dataset.action === "refresh-token") {
      await refreshProfileToken(button.dataset.key);
    }
  });
  window.addEventListener("focus", () => {
    refreshAll().catch((error) => console.error(error));
  });
}

function waitForProfileConnection(profileKey, authWindow) {
  let attempts = 0;
  const timer = setInterval(async () => {
    attempts += 1;
    if (attempts > 60 || authWindow.closed) {
      clearInterval(timer);
    }
    try {
      await refreshProfiles();
      const profile = state.profiles.find((item) => item.profile_key === profileKey);
      if (profile?.is_connected) {
        clearInterval(timer);
        $("#threads-profile-message").textContent = "Threads 연결 완료";
        clearMessage($("#threads-profile-message"));
      }
    } catch (error) {
      console.error(error);
    }
  }, 2000);
}

function waitForProfilesChange(authWindow) {
  const initialConnections = new Map(
    state.profiles.map((profile) => [profile.profile_key, profile.is_connected])
  );
  let attempts = 0;
  const timer = setInterval(async () => {
    attempts += 1;
    if (attempts > 60 || authWindow.closed) {
      clearInterval(timer);
    }
    try {
      await refreshProfiles();
      const imported = state.profiles.find(
        (profile) => profile.is_connected && initialConnections.get(profile.profile_key) !== true
      );
      if (imported) {
        clearInterval(timer);
        $("#threads-profile-message").textContent = `${imported.display_name} 가져오기 완료`;
        clearMessage($("#threads-profile-message"));
      }
    } catch (error) {
      console.error(error);
    }
  }, 2000);
}

async function init() {
  bindEvents();
  await checkHealth();
  await loadSettings();
  await refreshAll();
}

init().catch((error) => {
  console.error(error);
  $("#health-text").textContent = "startup error";
});
