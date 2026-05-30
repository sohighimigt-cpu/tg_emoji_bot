"use strict";

const tg = window.Telegram?.WebApp ?? null;

const ALLOWED_EXTENSIONS = [
  ".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mov",
];
const VIDEO_EXTENSIONS = [".mp4", ".webm", ".mov"];
const TERMINAL_STATUSES = ["done", "failed", "cancelled"];
const POLL_INTERVAL_MS = 2500;
const THEME_STORAGE_KEY = "emoji-pack-theme";

const state = {
  user: null,
  orientationOptions: {},
  gridOptionsByOrientation: {},
  orientation: null,
  gridCode: null,
  previewUrl: null,
  currentJob: null,
  pollTimer: null,
  submitting: false,
};

const els = {};

function cacheEls() {
  els.form = document.querySelector("[data-form]");
  els.user = document.querySelector("[data-user]");
  els.fileInput = document.querySelector("[data-file]");
  els.dropzone = document.querySelector("[data-dropzone]");
  els.dropzoneEmpty = document.querySelector("[data-dropzone-empty]");
  els.preview = document.querySelector("[data-preview]");
  els.previewMedia = document.querySelector("[data-preview-media]");
  els.fileName = document.querySelector("[data-file-name]");
  els.clear = document.querySelector("[data-clear]");
  els.config = document.querySelector("[data-config]");
  els.title = document.querySelector("[data-title]");
  els.orientationGroup = document.querySelector("[data-orientation-group]");
  els.gridGroup = document.querySelector("[data-grid-group]");
  els.submit = document.querySelector("[data-submit]");
  els.status = document.querySelector("[data-status]");
  els.result = document.querySelector("[data-result]");
  els.tabButtons = [...document.querySelectorAll("[data-tab]")];
  els.views = [...document.querySelectorAll("[data-view]")];
  els.history = document.querySelector("[data-history]");
  els.historyRefresh = document.querySelector("[data-history-refresh]");
  els.themeGroup = document.querySelector("[data-theme-group]");
}

/* ---------- Тема ---------- */
function getThemePref() {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY) || "auto";
  } catch {
    return "auto";
  }
}

function applyTheme() {
  const pref = getThemePref();
  let scheme;
  if (pref === "light" || pref === "dark") {
    scheme = pref;
  } else {
    scheme = tg?.colorScheme === "dark" ? "dark" : "light";
  }
  document.documentElement.dataset.theme = scheme;
}

function renderThemeControls() {
  const pref = getThemePref();
  els.themeGroup?.querySelectorAll("[data-theme-value]").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.themeValue === pref);
  });
}

function setThemePref(value) {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, value);
  } catch {
    /* ignore */
  }
  applyTheme();
  renderThemeControls();
}

function bindThemeControls() {
  els.themeGroup?.querySelectorAll("[data-theme-value]").forEach((btn) => {
    btn.addEventListener("click", () => setThemePref(btn.dataset.themeValue));
  });
}

/* ---------- Вкладки ---------- */
function setActiveTab(name) {
  els.tabButtons.forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.tab === name);
  });
  els.views.forEach((view) => {
    view.hidden = view.dataset.view !== name;
  });
  if (name === "history") {
    loadHistory();
  }
}

function bindTabs() {
  els.tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => setActiveTab(btn.dataset.tab));
  });
}

/* ---------- API ---------- */
function getInitData() {
  return tg?.initData || "";
}

function ensureInitData() {
  const initData = getInitData();
  if (!initData) {
    throw new Error("Откройте приложение из Telegram.");
  }
  return initData;
}

