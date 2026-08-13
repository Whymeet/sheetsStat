// ============================== Tabs ==============================
function activateTab(name) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  document.getElementById(`tab-${name}`).classList.add("active");
  if (name === "overview") renderOverview();
  if (name === "settings") loadConfig();
  if (name === "columns") loadColumnsTab();
  if (name === "history") loadHistory();
}

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab));
});

// ============================== Profiles (бренды) ==============================
let _activeProfileId = null;
let _profilesState = [];   // [{id, name, sub1, is_active, schedule, sheets_enabled, next_run, last_run}]
let _brandQuery = "";

function activeProfile() {
  return _profilesState.find(p => p.id === _activeProfileId) || null;
}

async function loadProfiles() {
  try {
    const r = await fetch("/api/profiles");
    const data = await r.json();
    _profilesState = data.profiles || [];
    if (!_profilesState.some(p => p.id === _activeProfileId)) {
      _activeProfileId = data.active_id || (_profilesState[0] && _profilesState[0].id) || null;
    }
  } catch {
    _profilesState = [];
    _activeProfileId = null;
  }
  renderSidebar();
  renderOverview();
  renderActiveBrandLabel();
  prefillReportSub1();
}

function renderActiveBrandLabel() {
  const el = document.getElementById("active-brand-label");
  const p = activeProfile();
  el.textContent = p ? `Бренд: ${p.name}` : "";
}

// ---------- Sidebar: список брендов ----------
function renderSidebar() {
  const list = document.getElementById("brand-list");
  list.innerHTML = "";
  const q = _brandQuery.trim().toLowerCase();
  const items = _profilesState.filter(p =>
    !q || (p.name || "").toLowerCase().includes(q) || (p.sub1 || "").toLowerCase().includes(q));

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "brand-empty";
    empty.textContent = _profilesState.length ? "Ничего не найдено" : "Нет брендов";
    list.appendChild(empty);
    return;
  }

  items.forEach(p => {
    const item = document.createElement("button");
    item.className = "brand-item" + (p.id === _activeProfileId ? " active" : "");
    item.dataset.pid = p.id;
    item.title = "ПКМ — переименовать / копировать / удалить";
    item.addEventListener("click", () => activateProfile(p.id));

    const sched = p.schedule || {};
    const schedBadge = sched.enabled
      ? `<span class="badge sched-on">⏰ ${escapeHtml(sched.time || "")}</span>`
      : `<span class="badge sched-off">выкл</span>`;
    const sheetsDot = p.sheets_enabled
      ? `<span class="dot dot-sheets" title="Запись в Sheets включена"></span>` : "";
    const runDot = lastRunDot(p.last_run);

    item.innerHTML = `
      <div class="brand-item-main">
        <span class="brand-name">${escapeHtml(p.name || p.id)}</span>
        ${runDot}${sheetsDot}
      </div>
      <div class="brand-item-sub">
        <span class="brand-sub1">${p.sub1 ? escapeHtml(p.sub1) : "— sub1 не задан"}</span>
        ${schedBadge}
      </div>`;
    list.appendChild(item);
  });
}

document.getElementById("brand-search").addEventListener("input", (e) => {
  _brandQuery = e.target.value || "";
  renderSidebar();
});

// ---------- Overview: дашборд всех брендов ----------
function renderOverview() {
  const tbody = document.querySelector("#overview-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!_profilesState.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="color: var(--muted);">Брендов пока нет. Создай первый кнопкой «＋ Бренд».</td></tr>`;
    return;
  }
  _profilesState.forEach(p => {
    const sched = p.schedule || { enabled: false, time: "09:00" };
    const tr = document.createElement("tr");
    if (p.id === _activeProfileId) tr.classList.add("row-active");

    tr.innerHTML = `
      <td><button class="link-btn" data-open="${escapeHtml(p.id)}">${escapeHtml(p.name || p.id)}</button></td>
      <td>${p.sub1 ? escapeHtml(p.sub1) : '<span class="muted">—</span>'}</td>
      <td>${p.sheets_enabled ? '<span class="dot dot-sheets"></span>' : '<span class="muted">—</span>'}${
        (p.shared_sheet_with || []).length
          ? ` <span class="status err" style="margin:0; font-size:11px;" title="Эту же таблицу использует: ${escapeHtml(p.shared_sheet_with.join(", "))}">⚠ общая</span>`
          : ""}</td>
      <td><label class="switch"><input type="checkbox" ${sched.enabled ? "checked" : ""} data-toggle="${escapeHtml(p.id)}"><span class="slider"></span></label></td>
      <td><input type="text" class="time-input" value="${escapeHtml(sched.time || "09:00")}" data-time="${escapeHtml(p.id)}"></td>
      <td class="nowrap">${fmtNextRun(p.next_run)}</td>
      <td>${lastRunCell(p.last_run)}</td>
      <td><button class="secondary sm" data-run="${escapeHtml(p.id)}">▶ вчера</button></td>`;
    tbody.appendChild(tr);
  });

  // открыть настройки бренда
  tbody.querySelectorAll("[data-open]").forEach(b =>
    b.addEventListener("click", () => { activateProfile(b.dataset.open); activateTab("settings"); }));
  // прогнать один бренд за вчера
  tbody.querySelectorAll("[data-run]").forEach(b =>
    b.addEventListener("click", () => runBrandNow(b.dataset.run)));
  // тумблер расписания
  tbody.querySelectorAll("[data-toggle]").forEach(cb =>
    cb.addEventListener("change", () => {
      const pid = cb.dataset.toggle;
      const timeEl = tbody.querySelector(`[data-time="${CSS.escape(pid)}"]`);
      saveScheduleInline(pid, cb.checked, (timeEl && timeEl.value) || "09:00");
    }));
  // правка времени (по Enter/blur)
  tbody.querySelectorAll("[data-time]").forEach(inp => {
    const commit = () => {
      const pid = inp.dataset.time;
      const toggleEl = tbody.querySelector(`[data-toggle="${CSS.escape(pid)}"]`);
      saveScheduleInline(pid, !!(toggleEl && toggleEl.checked), inp.value.trim());
    };
    inp.addEventListener("blur", commit);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") inp.blur(); });
  });
}

async function saveScheduleInline(pid, enabled, time) {
  const status = document.getElementById("overview-status");
  status.textContent = "Сохраняю расписание…";
  status.className = "status";
  try {
    const r = await fetch(`/api/profiles/${encodeURIComponent(pid)}/schedule`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !!enabled, time: time || "09:00" }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    status.textContent = "✅ Расписание сохранено";
    status.className = "status ok";
    await loadProfiles();
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  }
}

async function runBrandNow(pid) {
  const status = document.getElementById("overview-status");
  const p = _profilesState.find(x => x.id === pid);
  status.textContent = `Прогоняю «${p ? p.name : pid}» за вчера…`;
  status.className = "status";
  try {
    const r = await fetch("/api/schedule/run-now", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: pid }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    const res = (data.results || [])[0] || {};
    if (res.ok) {
      status.textContent = `✅ «${p ? p.name : pid}» (${data.date}): ${res.ok ? "ok" : "ошибка"}${res.google_sheets_error ? ` · Sheets: ${res.google_sheets_error}` : ""}`;
      status.className = res.google_sheets_error ? "status err" : "status ok";
    } else {
      status.textContent = `❌ «${p ? p.name : pid}»: ${res.error || data.error || "ошибка"}`;
      status.className = "status err";
    }
    await loadProfiles();
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  }
}

document.getElementById("overview-reload").addEventListener("click", loadProfiles);
document.getElementById("overview-run-all").addEventListener("click", async () => {
  const status = document.getElementById("overview-status");
  status.textContent = "Прогоняю все бренды за вчера…";
  status.className = "status";
  try {
    const r = await fetch("/api/schedule/run-now", { method: "POST" });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    const results = data.results || [];
    const okN = results.filter(x => x.ok).length;
    const failN = results.length - okN;
    status.textContent = `✅ Готово (${data.date}): ${okN} ок${failN ? `, ${failN} с ошибкой` : ""}`;
    status.className = failN ? "status err" : "status ok";
    await loadProfiles();
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  }
});

// ---------- helpers статуса ----------
function fmtNextRun(iso) {
  if (!iso) return '<span class="muted">—</span>';
  // "2026-06-27T09:00+04:00" → "27.06 09:00"
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) return escapeHtml(iso);
  return `${m[3]}.${m[2]} ${m[4]}:${m[5]}`;
}

