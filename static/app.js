// ------ Tabs ------
function activateTab(name) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  document.getElementById(`tab-${name}`).classList.add("active");
  if (name === "settings") loadConfig();
  if (name === "history") loadHistory();
}

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab));
});

// ------ Profiles ------
let _activeProfileId = null;
let _profilesState = [];   // [{id, name, sub1, is_active}]

function activeProfile() {
  return _profilesState.find(p => p.id === _activeProfileId) || null;
}

async function loadProfiles() {
  try {
    const r = await fetch("/api/profiles");
    const data = await r.json();
    _profilesState = data.profiles || [];
    _activeProfileId = data.active_id || (_profilesState[0] && _profilesState[0].id) || null;
  } catch {
    _profilesState = [];
    _activeProfileId = null;
  }
  renderProfileBar();
  prefillReportSub1();
}

function renderProfileBar() {
  const bar = document.getElementById("profile-bar");
  bar.innerHTML = "";
  _profilesState.forEach(p => {
    const btn = document.createElement("button");
    btn.className = "profile-btn" + (p.id === _activeProfileId ? " active" : "");
    btn.textContent = p.name;
    btn.title = `sub1=${p.sub1}`;
    btn.addEventListener("click", () => activateProfile(p.id));
    bar.appendChild(btn);
  });

  const actions = document.createElement("span");
  actions.className = "profile-actions";
  const last = _profilesState.length <= 1;
  actions.innerHTML = `
    <button class="secondary" id="profile-add" title="Создать профиль">＋</button>
    <button class="secondary" id="profile-copy" title="Копировать активный профиль">⧉</button>
    <button class="secondary" id="profile-rename" title="Переименовать активный">✎</button>
    <button class="danger" id="profile-delete" title="Удалить активный"${last ? " disabled" : ""}>🗑</button>`;
  bar.appendChild(actions);

  document.getElementById("profile-add").addEventListener("click", () => createProfile(false));
  document.getElementById("profile-copy").addEventListener("click", () => createProfile(true));
  document.getElementById("profile-rename").addEventListener("click", renameActiveProfile);
  document.getElementById("profile-delete").addEventListener("click", deleteActiveProfile);
}

function prefillReportSub1() {
  const p = activeProfile();
  const el = document.getElementById("report-sub1");
  if (p && el) el.value = p.sub1 || "kub";
}

async function activateProfile(id) {
  if (id === _activeProfileId) return;
  try {
    await fetch(`/api/profiles/${encodeURIComponent(id)}/activate`, { method: "POST" });
  } catch {}
  _activeProfileId = id;
  renderProfileBar();
  prefillReportSub1();
  if (document.getElementById("tab-settings").classList.contains("active")) loadConfig();
}

