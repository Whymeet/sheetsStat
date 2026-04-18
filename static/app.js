// ------ Tabs ------
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "settings") loadConfig();
    if (btn.dataset.tab === "history") loadHistory();
  });
});

// ------ Report ------
const today = new Date().toISOString().slice(0, 10);
document.getElementById("report-date").value = today;

document.getElementById("report-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const date = document.getElementById("report-date").value;
  const sub1 = document.getElementById("report-sub1").value.trim();
  const status = document.getElementById("report-status");
  const result = document.getElementById("report-result");

  status.textContent = "Считаю… это может занять 10–30 секунд.";
  status.className = "status";
  result.classList.add("hidden");

  try {
    const r = await fetch("/api/report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date, sub1 }),
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
  try {
    const r = await fetch("/api/config");
    if (r.status === 404) { _cfgState = emptyConfig(); }
    else { _cfgState = await r.json(); }
  } catch {
    _cfgState = emptyConfig();
  }
  renderConfig(_cfgState);
}

const DEFAULT_ADS_MANAGER_BASE_URL = "https://kybyshka-dev.ru";

function emptyConfig() {
  return {
    leadstech: { base_url: "https://api.leads.tech", login: "", password: "", page_size: 500 },
    ads_manager: { base_url: DEFAULT_ADS_MANAGER_BASE_URL, username: "", password: "" },
    yandex: { base_url: "", username: "", password: "" },
    yandex_metrika: { oauth_token: "", counter_id: 0, goals: ["Zayvka"], attribution: "LASTSIGN" },
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
}

document.getElementById("save-config").addEventListener("click", async () => {
  const status = document.getElementById("save-status");

  const payload = {
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
    analysis: _cfgState?.analysis || { lookback_days: 7 },
  };

  status.textContent = "Сохраняю…";
  status.className = "status";

  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    status.textContent = "✅ Сохранено";
    status.className = "status ok";
    _cfgState = payload;
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