async function apiFetch(path, options = {}) {
  const initData = ensureInitData();
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `TelegramInitData ${initData}`);

  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");
  const payload = isJson ? await response.json() : await response.text();

  if (!response.ok) {
    const message =
      (isJson && (payload.detail || payload.error)) ||
      (typeof payload === "string" && payload) ||
      `Ошибка ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

async function authMiniApp() {
  const initData = ensureInitData();
  const response = await fetch("/api/miniapp/auth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ init_data: initData }),
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || payload.detail || "Не удалось авторизоваться.");
  }
  return payload;
}

function createJob(body) {
  return apiFetch("/api/miniapp/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function uploadFile(publicId, file) {
  const formData = new FormData();
  formData.append("file", file);
  return apiFetch(`/api/miniapp/jobs/${publicId}/upload`, {
    method: "POST",
    body: formData,
  });
}

function startJob(publicId) {
  return apiFetch(`/api/miniapp/jobs/${publicId}/start`, { method: "POST" });
}

function fetchJob(publicId) {
  return apiFetch(`/api/miniapp/jobs/${publicId}`, { method: "GET" });
}

function fetchHistory() {
  return apiFetch("/api/miniapp/history", { method: "GET" });
}

/* ---------- Экранирование ---------- */
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* ---------- Пользователь и статус ---------- */
function renderUser(user) {
  state.user = user;
  els.user.textContent =
    user?.display_name || user?.username || (user?.id ? `ID ${user.id}` : "—");
}

function renderStatus(message, kind = "info") {
  if (!message) {
    els.status.hidden = true;
    return;
  }
  els.status.hidden = false;
  els.status.textContent = message;
  els.status.dataset.kind = kind;
}

function statusLabel(status) {
  const map = {
    draft: "черновик",
    queued: "в очереди",
    processing: "обрабатывается",
    ready: "готова к обработке",
    done: "готово",
    failed: "ошибка",
    cancelled: "отменено",
  };
  return map[status] || status;
}

/* ---------- Ориентация и сетка ---------- */
function renderOrientationOptions() {
  els.orientationGroup.innerHTML = "";
  Object.entries(state.orientationOptions).forEach(([code, label]) => {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "pill";
    pill.textContent = label;
    pill.dataset.value = code;
    pill.addEventListener("click", () => selectOrientation(code));
    els.orientationGroup.appendChild(pill);
  });
}

function selectOrientation(code) {
  state.orientation = code;
  [...els.orientationGroup.children].forEach((pill) => {
    pill.classList.toggle("is-active", pill.dataset.value === code);
  });
  renderGridOptions(code);
}

function renderGridOptions(orientation) {
  const options = state.gridOptionsByOrientation[orientation] || {};
  els.gridGroup.innerHTML = "";
  state.gridCode = null;

  const codes = Object.keys(options);
  Object.entries(options).forEach(([code, label]) => {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "pill";
    pill.textContent = label;
    pill.dataset.value = code;
    pill.addEventListener("click", () => selectGrid(code));
    els.gridGroup.appendChild(pill);
  });

  if (codes.length > 0) {
    selectGrid(codes[0]);
  } else {
    updateSubmitState();
  }
}

function selectGrid(code) {
  state.gridCode = code;
  [...els.gridGroup.children].forEach((pill) => {
    pill.classList.toggle("is-active", pill.dataset.value === code);
  });
  updateSubmitState();
}

/* ---------- Файл и превью ---------- */
function hasAllowedExtension(name) {
  const lower = (name || "").toLowerCase();
  return ALLOWED_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function isVideo(name) {
  const lower = (name || "").toLowerCase();
  return VIDEO_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function clearPreview() {
  if (state.previewUrl) {
    URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = null;
  }
  const prev = els.previewMedia.querySelector("img, video");
  if (prev) prev.remove();
  els.fileName.textContent = "";
  els.preview.hidden = true;
  els.dropzoneEmpty.hidden = false;
  els.dropzone.classList.remove("has-file");
  els.config.hidden = true;
  updateSubmitState();
}

function renderPreview(file) {
  if (state.previewUrl) {
    URL.revokeObjectURL(state.previewUrl);
  }
  const prev = els.previewMedia.querySelector("img, video");
  if (prev) prev.remove();

  state.previewUrl = URL.createObjectURL(file);

  let media;
  if (isVideo(file.name)) {
    media = document.createElement("video");
    media.src = state.previewUrl;
    media.muted = true;
    media.autoplay = true;
    media.loop = true;
    media.playsInline = true;
    media.setAttribute("muted", "");
    media.setAttribute("playsinline", "");
    media.preload = "metadata";
  } else {
    media = document.createElement("img");
    media.src = state.previewUrl;
    media.alt = file.name;
  }
  els.previewMedia.insertBefore(media, els.fileName);
  if (media.tagName === "VIDEO") {
    media.play?.().catch(() => {});
  }
  els.fileName.textContent = file.name;

  els.dropzoneEmpty.hidden = true;
  els.preview.hidden = false;
  els.dropzone.classList.add("has-file");
  els.config.hidden = false;
  updateSubmitState();
}

function handleFile(file) {
  if (!file) {
    clearPreview();
    return;
  }
  if (!hasAllowedExtension(file.name)) {
    els.fileInput.value = "";
    clearPreview();
    renderStatus(
      "Неподдерживаемый формат. Разрешены JPG, PNG, WEBP, GIF, MP4, WEBM, MOV.",
      "error"
    );
    return;
  }
  renderPreview(file);
  renderStatus("", "info");
}

function setDroppedFile(fileList) {
  if (!fileList || fileList.length === 0) return;
  const file = fileList[0];
  if (!hasAllowedExtension(file.name)) {
    renderStatus(
      "Неподдерживаемый формат. Разрешены JPG, PNG, WEBP, GIF, MP4, WEBM, MOV.",
      "error"
    );
    return;
  }
  const dataTransfer = new DataTransfer();
  dataTransfer.items.add(file);
  els.fileInput.files = dataTransfer.files;
  handleFile(file);
}

function bindDropzone() {
  const openPicker = () => els.fileInput.click();

  els.dropzone.addEventListener("click", (event) => {
    if (event.target.closest("[data-clear]")) return;
    openPicker();
  });

  els.dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openPicker();
    }
  });

  els.clear.addEventListener("click", (event) => {
    event.stopPropagation();
    els.fileInput.value = "";
    clearPreview();
  });

  els.fileInput.addEventListener("change", () => {
    handleFile(els.fileInput.files?.[0] || null);
  });

  ["dragenter", "dragover"].forEach((type) => {
    els.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      els.dropzone.classList.add("is-dragover");
    });
  });

  ["dragleave", "dragend", "drop"].forEach((type) => {
    els.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      els.dropzone.classList.remove("is-dragover");
    });
  });

  els.dropzone.addEventListener("drop", (event) => {
    setDroppedFile(event.dataTransfer?.files);
  });
}

/* ---------- Создание пака ---------- */
function getFile() {
  return els.fileInput.files?.[0] || null;
}

function isFormComplete() {
  return Boolean(
    getFile() &&
    els.title.value.trim() &&
    state.orientation &&
    state.gridCode
  );
}

function updateSubmitState() {
  if (state.submitting) {
    els.submit.disabled = true;
    return;
  }
  els.submit.disabled = !isFormComplete();
}

function setSubmitting(flag) {
  state.submitting = flag;
  els.submit.textContent = flag ? "Создаём…" : "Создать пак";
  updateSubmitState();
}

async function handleSubmit(event) {
  event.preventDefault();
  if (state.submitting) return;
  if (!isFormComplete()) return;

  const file = getFile();
  const body = {
    title: els.title.value.trim(),
    orientation: state.orientation,
    grid_code: state.gridCode,
  };

  setSubmitting(true);
  stopPolling();
  els.result.hidden = true;

  try {
    renderStatus("Создаём задачу…", "info");
    const created = await createJob(body);

    renderStatus("Загружаем файл…", "info");
    await uploadFile(created.public_id, file);

    renderStatus("Ставим в очередь…", "info");
    const started = await startJob(created.public_id);

    state.currentJob = started;
    renderResult(started);
    renderStatus(`Задача создана. Статус: ${statusLabel(started.status)}`, "info");
    startPolling(created.public_id);
  } catch (err) {
    renderStatus(err.message || "Не удалось создать пак.", "error");
  } finally {
    setSubmitting(false);
  }
}

/* ---------- Polling и результат ---------- */
function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function startPolling(publicId) {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      const job = await fetchJob(publicId);
      state.currentJob = job;
      renderResult(job);

      if (TERMINAL_STATUSES.includes(job.status)) {
        stopPolling();
        if (job.status === "done") {
          renderStatus("Готово! Пак собран.", "success");
        } else if (job.status === "failed") {
          renderStatus(job.error_message || "Задача завершилась с ошибкой.", "error");
        } else {
          renderStatus("Задача отменена.", "info");
        }
      } else {
        renderStatus(`Статус: ${statusLabel(job.status)}…`, "info");
      }
    } catch (err) {
      stopPolling();
      renderStatus(err.message || "Ошибка обновления статуса.", "error");
    }
  }, POLL_INTERVAL_MS);
}

function renderResult(job) {
  const rows = [
    ["Название", job.title || "—"],
    ["Статус", statusLabel(job.status)],
    ["Ориентация", state.orientationOptions[job.orientation] || job.orientation || "—"],
    ["Сетка", job.grid_code || "—"],
    ["Short name", job.short_name || "—"],
  ];

  const rowsHtml = rows
    .map(
      ([key, value]) =>
        `<div class="result__row"><span class="result__key">${escapeHtml(key)}</span><span class="result__value">${escapeHtml(String(value))}</span></div>`
    )
    .join("");

  const linkHtml = job.pack_url
    ? `<a class="btn btn--primary btn--block result__link" href="${escapeHtml(job.pack_url)}" target="_blank" rel="noopener">Открыть пак в Telegram</a>`
    : "";

  els.result.innerHTML = rowsHtml + linkHtml;
  els.result.hidden = false;
}

/* ---------- История ---------- */
function historyBadgeKind(status) {
  if (status === "done") return "success";
  if (status === "failed" || status === "cancelled") return "error";
  return "info";
}

function formatDate(value) {
  if (!value) return "";
  const iso = value.includes("T") ? value : value.replace(" ", "T") + "Z";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString();
}

async function loadHistory() {
  els.history.innerHTML = `<p class="history__empty">Загрузка…</p>`;
  try {
    const data = await fetchHistory();
    renderHistory(data.items || []);
  } catch (err) {
    els.history.innerHTML = `<p class="history__empty">${escapeHtml(
      err.message || "Не удалось загрузить историю."
    )}</p>`;
  }
}

function renderHistory(items) {
  if (!items.length) {
    els.history.innerHTML = `<p class="history__empty">Пока нет созданных паков.</p>`;
    return;
  }
  els.history.innerHTML = items
    .map((item) => {
      const kind = historyBadgeKind(item.status);
      const meta = [
        state.orientationOptions[item.orientation] || item.orientation,
        item.grid_code,
        formatDate(item.created_at),
      ]
        .filter(Boolean)
        .map((part) => escapeHtml(String(part)))
        .join(" · ");
      const link = item.pack_url
        ? `<a class="btn btn--primary btn--sm history__link" href="${escapeHtml(
            item.pack_url
          )}" target="_blank" rel="noopener">Открыть пак</a>`
        : "";
      return `<article class="history__item"><div class="history__row"><h3 class="history__title">${escapeHtml(
        item.title || "Без названия"
      )}</h3><span class="badge" data-kind="${kind}">${escapeHtml(
        statusLabel(item.status)
      )}</span></div><p class="history__meta">${meta}</p>${link}</article>`;
    })
    .join("");
}

/* ---------- Старт ---------- */
function bindUi() {
  els.form.addEventListener("submit", handleSubmit);
  els.title.addEventListener("input", updateSubmitState);
  bindDropzone();
  bindTabs();
  bindThemeControls();
  els.historyRefresh?.addEventListener("click", loadHistory);
}

async function bootstrap() {
  cacheEls();
  applyTheme();
  renderThemeControls();

  if (tg) {
    tg.ready();
    tg.expand();
    tg.onEvent?.("themeChanged", applyTheme);
  }

  bindUi();
  setActiveTab("create");
  updateSubmitState();

  try {
    const auth = await authMiniApp();
    renderUser(auth.user);
    state.orientationOptions = auth.orientation_options || {};
    state.gridOptionsByOrientation = auth.grid_options_by_orientation || {};

    renderOrientationOptions();
    const firstOrientation = Object.keys(state.orientationOptions)[0];
    if (firstOrientation) {
      selectOrientation(firstOrientation);
    }

    renderStatus("Mini App готов к работе.", "success");
  } catch (err) {
    renderStatus(err.message || "Ошибка инициализации Mini App.", "error");
  }
}

document.addEventListener("DOMContentLoaded", bootstrap);