function lastRunWhen(lr) {
  const t = (lr && (lr.finished_at || lr.started_at)) || "";
  const m = t.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  return m ? `${m[3]}.${m[2]} ${m[4]}:${m[5]}` : "";
}

function lastRunDot(lr) {
  if (!lr) return `<span class="dot dot-none" title="ещё не запускался"></span>`;
  if (lr.ok) return `<span class="dot dot-ok" title="последний запуск ок"></span>`;
  return `<span class="dot dot-err" title="последний запуск с ошибкой"></span>`;
}

function lastRunCell(lr) {
  if (!lr) return '<span class="muted">—</span>';
  const when = lastRunWhen(lr);
  if (lr.ok) {
    const warn = lr.google_sheets_error ? ` <span class="status err" style="margin:0; font-size:11px;">Sheets!</span>` : "";
    return `<span class="status ok" style="margin:0;">✅ ${when}</span>${warn}`;
  }
  return `<span class="status err" style="margin:0;" title="${escapeHtml(lr.error || "")}">❌ ${when}</span>`;
}

async function activateProfile(id) {
  if (id === _activeProfileId) return;
  try {
    await fetch(`/api/profiles/${encodeURIComponent(id)}/activate`, { method: "POST" });
  } catch {}
  _activeProfileId = id;
  renderSidebar();
  renderOverview();
  renderActiveBrandLabel();
  prefillReportSub1();
  if (document.getElementById("tab-settings").classList.contains("active")) loadConfig();
  if (document.getElementById("tab-columns").classList.contains("active")) loadColumnsTab();
}

// ---------- CRUD брендов (действия адресуются конкретному pid) ----------
function profileById(pid) {
  return _profilesState.find(p => p.id === pid) || null;
}