async function createProfile(copy) {
  const p = activeProfile();
  const def = copy ? `${p ? p.name : "Профиль"} (копия)` : "Новый профиль";
  const name = prompt(copy ? "Имя нового профиля (копия активного):" : "Имя нового профиля:", def);
  if (!name || !name.trim()) return;
  const body = { name: name.trim() };
  if (copy && _activeProfileId) body.copy_from = _activeProfileId;
  try {
    const r = await fetch("/api/profiles", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Ошибка");
    await loadProfiles();   // бэкенд уже сделал новый профиль активным
    activateTab("settings");
  } catch (e) {
    alert("❌ " + e.message);
  }
}

async function renameActiveProfile() {
  const p = activeProfile();
  if (!p) return;
  const name = prompt("Новое имя профиля:", p.name);
  if (!name || !name.trim()) return;
  try {
    const r = await fetch(`/api/profiles/${encodeURIComponent(_activeProfileId)}`, {
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

async function deleteActiveProfile() {
  if (_profilesState.length <= 1) return;
  const p = activeProfile();
  if (!confirm(`Удалить профиль «${p ? p.name : _activeProfileId}»? Это необратимо.`)) return;
  try {
    const r = await fetch(`/api/profiles/${encodeURIComponent(_activeProfileId)}`, { method: "DELETE" });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Ошибка");
    await loadProfiles();
    if (document.getElementById("tab-settings").classList.contains("active")) loadConfig();
  } catch (e) {
    alert("❌ " + e.message);
  }
}

// ------ Report ------
const today = new Date().toISOString().slice(0, 10);
document.getElementById("report-date").value = today;

document.getElementById("report-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const date = document.getElementById("report-date").value;
  const sub1 = document.getElementById("report-sub1").value.trim();
  const status = document.getElementById("report-status");
  const result = document.getElementById("report-result");

  if (!_activeProfileId) {
    status.textContent = "❌ Нет активного профиля. Создай его во вкладке «Настройки».";
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

// ------ Settings ------
let _cfgState = null;

async function loadConfig() {
  if (!_activeProfileId) await loadProfiles();
  if (!_activeProfileId) {
    _cfgState = emptyConfig();
    renderConfig(_cfgState);
    loadScheduleSettings();
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
  loadScheduleSettings();

  const p = activeProfile();
  document.getElementById("settings-profile-name").textContent = _cfgState.name || (p && p.name) || "—";
  document.getElementById("profile-sub1").value = _cfgState.sub1 || (p && p.sub1) || "kub";
}

const DEFAULT_ADS_MANAGER_BASE_URL = "https://kybyshka-dev.ru";

function emptyConfig() {
  return {
    name: "", sub1: "kub",
    leadstech: { base_url: "https://api.leads.tech", login: "", password: "", page_size: 500 },
    ads_manager: { base_url: DEFAULT_ADS_MANAGER_BASE_URL, username: "", password: "" },
    yandex: { base_url: "", username: "", password: "" },
    yandex_metrika: { oauth_token: "", counter_id: 0, goals: ["Zayvka"], attribution: "LASTSIGN" },
    eightconnect: { base_url: "https://8connect.ru", login: "", password: "",
                    category_ids: [149, 395, 620, 624],
                    scheme_ids: [1006, 2260, 2805, 2809, 612] },
    google_sheets: { enabled: false, spreadsheet_id: "", service_account_json_path: "cfg/service_account.json" },
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

  document.getElementById("ec-login").value = cfg.eightconnect?.login ?? "";
  document.getElementById("ec-password").value = cfg.eightconnect?.password ?? "";
  const categoryIds = Array.isArray(cfg.eightconnect?.category_ids) && cfg.eightconnect.category_ids.length
    ? cfg.eightconnect.category_ids
    : [149, 395, 620, 624];
  document.getElementById("ec-category-ids").value = categoryIds.join(", ");
  const schemeIds = Array.isArray(cfg.eightconnect?.scheme_ids) && cfg.eightconnect.scheme_ids.length
    ? cfg.eightconnect.scheme_ids
    : [1006, 2260, 2805, 2809, 612];
  document.getElementById("ec-scheme-ids").value = schemeIds.join(", ");

  document.getElementById("gs-spreadsheet-id").value = cfg.google_sheets?.spreadsheet_id ?? "";
  document.getElementById("gs-enabled").checked = !!cfg.google_sheets?.enabled;
  renderSheetLink();
}

// Общее расписание (не зависит от профиля) — из манифеста.
async function loadScheduleSettings() {
  try {
    const r = await fetch("/api/schedule-settings");
    const s = await r.json();
    document.getElementById("sched-enabled").checked = !!s.enabled;
    document.getElementById("sched-time").value = s.time || "09:00";
  } catch {
    document.getElementById("sched-time").value = "09:00";
  }
  loadScheduleStatus();
}

async function saveScheduleSettings() {
  const status = document.getElementById("sched-save-status");
  const payload = {
    enabled: document.getElementById("sched-enabled").checked,
    time: document.getElementById("sched-time").value.trim() || "09:00",
  };
  status.textContent = "Сохраняю…";
  status.className = "status";
  try {
    const r = await fetch("/api/schedule-settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    status.textContent = "✅ Сохранено";
    status.className = "status ok";
    loadScheduleStatus();
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  }
}

document.getElementById("sched-save").addEventListener("click", saveScheduleSettings);

async function loadScheduleStatus() {
  const el = document.getElementById("sched-status");
  try {
    const r = await fetch("/api/schedule");
    const s = await r.json();
    if (!s.enabled) {
      el.textContent = "Планировщик выключен.";
      el.className = "status";
    } else {
      const next = s.next_run ? s.next_run.replace("T", " ") : "—";
      let txt = `✅ Активен. Следующий запуск: ${next} (Samara)`;
      if (s.last_run) {
        const lr = s.last_run;
        const when = (lr.finished_at || lr.started_at || "").replace("T", " ");
        if (Array.isArray(lr.results)) {
          const okN = lr.results.filter(x => x.ok).length;
          txt += ` · последний: ${when} → ${okN}/${lr.results.length} профилей ок`;
        } else if (lr.ok) {
          txt += ` · последний: ${when} → ${lr.saved_to || "ok"}`;
        } else {
          txt += ` · последний упал: ${when} (${lr.error || "см. логи"})`;
        }
      }
      el.textContent = txt;
      el.className = "status ok";
    }
  } catch (e) {
    el.textContent = "Не удалось получить статус планировщика";
    el.className = "status err";
  }
}

document.getElementById("sched-run-now").addEventListener("click", async () => {
  const status = document.getElementById("sched-run-status");
  status.textContent = "Прогоняю все профили…";
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
    loadScheduleStatus();
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
    status.textContent = "❌ Нет активного профиля.";
    status.className = "status err";
    return;
  }

  const payload = {
    name: (activeProfile()?.name) || _cfgState?.name || "",
    sub1: document.getElementById("profile-sub1").value.trim() || "kub",
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
    await loadProfiles();   // name/sub1 в полосе могли измениться
  } catch (e) {
    status.textContent = "❌ " + e.message;
    status.className = "status err";
  }
});

// ------ History ------
async function loadHistory() {
  try {
    const r = await fetch("/api/reports");
    const data = await r.json();
    const tbody = document.querySelector("#history-table tbody");
    tbody.innerHTML = "";
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="4" style="color: var(--muted);">Пока пусто. Сгенерируй отчёт во вкладке «Отчёт».</td></tr>`;
      return;
    }
    data.items.forEach(item => {
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
  } catch (e) {
    console.error(e);
  }
}

document.getElementById("history-reload").addEventListener("click", loadHistory);

// ------ Bootstrap ------
loadProfiles();
