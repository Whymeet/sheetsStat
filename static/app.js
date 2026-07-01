// ============================== Tabs ==============================
function activateTab(name) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  document.getElementById(`tab-${name}`).classList.add("active");
  if (name === "overview") renderOverview();
  if (name === "settings") loadConfig();
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
      <td>${p.sheets_enabled ? '<span class="dot dot-sheets"></span>' : '<span class="muted">—</span>'}</td>
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
      status.textContent = `✅ «${p ? p.name : pid}» (${data.date}): ${res.saved_to || "ok"}${res.google_sheets_error ? ` · Sheets: ${res.google_sheets_error}` : ""}`;
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

  status.textContent = "Считаю… это может занять 10–30 секунд.";
  status.className = "status";
  result.classList.add("hidden");

  try {
    const r = await fetch("/api/report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: _activeProfileId, date, sub1 }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    renderReport(data);
    status.textContent = data._saved_to ? `✅ Сохранено в output/${data._saved_to}` : (data.warning || "Готово");
    status.className = data.warning ? "status err" : "status ok";
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
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

document.getElementById("report-range-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const start = document.getElementById("report-start").value;
  const end = document.getElementById("report-end").value;
  const sub1 = document.getElementById("report-range-sub1").value.trim();
  const btn = document.getElementById("report-range-submit");
  const status = document.getElementById("report-status");
  const progress = document.getElementById("range-progress");
  const rangeStatus = document.getElementById("range-status");
  const rangeLog = document.getElementById("range-log");
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
  rangeLog.innerHTML = "";
  btn.disabled = true;

  let lastData = null;
  let stopped = false;
  try {
    for (let i = 0; i < dates.length; i++) {
      const date = dates[i];
      rangeStatus.textContent = `День ${i + 1} из ${dates.length}: ${date}…`;
      rangeStatus.className = "status";
      try {
        const r = await fetch("/api/report", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile_id: _activeProfileId, date, sub1 }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || r.statusText);
        const gsErr = data.google_sheets && data.google_sheets.error;
        if (gsErr) throw new Error(`Sheets: ${gsErr}`);
        lastData = data;
        const warns = reportSourceErrors(data);
        if (warns.length) {
          appendRangeLine(rangeLog, `⚠️ ${date}: ${warns.join(" | ")}`, "warn");
        } else {
          appendRangeLine(rangeLog, `✅ ${date}`, "ok");
        }
      } catch (e) {
        appendRangeLine(rangeLog, `❌ ${date}: ${e.message}`, "err");
        rangeStatus.textContent = `❌ Остановлено на ${date} (${i + 1} из ${dates.length})`;
        rangeStatus.className = "status err";
        stopped = true;
        break;
      }
    }
    if (!stopped) {
      rangeStatus.textContent = `✅ Готово: ${dates.length} дн.`;
      rangeStatus.className = "status ok";
    }
  } finally {
    btn.disabled = false;
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

function appendRangeLine(container, text, cls) {
  const line = document.createElement("div");
  line.className = "range-line " + (cls || "");
  line.textContent = text;
  container.appendChild(line);
  container.scrollTop = container.scrollHeight;
}

function renderReport(data) {
  const result = document.getElementById("report-result");
  result.classList.remove("hidden");

  document.getElementById("report-summary").innerHTML =
    `<div style="color: var(--muted); font-size: 13px;">
      ${data.date} · sub1=<b>${data.sub1}</b> · кабинетов: <b>${data.cabinet_count}</b>
      ${data.warning ? `<br><span class="status err">${data.warning}</span>` : ""}
    </div>`;

  const gs = data.google_sheets;
  const gsEl = document.getElementById("report-gs");
  if (!gs || gs.enabled === false) {
    gsEl.textContent = "";
    gsEl.className = "status";
  } else if (gs.error) {
    gsEl.textContent = `Google Sheets: ❌ ${gs.error}`;
    gsEl.className = "status err";
  } else {
    const nMatched = (gs.matched || []).length;
    const nUnmatched = (gs.unmatched || []).length;
    const fallbackRange = nUnmatched
      ? ` · fallback A${gs.unmatched[0].row}…A${gs.unmatched[nUnmatched - 1].row}`
      : "";
    gsEl.textContent = `Google Sheets ✅ «${gs.worksheet}», строка ${gs.date_row} · matched ${nMatched}, unmatched ${nUnmatched}${fallbackRange}`;
    gsEl.className = "status ok";
  }

  // Ads Manager table
  const tbody = document.querySelector("#cabinets-table tbody");
  tbody.innerHTML = "";
  const ads = data.ads_manager || { cabinets: {}, total: 0 };
  Object.entries(ads.cabinets || {}).forEach(([name, spent]) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(name)}</td><td class="num">${fmtMoney(spent)}</td>`;
    tbody.appendChild(tr);
  });
  document.getElementById("ads-total").textContent = fmtMoney(ads.total);

  // Yandex Direct table
  const yxTbody = document.querySelector("#yandex-table tbody");
  yxTbody.innerHTML = "";
  const yx = data.yandex || { cabinets: {}, total: 0 };
  Object.entries(yx.cabinets || {}).forEach(([name, spent]) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(name)}</td><td class="num">${fmtMoney(spent)}</td>`;
    yxTbody.appendChild(tr);
  });
  document.getElementById("yandex-total").textContent = fmtMoney(yx.total);

  // Yandex Metrika
  const ym = data.yandex_metrika || {};
  const fmtInt = (v) => (v ?? 0).toLocaleString("ru-RU");
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

  // 8connect
  const ec = data.eightconnect || {};
  document.getElementById("ec-cost").textContent = fmtMoney(ec.cost);
  document.getElementById("ec-charge").textContent = fmtMoney(ec.charge);
  const ecSchemes = Array.isArray(ec.scheme_ids) ? ec.scheme_ids : [];
  document.getElementById("ec-scheme-list").textContent = ecSchemes.length ? ecSchemes.join(", ") : "—";
  const ecErrEl = document.getElementById("ec-error");
  const ecErr = (ec.errors && ec.errors[0] && ec.errors[0].error) || "";
  ecErrEl.textContent = ecErr ? `❌ ${ecErr}` : "";

  document.getElementById("report-raw").textContent = JSON.stringify(data, null, 2);
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
    leadstech: { base_url: "https://api.leads.tech", login: "", password: "", page_size: 500 },
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

function renderConfig(cfg) {
  document.getElementById("lt-login").value = cfg.leadstech?.login ?? "";
  document.getElementById("lt-password").value = cfg.leadstech?.password ?? "";
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
      ? ` · последний: ${lastRunWhen(lr)} → ${lr.saved_to || "ok"}`
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
      ? `✅ ${data.date}: ${res.saved_to || "ok"}${res.google_sheets_error ? ` · Sheets: ${res.google_sheets_error}` : ""}`
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
      login: document.getElementById("lt-login").value.trim(),
      password: document.getElementById("lt-password").value,
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
  // pid вытаскиваем из имени файла "<date>_<sub1>__<pid>.json"
  _historyItems.forEach(it => {
    const m = it.name.match(/__([a-z0-9-]+)\.json$/i);
    if (m) ids.add(m[1]);
  });
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
    ? _historyItems.filter(it => it.name.match(new RegExp(`__${filter.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\.json$`, "i")))
    : _historyItems;
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="4" style="color: var(--muted);">Пусто. Сгенерируй отчёт во вкладке «Отчёт» или прогони бренд в «Обзоре».</td></tr>`;
    return;
  }
  items.forEach(item => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(item.name)}</td>
      <td>${item.size} B</td>
      <td>${item.mtime.replace("T", " ")}</td>
      <td><button class="secondary" data-view="${escapeHtml(item.name)}">Открыть</button></td>
    `;
    tr.querySelector("button").addEventListener("click", async (ev) => {
      const name = ev.target.dataset.view;
      const rr = await fetch(`/api/reports/${encodeURIComponent(name)}`);
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