async function createProfile(copy, sourcePid) {
  const src = copy ? profileById(sourcePid || _activeProfileId) : null;
  const def = copy ? `${src ? src.name : "Бренд"} (копия)` : "Новый бренд";
  const name = prompt(copy ? "Имя нового бренда (копия):" : "Имя нового бренда:", def);
  if (!name || !name.trim()) return;
  const body = { name: name.trim() };
  if (copy && src) body.copy_from = src.id;
  try {
    const r = await fetch("/api/profiles", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Ошибка");
    _activeProfileId = data.id || data.active_id || _activeProfileId;
    await loadProfiles();
    activateTab("settings");
  } catch (e) {
    alert("❌ " + e.message);
  }
}

async function renameProfile(pid) {
  const p = profileById(pid);
  if (!p) return;
  const name = prompt("Новое имя бренда:", p.name);
  if (!name || !name.trim()) return;
  try {
    const r = await fetch(`/api/profiles/${encodeURIComponent(pid)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    if (!r.ok) { const d = await r.json(); throw new Error(d.detail || "Ошибка"); }
    await loadProfiles();
    if (document.getElementById("tab-settings").classList.contains("active")) loadConfig();
  } catch (e) {
    alert("❌ " + e.message);
  }
}

async function deleteProfile(pid) {
  if (!pid) return;
  if (_profilesState.length <= 1) { alert("Нельзя удалить последний бренд."); return; }
  const p = profileById(pid);
  if (!confirm(`Удалить бренд «${p ? p.name : pid}»? Это необратимо.`)) return;
  try {
    const r = await fetch(`/api/profiles/${encodeURIComponent(pid)}`, { method: "DELETE" });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Ошибка");
    // backend меняет active_id только если удалили активный — берём его как есть
    _activeProfileId = data.active_id || null;
    await loadProfiles();
    if (document.getElementById("tab-settings").classList.contains("active")) loadConfig();
  } catch (e) {
    alert("❌ " + e.message);
  }
}

document.getElementById("profile-add").addEventListener("click", () => createProfile(false));
document.getElementById("profile-copy").addEventListener("click", () => createProfile(true, _activeProfileId));
document.getElementById("profile-rename").addEventListener("click", () => renameProfile(_activeProfileId));
document.getElementById("profile-delete").addEventListener("click", () => deleteProfile(_activeProfileId));

// ---------- Контекстное меню бренда (ПКМ) ----------
const _brandMenu = document.createElement("div");
_brandMenu.id = "brand-menu";
_brandMenu.className = "ctx-menu hidden";
document.body.appendChild(_brandMenu);
let _menuPid = null;

function openBrandMenu(x, y, pid) {
  _menuPid = pid;
  const p = profileById(pid);
  const last = _profilesState.length <= 1;
  _brandMenu.innerHTML = `
    <div class="ctx-title">${escapeHtml(p ? (p.name || p.id) : pid)}</div>
    <button class="ctx-item" data-act="rename">✎ Переименовать</button>
    <button class="ctx-item" data-act="copy">⧉ Копировать</button>
    <button class="ctx-item danger" data-act="delete"${last ? " disabled" : ""}>🗑 Удалить</button>`;
  _brandMenu.classList.remove("hidden");
  // позиционируем с учётом краёв экрана (после снятия hidden — размеры известны)
  const rect = _brandMenu.getBoundingClientRect();
  const px = Math.min(x, window.innerWidth - rect.width - 8);
  const py = Math.min(y, window.innerHeight - rect.height - 8);
  _brandMenu.style.left = `${Math.max(8, px)}px`;
  _brandMenu.style.top = `${Math.max(8, py)}px`;
  _brandMenu.querySelectorAll(".ctx-item").forEach(b =>
    b.addEventListener("click", () => {
      const act = b.dataset.act;
      const target = _menuPid;
      hideBrandMenu();
      if (act === "rename") renameProfile(target);
      else if (act === "copy") createProfile(true, target);
      else if (act === "delete") deleteProfile(target);
    }));
}

function hideBrandMenu() {
  _brandMenu.classList.add("hidden");
  _menuPid = null;
}

document.getElementById("brand-list").addEventListener("contextmenu", (e) => {
  const item = e.target.closest(".brand-item");
  if (!item || !item.dataset.pid) return;
  e.preventDefault();
  openBrandMenu(e.clientX, e.clientY, item.dataset.pid);
});
document.addEventListener("click", (e) => { if (!_brandMenu.contains(e.target)) hideBrandMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideBrandMenu(); });
window.addEventListener("blur", hideBrandMenu);
document.getElementById("brand-list").addEventListener("scroll", hideBrandMenu);

function prefillReportSub1() {
  const p = activeProfile();
  const sub1 = p ? (p.sub1 || "") : "";
  const el = document.getElementById("report-sub1");
  if (el) el.value = sub1;
  const elRange = document.getElementById("report-range-sub1");
  if (elRange) elRange.value = sub1;
}

// ============================== Report ==============================
const today = new Date().toISOString().slice(0, 10);
document.getElementById("report-date").value = today;
document.getElementById("report-start").value = today;
document.getElementById("report-end").value = today;

// Переключатель режимов «За день» / «За период»
document.querySelectorAll("#report-mode-tabs .mode-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const mode = tab.dataset.mode;
    document.querySelectorAll("#report-mode-tabs .mode-tab").forEach((t) =>
      t.classList.toggle("active", t === tab));
    document.querySelectorAll("[data-mode-form]").forEach((f) =>
      f.classList.toggle("hidden", f.dataset.modeForm !== mode));
  });
});

document.getElementById("report-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const date = document.getElementById("report-date").value;
  const sub1 = document.getElementById("report-sub1").value.trim();
  const status = document.getElementById("report-status");
  const result = document.getElementById("report-result");

  if (!_activeProfileId) {
    status.textContent = "❌ Не выбран бренд. Выбери его в списке слева.";
    status.className = "status err";
    return;
  }

  const submitBtn = ev.target.querySelector("button[type=submit]");
  status.textContent = `Считаю отчёт за ${date}… (обычно 10–60 секунд)`;
  status.className = "status";
  result.classList.add("hidden");
  if (submitBtn) submitBtn.disabled = true;

  // прогресс-бар в «бегущем» режиме + серверная отмена по токену
  const progress = document.getElementById("range-progress");
  const bar = document.getElementById("range-bar");
  const cancelBtn = document.getElementById("range-cancel");
  progress.classList.remove("hidden");
  document.getElementById("range-counter").textContent = "1 день";
  document.getElementById("range-days").style.display = "none";
  document.getElementById("range-status").textContent = "";
  bar.classList.add("indeterminate");
  _rangeCancel = false;
  cancelBtn.disabled = false;
  const token = _newCancelToken();
  _currentCancelToken = token;

  try {
    const r = await fetch("/api/report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: _activeProfileId, date, sub1, cancel_token: token }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    if (data.cancelled) {
      status.textContent = `⏹ Отменено — день ${date} не записан`;
      status.className = "status err";
    } else {
      renderReport(data);
      status.textContent = data._saved ? `✅ Сохранено (${data._saved.date})` : "Готово";
      status.className = "status ok";
    }
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  } finally {
    _currentCancelToken = null;
    if (submitBtn) submitBtn.disabled = false;
    bar.classList.remove("indeterminate");
    bar.style.width = "100%";
    setTimeout(() => { progress.classList.add("hidden"); bar.style.width = "0%"; }, 600);
    document.getElementById("range-days").style.display = "";
    cancelBtn.disabled = false;
    document.getElementById("range-status").textContent = "";
  }
});

// Перечисляет даты ISO YYYY-MM-DD от startIso до endIso включительно.
function enumerateDates(startIso, endIso) {
  const out = [];
  // Считаем в UTC (суффикс Z + setUTCDate), иначе toISOString() переведёт
  // местную полночь в UTC и сдвинет дату на сутки назад в зонах восточнее UTC.
  const end = new Date(endIso + "T00:00:00Z");
  for (let d = new Date(startIso + "T00:00:00Z"); d <= end; d.setUTCDate(d.getUTCDate() + 1)) {
    out.push(d.toISOString().slice(0, 10));
  }
  return out;
}

let _rangeCancel = false;
let _currentCancelToken = null;  // токен текущего серверного прогона

function _newCancelToken() {
  return (crypto.randomUUID ? crypto.randomUUID() : `t${Date.now()}${Math.random()}`);
}

document.getElementById("range-cancel").addEventListener("click", async () => {
  _rangeCancel = true;
  const rangeStatus = document.getElementById("range-status");
  rangeStatus.textContent = "Останавливаю — текущий день отменяется и не записывается…";
  rangeStatus.className = "status";
  document.getElementById("range-cancel").disabled = true;
  if (_currentCancelToken) {
    try {
      await fetch("/api/report/cancel", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cancel_token: _currentCancelToken }),
      });
    } catch {}
  }
});

function _rangeDayRow(date) {
  const tr = document.createElement("tr");
  tr.className = "current";
  tr.innerHTML = `<td>${escapeHtml(date)}</td>
    <td class="status" style="margin:0;">считаю…</td><td>—</td><td></td>`;
  const tbody = document.querySelector("#range-days tbody");
  tbody.appendChild(tr);
  tr.scrollIntoView({ block: "nearest" });
  return tr;
}

function _rangeDayDone(tr, data, err) {
  tr.classList.remove("current");
  const cells = tr.children;
  if (err) {
    cells[1].innerHTML = `<span class="status err" style="margin:0;">❌ ${escapeHtml(err)}</span>`;
    return;
  }
  cells[1].innerHTML = `<span class="status ok" style="margin:0;">✅ ок</span>`;
  const gs = data.google_sheets || {};
  cells[2].textContent = gs.error
    ? `❌ ${gs.error}`
    : (gs.enabled === false ? "выкл" :
       `стр. ${gs.date_row} · ${(gs.matched || []).length}/${(gs.unmatched || []).length}`);
  const warns = reportSourceErrors(data).concat(gs.header_warnings || []);
  if (data.warning) warns.unshift(data.warning);
  if (warns.length) {
    cells[3].innerHTML = `<span title="${escapeHtml(warns.join("\\n"))}" style="cursor:help;">⚠</span>`;
  }
}

document.getElementById("report-range-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const start = document.getElementById("report-start").value;
  const end = document.getElementById("report-end").value;
  const sub1 = document.getElementById("report-range-sub1").value.trim();
  const btn = document.getElementById("report-range-submit");
  const dayBtn = document.querySelector("#report-form button[type=submit]");
  const status = document.getElementById("report-status");
  const progress = document.getElementById("range-progress");
  const rangeStatus = document.getElementById("range-status");
  const bar = document.getElementById("range-bar");
  const counter = document.getElementById("range-counter");
  const cancelBtn = document.getElementById("range-cancel");
  const result = document.getElementById("report-result");

  if (!_activeProfileId) {
    status.textContent = "❌ Не выбран бренд. Выбери его в списке слева.";
    status.className = "status err";
    return;
  }
  if (!start || !end || start > end) {
    status.textContent = "❌ Начало периода должно быть не позже конца.";
    status.className = "status err";
    return;
  }

  const dates = enumerateDates(start, end);
  if (dates.length > 92 && !confirm(`Период ${dates.length} дней — это может занять несколько минут и много запросов. Продолжить?`)) {
    return;
  }

  status.textContent = "";
  status.className = "status";
  result.classList.add("hidden");
  progress.classList.remove("hidden");
  document.querySelector("#range-days tbody").innerHTML = "";
  bar.style.width = "0%";
  counter.textContent = `0 / ${dates.length}`;
  _rangeCancel = false;
  cancelBtn.disabled = false;
  btn.disabled = true;
  if (dayBtn) dayBtn.disabled = true;

  let lastData = null;
  let okCount = 0;
  let errCount = 0;
  let cancelled = false;
  try {
    for (let i = 0; i < dates.length; i++) {
      if (_rangeCancel) { cancelled = true; break; }
      const date = dates[i];
      rangeStatus.textContent = `Считаю ${date} (${i + 1} из ${dates.length})…`;
      rangeStatus.className = "status";
      const tr = _rangeDayRow(date);
      const token = _newCancelToken();
      _currentCancelToken = token;
      try {
        const r = await fetch("/api/report", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile_id: _activeProfileId, date, sub1, cancel_token: token }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || r.statusText);
        if (data.cancelled) {
          tr.classList.remove("current");
          tr.children[1].innerHTML = `<span class="status err" style="margin:0;">⏹ отменён, не записан</span>`;
          cancelled = true;
          break;
        }
        lastData = data;
        _rangeDayDone(tr, data, null);
        okCount++;
      } catch (e) {
        _rangeDayDone(tr, null, e.message);
        errCount++;
      } finally {
        _currentCancelToken = null;
      }
      bar.style.width = `${Math.round(((i + 1) / dates.length) * 100)}%`;
      counter.textContent = `${i + 1} / ${dates.length}`;
    }
    if (cancelled) {
      rangeStatus.textContent = `⏹ Отменено: успело ${okCount + errCount} из ${dates.length}`
        + (errCount ? ` (ошибок: ${errCount})` : "");
      rangeStatus.className = "status err";
    } else {
      rangeStatus.textContent = errCount
        ? `Готово: ${okCount} ок, ${errCount} с ошибками`
        : `✅ Готово: ${okCount} дн.`;
      rangeStatus.className = errCount ? "status err" : "status ok";
    }
  } finally {
    btn.disabled = false;
    if (dayBtn) dayBtn.disabled = false;
    cancelBtn.disabled = false;
  }

  if (lastData) renderReport(lastData);
});

// Собирает ошибки по источникам из отчёта дня (каждая секция кладёт errors[]).
// Пустой массив = день полный. Непустой = день записан, но часть данных не собрана.
function reportSourceErrors(data) {
  const sections = ["ads_manager", "yandex", "yandex_metrika", "leadstech", "eightconnect"];
  const msgs = [];
  for (const s of sections) {
    const errs = data[s] && data[s].errors;
    if (Array.isArray(errs) && errs.length) {
      const txt = errs.map((e) => e.error || JSON.stringify(e)).join("; ");
      msgs.push(`${s}: ${txt}`);
    }
  }
  return msgs;
}

function _fillCabinetTable(tableId, cabinets) {
  const table = document.getElementById(tableId);
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  table.classList.remove("show-zeros");
  const entries = Object.entries(cabinets || {})
    .map(([n, v]) => [n, Number(v || 0)])
    .sort((a, b) => b[1] - a[1]);
  entries.forEach(([name, spent]) => {
    const tr = document.createElement("tr");
    if (!spent) tr.className = "zero-row";
    tr.innerHTML = `<td>${escapeHtml(name)}</td><td class="num">${fmtMoney(spent)}</td>`;
    tbody.appendChild(tr);
  });
  const zeros = entries.filter(e => !e[1]).length;
  if (zeros) {
    const tr = document.createElement("tr");
    tr.className = "zero-toggle";
    tr.innerHTML = `<td colspan="2">ещё ${zeros} с нулевым расходом — показать</td>`;
    tr.addEventListener("click", () => {
      const shown = table.classList.toggle("show-zeros");
      tr.querySelector("td").textContent = shown
        ? "скрыть нулевые" : `ещё ${zeros} с нулевым расходом — показать`;
    });
    tbody.appendChild(tr);
  }
  return entries.length;
}

function renderReport(data) {
  const result = document.getElementById("report-result");
  result.classList.remove("hidden");

  // Шапка одной строкой: дата · sub1 · кабинеты · чип Sheets
  const gs = data.google_sheets || {};
  let gsChip = "";
  if (gs.enabled === false || !data.google_sheets) {
    gsChip = `<span class="chip">Sheets выкл</span>`;
  } else if (gs.error) {
    gsChip = `<span class="chip err">Sheets: ${escapeHtml(gs.error)}</span>`;
  } else {
    gsChip = `<span class="chip ok">Sheets ✓ «${escapeHtml(gs.worksheet || "")}» стр. ${gs.date_row}
      · ${(gs.matched || []).length} записано${(gs.unmatched || []).length ? ` · ${(gs.unmatched || []).length} мимо` : ""}</span>`;
  }
  document.getElementById("report-head").innerHTML =
    `<b>${escapeHtml(data.date || "")}</b>
     <span>sub1 <b>${escapeHtml(data.sub1 || "")}</b></span>
     <span>кабинетов <b>${data.cabinet_count ?? 0}</b></span>
     ${gsChip}`;

  // Предупреждения — списком, каждое с новой строки
  const meta = data.metrics_meta || {};
  const warns = [];
  if (data.warning) warns.push(data.warning);
  (gs.header_warnings || []).forEach(w => warns.push("шапка листа: " + w));
  if ((meta.assumed_zero || []).length)
    warns.push("ручные поля без значения, посчитаны как 0: " + meta.assumed_zero.join(", "));
  (meta.coeff_warnings || []).forEach(w => warns.push(w));
  reportSourceErrors(data).forEach(w => warns.push(w));
  document.getElementById("report-warnings").innerHTML =
    warns.map(w => `<div class="warn-line">${escapeHtml(w)}</div>`).join("");

  renderReportMetrics(data);

  // Ads Manager / Yandex: сортировка по расходу, нули спрятаны
  const ads = data.ads_manager || { cabinets: {}, total: 0 };
  const adsCount = _fillCabinetTable("cabinets-table", ads.cabinets);
  document.getElementById("ads-total").textContent = fmtMoney(ads.total);
  document.getElementById("ads-info").textContent =
    `· ${adsCount} кабинетов · ${fmtMoney(ads.total)}`;

  const yx = data.yandex || { cabinets: {}, total: 0 };
  const yxCount = _fillCabinetTable("yandex-table", yx.cabinets);
  document.getElementById("yandex-total").textContent = fmtMoney(yx.total);
  document.getElementById("yx-info").textContent =
    `· ${yxCount} кабинетов · ${fmtMoney(yx.total)}`;

  // Yandex Metrika
  const ym = data.yandex_metrika || {};
  const fmtInt = (v) => (v ?? 0).toLocaleString("ru-RU");
  document.getElementById("ym-info").textContent =
    `· визиты ${fmtInt(ym.visits)} · просмотры ${fmtInt(ym.pageviews)}`;
  document.getElementById("ym-visits-total").textContent = fmtInt(ym.visits);
  document.getElementById("ym-pageviews-total").textContent = fmtInt(ym.pageviews);
  document.getElementById("ym-users-total").textContent = fmtInt(ym.users);
  const ymTbody = document.querySelector("#metrika-table tbody");
  ymTbody.innerHTML = "";
  const goalsArr = Array.isArray(ym.goals) ? ym.goals : [];
  if (!goalsArr.length) {
    const err = (ym.errors && ym.errors[0] && ym.errors[0].error) || "нет данных";
    ymTbody.innerHTML = `<tr><td colspan="4" style="color: var(--muted);">${escapeHtml(err)}</td></tr>`;
  } else {
    goalsArr.forEach(g => {
      const tr = document.createElement("tr");
      const nameCell = g.error
        ? `${escapeHtml(g.goal_name)} <span class="status err" style="font-size:11px">${escapeHtml(g.error)}</span>`
        : escapeHtml(g.goal_name);
      const crTxt = (g.conversion_rate === null || g.conversion_rate === undefined)
        ? "—"
        : Number(g.conversion_rate).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      tr.innerHTML = `<td>${nameCell}</td>
        <td class="num">${(g.reaches ?? 0).toLocaleString("ru-RU")}</td>
        <td class="num">${(g.visits ?? 0).toLocaleString("ru-RU")}</td>
        <td class="num">${crTxt}</td>`;
      ymTbody.appendChild(tr);
    });
  }

  // LeadsTech
  const lt = data.leadstech || {};
  document.getElementById("lt-info").textContent =
    `· клики ${(lt.clicks ?? 0).toLocaleString("ru-RU")} · сумма ${fmtMoney(lt.sum)}`;
  document.getElementById("lt-clicks").textContent = (lt.clicks ?? 0).toLocaleString("ru-RU");
  document.getElementById("lt-hosts").textContent = (lt.hosts ?? 0).toLocaleString("ru-RU");
  document.getElementById("lt-sum").textContent = fmtMoney(lt.sum);
  const convEl = document.getElementById("lt-conv");
  if (convEl) {
    const c = lt.conversions ?? 0;
    const a = lt.approved ?? 0;
    const r = lt.rejected ?? 0;
    convEl.textContent = `${c} / ${a} / ${r}`;
  }

  // Разбивка по аккаунтам LeadsTech — показываем, только если их больше одного
  const ltAccounts = Array.isArray(lt.accounts) ? lt.accounts : [];
  const ltTable = document.getElementById("leadstech-accounts-table");
  const ltTbody = ltTable.querySelector("tbody");
  ltTbody.innerHTML = "";
  ltTable.style.display = ltAccounts.length > 1 ? "" : "none";
  ltAccounts.forEach(a => {
    const tr = document.createElement("tr");
    const nameCell = a.error
      ? `${escapeHtml(a.account || "—")} <span class="status err" style="font-size:11px">${escapeHtml(a.error)}</span>`
      : escapeHtml(a.account || "—");
    tr.innerHTML = `<td>${nameCell}</td>
      <td>${escapeHtml(a.sub1 || "—")}</td>
      <td class="num">${(a.clicks ?? 0).toLocaleString("ru-RU")}</td>
      <td class="num">${(a.hosts ?? 0).toLocaleString("ru-RU")}</td>
      <td class="num">${fmtMoney(a.sum)}</td>`;
    ltTbody.appendChild(tr);
  });

  const ltErrEl = document.getElementById("lt-error");
  const ltErrs = Array.isArray(lt.errors) ? lt.errors : [];
  ltErrEl.textContent = ltErrs.length
    ? "❌ " + ltErrs.map(e => (e.account ? `${e.account}: ` : "") + (e.error || JSON.stringify(e))).join("; ")
    : "";

  // 8connect
  const ec = data.eightconnect || {};
  document.getElementById("ec-info").textContent =
    `· расход ${fmtMoney(ec.cost)} · доход ${fmtMoney(ec.charge)}`;
  document.getElementById("ec-cost").textContent = fmtMoney(ec.cost);
  document.getElementById("ec-charge").textContent = fmtMoney(ec.charge);
  const ecSchemes = Array.isArray(ec.scheme_ids) ? ec.scheme_ids : [];
  document.getElementById("ec-scheme-list").textContent = ecSchemes.length ? ecSchemes.join(", ") : "—";
  const ecErrEl = document.getElementById("ec-error");
  const ecErr = (ec.errors && ec.errors[0] && ec.errors[0].error) || "";
  ecErrEl.textContent = ecErr ? `❌ ${ecErr}` : "";

  document.getElementById("report-raw").textContent = JSON.stringify(data, null, 2);

  // Секции с ошибками источника раскрываются сами и помечаются
  const errMap = {
    "sec-ads": (data.ads_manager || {}).errors,
    "sec-yx": (data.yandex || {}).errors,
    "sec-ym": (data.yandex_metrika || {}).errors,
    "sec-lt": (data.leadstech || {}).errors,
    "sec-ec": (data.eightconnect || {}).errors,
  };
  for (const [secId, errs] of Object.entries(errMap)) {
    const sec = document.getElementById(secId);
    if (!sec) continue;
    const hasErr = Array.isArray(errs) && errs.length > 0;
    sec.open = hasErr;
    sec.querySelector("summary").classList.toggle("has-error", hasErr);
    if (hasErr) {
      const info = sec.querySelector(".sec-info");
      if (info) info.textContent += " · ⚠ ошибка источника";
    }
  }
}

function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

// ============================== Settings ==============================
let _cfgState = null;

async function loadConfig() {
  if (!_activeProfileId) await loadProfiles();
  if (!_activeProfileId) {
    _cfgState = emptyConfig();
    renderConfig(_cfgState);
    renderBrandSchedule(_cfgState.schedule);
    return;
  }
  try {
    const r = await fetch(`/api/profiles/${encodeURIComponent(_activeProfileId)}/config`);
    if (!r.ok) { _cfgState = emptyConfig(); }
    else { _cfgState = await r.json(); }
  } catch {
    _cfgState = emptyConfig();
  }
  renderConfig(_cfgState);
  renderBrandSchedule(_cfgState.schedule);
  renderBrandScheduleInfo();

  const p = activeProfile();
  document.getElementById("settings-profile-name").textContent = _cfgState.name || (p && p.name) || "—";
  document.getElementById("profile-sub1").value = _cfgState.sub1 || (p && p.sub1) || "";
}

const DEFAULT_ADS_MANAGER_BASE_URL = "https://kybyshka-dev.ru";

function emptyConfig() {
  return {
    name: "", sub1: "",
    leadstech: { base_url: "https://api.leads.tech", login: "", password: "", accounts: [], page_size: 500 },
    ads_manager: { base_url: DEFAULT_ADS_MANAGER_BASE_URL, username: "", password: "" },
    yandex: { base_url: "", username: "", password: "" },
    yandex_metrika: { oauth_token: "", counter_id: 0, goals: ["Zayvka"], attribution: "LASTSIGN" },
    eightconnect: { base_url: "https://8connect.ru", login: "", password: "",
                    category_ids: [149, 395, 620, 624],
                    scheme_ids: [1006, 2260, 2805, 2809, 612] },
    google_sheets: { enabled: false, spreadsheet_id: "", service_account_json_path: "cfg/service_account.json" },
    schedule: { enabled: false, time: "09:00" },
  };
}

// ---------- Аккаунты LeadsTech (стата со всех складывается) ----------

function ltAccountRow(acc) {
  const a = acc || {};
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td><input type="text" data-lt="name" placeholder="напр. Основной"></td>
    <td><input type="text" data-lt="login" autocomplete="off"></td>
    <td><input type="password" data-lt="password" autocomplete="off" placeholder="пусто — не менять"></td>
    <td><input type="text" data-lt="sub1" placeholder="общий"></td>
    <td><input type="text" data-lt="base_url" placeholder="общий"></td>
    <td class="num"><input type="checkbox" data-lt="enabled"></td>
    <td><button type="button" class="danger sm" data-lt-del title="Удалить аккаунт">🗑</button></td>`;
  tr.querySelector('[data-lt="name"]').value = a.name ?? "";
  tr.querySelector('[data-lt="login"]').value = a.login ?? "";
  tr.querySelector('[data-lt="password"]').value = a.password ?? "";
  tr.querySelector('[data-lt="sub1"]').value = a.sub1 ?? "";
  tr.querySelector('[data-lt="base_url"]').value = a.base_url ?? "";
  tr.querySelector('[data-lt="enabled"]').checked = a.enabled !== false;
  tr.querySelector("[data-lt-del]").addEventListener("click", () => tr.remove());
  return tr;
}

function renderLeadstechAccounts(lt) {
  const tbody = document.querySelector("#lt-accounts tbody");
  tbody.innerHTML = "";
  let accounts = Array.isArray(lt?.accounts) ? lt.accounts : [];
  // legacy-профиль: креды лежали прямо в секции — показываем их одной строкой
  if (!accounts.length && (lt?.login || lt?.password)) {
    accounts = [{ name: "", login: lt.login, password: lt.password, sub1: "", base_url: "", enabled: true }];
  }
  if (!accounts.length) accounts = [{}];
  accounts.forEach(a => tbody.appendChild(ltAccountRow(a)));
}

function readLeadstechAccounts() {
  const rows = document.querySelectorAll("#lt-accounts tbody tr");
  const out = [];
  rows.forEach(tr => {
    const get = (k) => tr.querySelector(`[data-lt="${k}"]`);
    const login = get("login").value.trim();
    if (!login) return;   // пустая строка — просто не сохраняем
    out.push({
      name: get("name").value.trim(),
      login,
      password: get("password").value,
      sub1: get("sub1").value.trim(),
      base_url: get("base_url").value.trim(),
      enabled: get("enabled").checked,
    });
  });
  return out;
}

document.getElementById("lt-account-add").addEventListener("click", () => {
  document.querySelector("#lt-accounts tbody").appendChild(ltAccountRow({}));
});

function renderConfig(cfg) {
  renderLeadstechAccounts(cfg.leadstech);
  document.getElementById("lt-base-url").value = cfg.leadstech?.base_url ?? "https://api.leads.tech";
  document.getElementById("lt-page-size").value = cfg.leadstech?.page_size ?? 500;

  document.getElementById("am-base-url").value = cfg.ads_manager?.base_url || DEFAULT_ADS_MANAGER_BASE_URL;
  document.getElementById("am-username").value = cfg.ads_manager?.username ?? "";
  document.getElementById("am-password").value = cfg.ads_manager?.password ?? "";

  document.getElementById("yx-base-url").value = cfg.yandex?.base_url ?? "";
  document.getElementById("yx-username").value = cfg.yandex?.username ?? "";
  document.getElementById("yx-password").value = cfg.yandex?.password ?? "";

  document.getElementById("ym-oauth-token").value = cfg.yandex_metrika?.oauth_token ?? "";
  document.getElementById("ym-counter-id").value = cfg.yandex_metrika?.counter_id ?? 0;
  const goalsArr = Array.isArray(cfg.yandex_metrika?.goals) ? cfg.yandex_metrika.goals
                 : (cfg.yandex_metrika?.goal_name ? [cfg.yandex_metrika.goal_name] : ["Zayvka"]);
  document.getElementById("ym-goals").value = goalsArr.join(", ");
  document.getElementById("ym-attribution").value = cfg.yandex_metrika?.attribution ?? "LASTSIGN";
  document.getElementById("ym-zayavki-metric").value = cfg.yandex_metrika?.zayavki_metric ?? "visits";

  document.getElementById("ec-login").value = cfg.eightconnect?.login ?? "";
  document.getElementById("ec-password").value = cfg.eightconnect?.password ?? "";
  const categoryIds = Array.isArray(cfg.eightconnect?.category_ids) ? cfg.eightconnect.category_ids : [];
  document.getElementById("ec-category-ids").value = categoryIds.join(", ");
  const schemeIds = Array.isArray(cfg.eightconnect?.scheme_ids) ? cfg.eightconnect.scheme_ids : [];
  document.getElementById("ec-scheme-ids").value = schemeIds.join(", ");

  document.getElementById("gs-spreadsheet-id").value = cfg.google_sheets?.spreadsheet_id ?? "";
  document.getElementById("gs-enabled").checked = !!cfg.google_sheets?.enabled;
  renderSheetLink();
}

// ---------- Расписание выбранного бренда (в настройках) ----------
function renderBrandSchedule(schedule) {
  const s = schedule || { enabled: false, time: "09:00" };
  document.getElementById("brand-sched-enabled").checked = !!s.enabled;
  document.getElementById("brand-sched-time").value = s.time || "09:00";
}

function renderBrandScheduleInfo() {
  const el = document.getElementById("brand-sched-info");
  const p = activeProfile();
  if (!el) return;
  if (!p || !(p.schedule && p.schedule.enabled)) {
    el.textContent = "Автозапуск выключен.";
    el.className = "status";
    return;
  }
  let txt = `✅ Активно. Следующий запуск: ${fmtNextRun(p.next_run)} (Samara)`;
  if (p.last_run) {
    const lr = p.last_run;
    txt += lr.ok
      ? ` · последний: ${lastRunWhen(lr)} → ${lr.ok ? "ok" : "ошибка"}`
      : ` · последний упал: ${lastRunWhen(lr)} (${lr.error || "см. логи"})`;
  }
  el.textContent = txt;
  el.className = "status ok";
}

async function saveBrandSchedule() {
  if (!_activeProfileId) return;
  const status = document.getElementById("brand-sched-status");
  const payload = {
    enabled: document.getElementById("brand-sched-enabled").checked,
    time: document.getElementById("brand-sched-time").value.trim() || "09:00",
  };
  status.textContent = "Сохраняю…";
  status.className = "status";
  try {
    const r = await fetch(`/api/profiles/${encodeURIComponent(_activeProfileId)}/schedule`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    status.textContent = "✅ Сохранено";
    status.className = "status ok";
    await loadProfiles();
    renderBrandScheduleInfo();
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  }
}
document.getElementById("brand-sched-save").addEventListener("click", saveBrandSchedule);

document.getElementById("brand-sched-run").addEventListener("click", async () => {
  if (!_activeProfileId) return;
  const status = document.getElementById("brand-sched-status");
  status.textContent = "Прогоняю за вчера…";
  status.className = "status";
  try {
    const r = await fetch("/api/schedule/run-now", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: _activeProfileId }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    const res = (data.results || [])[0] || {};
    status.textContent = res.ok
      ? `✅ ${data.date}: ${res.ok ? "ok" : "ошибка"}${res.google_sheets_error ? ` · Sheets: ${res.google_sheets_error}` : ""}`
      : `❌ ${res.error || "ошибка"}`;
    status.className = (res.ok && !res.google_sheets_error) ? "status ok" : "status err";
    await loadProfiles();
    renderBrandScheduleInfo();
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  }
});

function extractSpreadsheetId(v) {
  const s = (v || "").trim();
  const m = s.match(/\/spreadsheets\/d\/([a-zA-Z0-9_-]+)/);
  return m ? m[1] : s;
}

function renderSheetLink() {
  const raw = document.getElementById("gs-spreadsheet-id").value;
  const id = extractSpreadsheetId(raw);
  const link = document.getElementById("gs-link");
  if (!id) { link.textContent = ""; return; }
  link.innerHTML = `ID: <code>${escapeHtml(id)}</code> — <a href="https://docs.google.com/spreadsheets/d/${encodeURIComponent(id)}/edit" target="_blank" rel="noopener">открыть</a>`;
}

document.addEventListener("input", (e) => {
  if (e.target && e.target.id === "gs-spreadsheet-id") renderSheetLink();
});

document.getElementById("save-config").addEventListener("click", async () => {
  const status = document.getElementById("save-status");

  if (!_activeProfileId) {
    status.textContent = "❌ Не выбран бренд.";
    status.className = "status err";
    return;
  }

  const payload = {
    name: (activeProfile()?.name) || _cfgState?.name || "",
    sub1: document.getElementById("profile-sub1").value.trim(),
    leadstech: {
      base_url: document.getElementById("lt-base-url").value.trim(),
      // legacy-поля больше не редактируются, но и не затираются
      login: _cfgState?.leadstech?.login ?? "",
      password: _cfgState?.leadstech?.password ?? "",
      accounts: readLeadstechAccounts(),
      page_size: parseInt(document.getElementById("lt-page-size").value, 10) || 500,
    },
    ads_manager: {
      base_url: document.getElementById("am-base-url").value.trim(),
      username: document.getElementById("am-username").value.trim(),
      password: document.getElementById("am-password").value,
    },
    yandex: {
      base_url: document.getElementById("yx-base-url").value.trim(),
      username: document.getElementById("yx-username").value.trim(),
      password: document.getElementById("yx-password").value,
    },
    yandex_metrika: {
      oauth_token: document.getElementById("ym-oauth-token").value.trim(),
      counter_id: parseInt(document.getElementById("ym-counter-id").value, 10) || 0,
      goals: document.getElementById("ym-goals").value
              .split(",").map(s => s.trim()).filter(Boolean),
      attribution: document.getElementById("ym-attribution").value.trim() || "LASTSIGN",
      zayavki_metric: document.getElementById("ym-zayavki-metric").value || "visits",
    },
    eightconnect: {
      base_url: _cfgState?.eightconnect?.base_url || "https://8connect.ru",
      login: document.getElementById("ec-login").value.trim(),
      password: document.getElementById("ec-password").value,
      category_ids: document.getElementById("ec-category-ids").value
                      .split(",").map(s => parseInt(s.trim(), 10))
                      .filter(n => Number.isFinite(n) && n > 0),
      scheme_ids: document.getElementById("ec-scheme-ids").value
                    .split(",").map(s => parseInt(s.trim(), 10))
                    .filter(n => Number.isFinite(n) && n > 0),
    },
    google_sheets: {
      enabled: document.getElementById("gs-enabled").checked,
      spreadsheet_id: extractSpreadsheetId(document.getElementById("gs-spreadsheet-id").value),
      service_account_json_path: _cfgState?.google_sheets?.service_account_json_path || "cfg/service_account.json",
      // поля ниже правятся в JSON профиля / другими экранами — пробрасываем,
      // чтобы сохранение настроек их не затёрло
      column_labels: _cfgState?.google_sheets?.column_labels || {},
      metric_names: _cfgState?.google_sheets?.metric_names || {},
      disabled_metrics: _cfgState?.google_sheets?.disabled_metrics || [],
      managed_formulas: !!_cfgState?.google_sheets?.managed_formulas,
      auto_create_tab: !!_cfgState?.google_sheets?.auto_create_tab,
      cabinet_coeffs: _cfgState?.google_sheets?.cabinet_coeffs || {},
      manual_cabinets: _cfgState?.google_sheets?.manual_cabinets || [],
      cabinets: _cfgState?.google_sheets?.cabinets || [],
      share_with: _cfgState?.google_sheets?.share_with || [],
      cabinet_start_col: _cfgState?.google_sheets?.cabinet_start_col || "",
      cabinet_max_col: _cfgState?.google_sheets?.cabinet_max_col || "",
    },
    // расписание включаем в payload, чтобы сохранение настроек его не затёрло
    schedule: {
      enabled: document.getElementById("brand-sched-enabled").checked,
      time: document.getElementById("brand-sched-time").value.trim() || "09:00",
    },
    analysis: _cfgState?.analysis || { lookback_days: 7 },
  };

  status.textContent = "Сохраняю…";
  status.className = "status";

  try {
    const r = await fetch(`/api/profiles/${encodeURIComponent(_activeProfileId)}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    status.textContent = "✅ Сохранено";
    status.className = "status ok";
    _cfgState = payload;
    await loadProfiles();   // name/sub1/расписание в списке могли измениться
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  }
});

// ---------- Вычисленные метрики в отчёте ----------
async function renderReportMetrics(data) {
  const el = document.getElementById("report-metrics");
  if (!el) return;
  const m = data.metrics;
  if (!m) { el.innerHTML = ""; return; }
  // схема per-brand: системные имена активного бренда
  let schema = _columnsSchema;
  try {
    const rr = await fetch(`/api/metrics${_activeProfileId ? `?profile_id=${encodeURIComponent(_activeProfileId)}` : ""}`);
    schema = (await rr.json()).metrics || schema || [];
  } catch { schema = schema || []; }
  const meta = data.metrics_meta || {};
  const manualVals = meta.manual_cabinets || {};
  const fmt = v => v === null || v === undefined ? "—"
    : (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString("ru-RU") : (+v.toFixed(2)).toLocaleString("ru-RU"));
  const cells = (schema || [])
    .filter(c => !c.disabled)
    .filter(c => (c.kind !== "date" && c.key in m) || c.kind === "manual_cabinet")
    .map(c => {
      const v = c.kind === "manual_cabinet" ? manualVals[c.label] : m[c.key];
      const nm = c.system_name || c.label || c.key;
      const empty = v === null || v === undefined;
      return `<div class="metric-cell">
        <div class="m-name" title="${escapeHtml(nm)}">${escapeHtml(nm)}</div>
        <div class="m-value${empty ? " empty" : ""}">${fmt(v)}</div></div>`;
    })
    .join("");
  el.innerHTML = `<details class="src" open><summary>Метрики (бэкенд)</summary>
    <div class="metric-grid">${cells}</div></details>`;
}

// ---------- Создание/разметка таблицы ----------
// Пустой Spreadsheet ID → попытка создать файл (у сервисного аккаунта обычно
// нулевая квота Drive — бэкенд вернёт понятную инструкцию). Заполненный →
// разметка: вкладка текущего месяца со всеми колонками/формулами из реестра.
document.getElementById("gs-create").addEventListener("click", async () => {
  const status = document.getElementById("gs-create-status");
  if (!_activeProfileId) { status.textContent = "❌ Не выбран бренд."; status.className = "status err"; return; }
  const existing = extractSpreadsheetId(document.getElementById("gs-spreadsheet-id").value.trim());
  if (existing && !confirm("Разметить таблицу: создать вкладку текущего месяца со всеми колонками и формулами и включить managed-режим (формулы будут переписываться при каждом прогоне)?")) return;
  status.textContent = existing ? "Размечаю таблицу…" : "Создаю таблицу…";
  status.className = "status";
  try {
    const url = existing
      ? `/api/profiles/${encodeURIComponent(_activeProfileId)}/sheets/init`
      : `/api/profiles/${encodeURIComponent(_activeProfileId)}/sheets/create`;
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      // init: вставленная в поле ссылка сохраняется в конфиг и размечается
      // именно она (не старая из БД)
      body: JSON.stringify(existing ? { spreadsheet_id: existing } : {}),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    if (data.spreadsheet_id) document.getElementById("gs-spreadsheet-id").value = data.spreadsheet_id;
    const note = data.worksheet ? (data.created ? `вкладка «${data.worksheet}» создана` : `вкладка «${data.worksheet}» уже была`) : "";
    status.innerHTML = `✅ <a href="${escapeHtml(data.url)}" target="_blank">Открыть таблицу</a> ${escapeHtml(note)}`
      + ((data.warnings || []).length ? ` · ⚠ ${escapeHtml(data.warnings.join("; "))}` : "");
    status.className = (data.warnings || []).length ? "status err" : "status ok";
    await loadConfig();  // подтянуть managed_formulas/auto_create_tab в _cfgState
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  }
});

// ============================== Metrics (семантический реестр) ==============================
let _columnsSchema = null;   // [{key, kind, col, label, occurrence, formula, description}] — общий

const KIND_TITLES = {
  base_service: "Запрашиваемые (собираются из сервисов)",
  base_manual: "Ручные (вводятся в таблице, бэкенд читает)",
  computed: "Вычисляемые (формулы; правятся в metrics.py)",
};

async function loadColumnsTab() {
  const status = document.getElementById("columns-status");
  status.textContent = "";
  status.className = "status";
  document.getElementById("columns-save-status").textContent = "";

  if (!_activeProfileId) await loadProfiles();
  const p = activeProfile();
  document.getElementById("columns-profile-name").textContent = (p && p.name) || _activeProfileId || "—";
  if (!_activeProfileId) {
    status.textContent = "❌ Не выбран бренд.";
    status.className = "status err";
    return;
  }

  try {
    // схема per-brand: содержит системные имена и формулы в именах бренда
    const r0 = await fetch(`/api/metrics?profile_id=${encodeURIComponent(_activeProfileId)}`);
    if (!r0.ok) throw new Error("не удалось загрузить реестр метрик");
    _columnsSchema = (await r0.json()).metrics || [];
    const r = await fetch(`/api/profiles/${encodeURIComponent(_activeProfileId)}/config`);
    if (!r.ok) throw new Error("не удалось прочитать конфиг бренда");
    const cfg = await r.json();
    _disabledMetrics = new Set(cfg?.google_sheets?.disabled_metrics || []);
    document.getElementById("gs-cab-start").value = cfg?.google_sheets?.cabinet_start_col ?? "";
    document.getElementById("gs-cab-end").value = cfg?.google_sheets?.cabinet_max_col ?? "";
    renderColumnsTable(cfg?.google_sheets?.column_labels || {},
                       cfg?.google_sheets?.metric_names || {});
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  }
}

let _disabledMetrics = new Set();  // локальный state вкладки до сохранения

function loadColumnsTabRerender(key, disable) {
  if (disable) _disabledMetrics.add(key); else _disabledMetrics.delete(key);
  // перерисовать по текущей схеме с локальным состоянием
  _columnsSchema = _columnsSchema.map(c =>
    c.key === key ? { ...c, disabled: disable } : c);
  const overrides = {}; const names = {};
  document.querySelectorAll("#columns-table [data-col-key]").forEach(i => { if (i.value.trim()) overrides[i.dataset.colKey] = i.value.trim(); });
  document.querySelectorAll("#columns-table [data-name-key]").forEach(i => { if (i.value.trim()) names[i.dataset.nameKey] = i.value.trim(); });
  renderColumnsTable(overrides, names);
}

const KIND_BADGES = {
  base_service: `<span class="kind-tag service" title="Собирается из внешнего сервиса автоматически">сервис</span>`,
  base_manual: `<span class="kind-tag manual" title="Вводится руками в таблице; бэкенд читает, но не пишет">ручная</span>`,
  computed: `<span class="kind-tag formula" title="Считается по формуле — бэкендом и в Google Sheets">формула</span>`,
};

function _manualCabinetRow(label, name, target) {
  const tr = document.createElement("tr");
  tr.dataset.mcRow = "1";
  const isIncome = target === "prihod";
  tr.innerHTML = `
    <td class="col-letter">—</td>
    <td><input type="text" class="inp-sheet" data-mc-label
          value="${escapeHtml(label || "")}" placeholder="Подпись колонки в зоне кабинетов"></td>
    <td class="zone-split"><input type="text" class="inp-sys" data-mc-name
          value="${escapeHtml(name || "")}" placeholder="${escapeHtml(label || "Название в системе")}"></td>
    <td>${KIND_BADGES.base_manual}</td>
    <td class="formula-cell">
      <select data-mc-target class="select" style="font-size:12px; padding:3px 6px;">
        <option value="zatraty"${isIncome ? "" : " selected"}>расход → Затраты</option>
        <option value="prihod"${isIncome ? " selected" : ""}>доход → Приход</option>
      </select>
    </td>
    <td class="desc">суммируется в формулу, коэф 1
      <button type="button" class="danger sm" data-mc-del title="Убрать поле (колонку в листе не трогает)">✕</button>
    </td>`;
  tr.querySelector("[data-mc-del]").addEventListener("click", () => tr.remove());
  return tr;
}

function renderColumnsTable(overrides, nameOverrides) {
  const tbody = document.querySelector("#columns-table tbody");
  tbody.innerHTML = "";
  ["base_service", "base_manual", "computed"].forEach(kind => {
    const group = _columnsSchema.filter(c => c.kind === kind);
    if (!group.length && kind !== "base_manual") return;
    const trh = document.createElement("tr");
    trh.className = "metric-group";
    trh.innerHTML = `<td colspan="6">${KIND_TITLES[kind]}</td>`;
    tbody.appendChild(trh);
    group.forEach(c => {
      const tr = document.createElement("tr");
      if (c.optional && c.disabled) {
        // отключённая опциональная метрика: серым, с кнопкой возврата
        tr.dataset.optKey = c.key;
        tr.dataset.optDisabled = "1";
        tr.innerHTML = `
          <td class="col-letter">${escapeHtml(c.col || "—")}</td>
          <td colspan="4" class="desc" style="font-style: italic;">
            «${escapeHtml(c.label || c.key)}» отключена — не читается и выпала из формул
          </td>
          <td>
            <button type="button" class="secondary sm" data-opt-on>вернуть</button>
          </td>`;
        tr.querySelector("[data-opt-on]").addEventListener("click", () => {
          tr.dataset.optDisabled = "";
          loadColumnsTabRerender(c.key, false);
        });
        tbody.appendChild(tr);
        return;
      }
      const occHint = c.occurrence > 1 ? ` <span style="color:var(--muted); font-size:11px;">(${c.occurrence}-е вхожд.)</span>` : "";
      const what = c.kind === "computed" ? (c.formula || "") : (c.source || (c.kind === "base_manual" ? "ручной ввод в таблице" : ""));
      const labelOvr = overrides[c.key] || "";
      const nameOvr = (nameOverrides || {})[c.key] || "";
      const labelCell = c.label === null
        ? `<span style="color:var(--muted); font-size:12px;">— нет колонки (скрытая)</span>`
        : `<input type="text" class="inp-sheet${labelOvr ? " overridden" : ""}"
                  data-col-key="${escapeHtml(c.key)}" value="${escapeHtml(labelOvr)}"
                  placeholder="${escapeHtml(c.label)}">${occHint}`;
      const nameCell = `<input type="text" class="inp-sys${nameOvr ? " overridden" : ""}"
                  data-name-key="${escapeHtml(c.key)}" value="${escapeHtml(nameOvr)}"
                  placeholder="${escapeHtml(c.system_name_default || c.label || c.key)}">`;
      const delBtn = c.optional
        ? ` <button type="button" class="danger sm" data-opt-off="${escapeHtml(c.key)}"
              title="Отключить метрику для этого бренда (колонку в листе не трогает)">✕</button>`
        : "";
      tr.innerHTML = `
        <td class="col-letter">${escapeHtml(c.col || "—")}</td>
        <td>${labelCell}</td>
        <td class="zone-split">${nameCell}</td>
        <td>${KIND_BADGES[c.kind] || ""}</td>
        <td class="formula-cell">${escapeHtml(what)}</td>
        <td class="desc">${escapeHtml(c.description || "")}${delBtn}</td>`;
      const off = tr.querySelector("[data-opt-off]");
      if (off) off.addEventListener("click", () => loadColumnsTabRerender(c.key, true));
      tbody.appendChild(tr);
    });

    if (kind === "base_manual") {
      // динамические ручные поля расходов (AVITO, Google, …): +/✕
      _columnsSchema.filter(c => c.kind === "manual_cabinet").forEach(c =>
        tbody.appendChild(_manualCabinetRow(
          c.label, c.system_name === c.label ? "" : c.system_name, c.target)));
      const trAdd = document.createElement("tr");
      trAdd.innerHTML = `<td></td><td colspan="5">
        <button type="button" class="secondary sm" id="mc-add">＋ Ручное поле</button>
        <span class="desc" style="margin-left:8px;">
          колонку с такой подписью заведи в зоне кабинетов листа — или она появится при генерации новой вкладки
        </span></td>`;
      trAdd.querySelector("#mc-add").addEventListener("click", () =>
        tbody.insertBefore(_manualCabinetRow("", ""), trAdd));
      tbody.appendChild(trAdd);
    }
  });
  // живое выделение изменённых полей (жирная рамка = есть оверрайд)
  tbody.querySelectorAll("input:not([data-mc-label]):not([data-mc-name])").forEach(inp =>
    inp.addEventListener("input", () =>
      inp.classList.toggle("overridden", !!inp.value.trim())));
}

document.getElementById("columns-save").addEventListener("click", async () => {
  const status = document.getElementById("columns-save-status");
  if (!_activeProfileId || !_columnsSchema) {
    status.textContent = "❌ Не выбран бренд.";
    status.className = "status err";
    return;
  }

  // Пустое поле или совпадение с дефолтом = без оверрайда.
  const labels = {};
  document.querySelectorAll("#columns-table [data-col-key]").forEach(inp => {
    const key = inp.dataset.colKey;
    const val = inp.value.trim();
    const def = (_columnsSchema.find(c => c.key === key) || {}).label || "";
    if (val && val !== def) labels[key] = val;
  });
  const names = {};
  document.querySelectorAll("#columns-table [data-name-key]").forEach(inp => {
    const key = inp.dataset.nameKey;
    const val = inp.value.trim();
    const def = (_columnsSchema.find(c => c.key === key) || {}).system_name_default || "";
    if (val && val !== def) names[key] = val;
  });
  // динамические ручные поля расходов: пустая подпись = строка выброшена
  const manualCabinets = [];
  document.querySelectorAll("#columns-table tr[data-mc-row]").forEach(tr => {
    const label = tr.querySelector("[data-mc-label]").value.trim();
    if (!label) return;
    const name = tr.querySelector("[data-mc-name]").value.trim();
    const target = tr.querySelector("[data-mc-target]").value;
    manualCabinets.push({ label, name: name || label, target });
  });

  status.textContent = "Сохраняю…";
  status.className = "status";
  try {
    // read-modify-write всего конфига: секреты в GET приходят пустыми,
    // сервер восстановит их с диска при сохранении (_merge_preserved_secrets)
    const r = await fetch(`/api/profiles/${encodeURIComponent(_activeProfileId)}/config`);
    if (!r.ok) throw new Error("не удалось прочитать конфиг");
    const cfg = await r.json();
    cfg.google_sheets = cfg.google_sheets || {};
    cfg.google_sheets.column_labels = labels;
    cfg.google_sheets.metric_names = names;
    cfg.google_sheets.manual_cabinets = manualCabinets;
    cfg.google_sheets.disabled_metrics = Array.from(_disabledMetrics);
    cfg.google_sheets.cabinet_start_col = document.getElementById("gs-cab-start").value.trim().toUpperCase();
    cfg.google_sheets.cabinet_max_col = document.getElementById("gs-cab-end").value.trim().toUpperCase();
    const w = await fetch(`/api/profiles/${encodeURIComponent(_activeProfileId)}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await w.json();
    if (!w.ok) throw new Error(data.detail || JSON.stringify(data));
    if (_cfgState?.google_sheets) {
      _cfgState.google_sheets.column_labels = labels;
      _cfgState.google_sheets.metric_names = names;
    }
    await loadColumnsTab();  // перерисовать формулы с новыми именами
    status.textContent = "✅ Сохранено";
    status.className = "status ok";
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  }
});

// ============================== History ==============================
async function loadHistory() {
  try {
    const r = await fetch("/api/reports");
    const data = await r.json();
    _historyItems = data.items || [];
    fillHistoryFilter();
    renderHistory();
  } catch (e) {
    console.error(e);
  }
}

let _historyItems = [];

function fillHistoryFilter() {
  const sel = document.getElementById("history-brand-filter");
  const cur = sel.value;
  const ids = new Set();
  _profilesState.forEach(p => ids.add(p.id));
  _historyItems.forEach(it => { if (it.profile_id) ids.add(it.profile_id); });
  const nameById = {};
  _profilesState.forEach(p => { nameById[p.id] = p.name; });
  sel.innerHTML = `<option value="">Все бренды</option>` +
    [...ids].sort().map(id => `<option value="${escapeHtml(id)}">${escapeHtml(nameById[id] || id)}</option>`).join("");
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
}

function renderHistory() {
  const tbody = document.querySelector("#history-table tbody");
  tbody.innerHTML = "";
  const filter = document.getElementById("history-brand-filter").value;
  const items = filter
    ? _historyItems.filter(it => it.profile_id === filter)
    : _historyItems;
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="6" style="color: var(--muted);">Пусто. Сгенерируй отчёт во вкладке «Отчёт» или прогони бренд в «Обзоре».</td></tr>`;
    return;
  }
  const nameById = {};
  _profilesState.forEach(p => { nameById[p.id] = p.name; });
  items.forEach(item => {
    const tr = document.createElement("tr");
    const status = item.ok
      ? `<span class="status ok" style="margin:0;">✅ ${escapeHtml(item.trigger || "")}</span>`
        + (item.google_sheets_error
            ? ` <span class="status err" style="margin:0; font-size:11px;" title="${escapeHtml(item.google_sheets_error)}">Sheets!</span>`
            : "")
      : `<span class="status err" style="margin:0;" title="${escapeHtml(item.error || "")}">❌ ${escapeHtml(item.trigger || "")}</span>`;
    tr.innerHTML = `
      <td>${escapeHtml(nameById[item.profile_id] || item.profile_id)}</td>
      <td>${escapeHtml(item.date)}</td>
      <td>${escapeHtml(item.sub1 || "")}</td>
      <td>${status}</td>
      <td>${escapeHtml((item.finished_at || "").replace("T", " "))}</td>
      <td><button class="secondary" ${item.has_report ? "" : "disabled"}
                  data-pid="${escapeHtml(item.profile_id)}" data-date="${escapeHtml(item.date)}">Открыть</button></td>
    `;
    tr.querySelector("button").addEventListener("click", async (ev) => {
      const { pid, date } = ev.target.dataset;
      const rr = await fetch(`/api/reports/${encodeURIComponent(pid)}/${encodeURIComponent(date)}`);
      const data = await rr.json();
      const pre = document.getElementById("history-preview");
      pre.textContent = JSON.stringify(data, null, 2);
      pre.classList.remove("hidden");
      pre.scrollIntoView({ behavior: "smooth" });
    });
    tbody.appendChild(tr);
  });
}

document.getElementById("history-reload").addEventListener("click", loadHistory);
document.getElementById("history-brand-filter").addEventListener("change", renderHistory);

// ============================== Bootstrap ==============================
loadProfiles();
