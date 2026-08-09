const state = {
  days: 30,
  stage: "",
};

const stageLabels = {
  recommended: "推薦",
  saved: "已儲存",
  applied: "已申請",
  interview: "面試",
  offer: "Offer",
  rejected: "未錄取",
  archived: "封存",
};

const pct = (value) => `${(Number(value || 0) * 100).toFixed(1)}%`;
const score = (value) => (Number(value || 0) * 100).toFixed(0);
const html = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        character
      ],
  );
const safeStage = (value) => (Object.hasOwn(stageLabels, value) ? value : "recommended");
const safeJobUrl = (value) => {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? html(url.href) : "#";
  } catch {
    return "#";
  }
};
const text = (id, value) => {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
};

function formatTime(value) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-Hant", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || response.statusText);
  }
  return response.json();
}

function renderBars(kpis) {
  const max = Math.max(kpis.recommended, 1);
  const setWidth = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.style.width = `${Math.max(3, (Number(value || 0) / max) * 100)}%`;
  };
  setWidth("bar-recommended", kpis.recommended);
  setWidth("bar-applied", kpis.applied);
  setWidth("bar-interviews", kpis.interviews);
  text("bar-recommended-value", kpis.recommended);
  text("bar-applied-value", kpis.applied);
  text("bar-interviews-value", kpis.interviews);
}

function renderMetricRows(hostId, rows, sourceMode = false) {
  const host = document.getElementById(hostId);
  if (!host) return;
  if (!rows.length) {
    host.innerHTML = '<div class="empty">目前沒有資料</div>';
    return;
  }
  host.innerHTML = rows
    .map((row) => {
      const applied = Number(row.applied || 0);
      const interviews = Number(row.interviews || 0);
      const recommended = Number(row.recommended || 0);
      const rate = applied ? interviews / applied : 0;
      const width = Math.max(3, rate * 100);
      const name = sourceMode ? row.source : row.industry;
      return `
        <div class="metric-row">
          <div>
            <strong>${html(name)}</strong>
            <small>${recommended} 推薦 · ${applied} 申請 · ${interviews} 面試</small>
          </div>
          <div class="rail" aria-hidden="true"><i style="width:${width}%"></i></div>
          <em>${pct(rate)}</em>
        </div>
      `;
    })
    .join("");
}

function renderQueue(rows) {
  const host = document.getElementById("queue-list");
  if (!host) return;
  if (!rows.length) {
    host.innerHTML = '<div class="empty">沒有待處理職缺</div>';
    return;
  }
  host.innerHTML = rows
    .slice(0, 6)
    .map(
      (job) => `
      <article class="queue-item">
        <div>
          <div class="title-line">${html(job.title)}</div>
          <div class="meta-line">${html(job.company)} · ${html(job.location_raw || "未列地點")}</div>
        </div>
        <span class="badge ${safeStage(job.stage)}">${html(stageLabels[safeStage(job.stage)])}</span>
      </article>
    `,
    )
    .join("");
}

function renderJobs(rows) {
  const host = document.getElementById("jobs-table");
  if (!host) return;
  if (!rows.length) {
    host.innerHTML = '<tr><td colspan="5" class="empty">目前沒有符合條件的職缺</td></tr>';
    return;
  }
  host.innerHTML = rows
    .map(
      (job) => `
      <tr>
        <td class="job-cell">
          <div class="title-line">${html(job.title)}</div>
          <div class="meta-line">${html(job.company)} · ${html(job.location_raw || "未列地點")}</div>
        </td>
        <td>${html(job.industry)}</td>
        <td class="score">${score(job.score)}</td>
        <td>
          <select class="stage-select" data-job-id="${html(job.id)}" aria-label="更新 ${html(job.title)} 狀態">
            ${Object.entries(stageLabels)
              .filter(([value]) => value !== "archived")
              .map(
                ([value, label]) =>
                  `<option value="${value}" ${job.stage === value ? "selected" : ""}>${label}</option>`,
              )
              .join("")}
          </select>
        </td>
        <td><a class="open-link" href="${safeJobUrl(job.canonical_url)}" target="_blank" rel="noopener noreferrer">開啟</a></td>
      </tr>
    `,
    )
    .join("");

  document.querySelectorAll(".stage-select").forEach((select) => {
    select.addEventListener("change", async (event) => {
      const target = event.currentTarget;
      target.disabled = true;
      try {
        await api(`/api/jobs/${target.dataset.jobId}/application`, {
          method: "PATCH",
          body: JSON.stringify({ stage: target.value }),
        });
        await loadAll();
      } finally {
        target.disabled = false;
      }
    });
  });
}

async function loadDashboard() {
  const data = await api(`/api/dashboard?days=${state.days}`);
  const { kpis } = data;
  text("kpi-recommended", kpis.recommended);
  text("kpi-applied", kpis.applied);
  text("kpi-interviews", kpis.interviews);
  text("kpi-apply-rate", pct(kpis.apply_rate));
  text("kpi-interview-rate", pct(kpis.interview_rate));
  text("kpi-total-rate", pct(kpis.total_rate));
  text("generated-at", `更新 ${formatTime(data.generated_at)}`);
  renderBars(kpis);
  renderMetricRows("industry-list", data.industries);
  renderMetricRows("source-list", data.sources, true);
  renderQueue(data.queue);
}

async function loadJobs() {
  const params = new URLSearchParams({ days: String(state.days), limit: "80" });
  if (state.stage) params.set("stage", state.stage);
  const data = await api(`/api/jobs?${params.toString()}`);
  renderJobs(data.jobs);
}

async function loadAll() {
  await Promise.all([loadDashboard(), loadJobs()]);
}

document.querySelectorAll("[data-days]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-days]").forEach((peer) => {
      peer.classList.toggle("is-active", peer === button);
    });
    state.days = Number(button.dataset.days);
    loadAll();
  });
});

document.getElementById("stage-filter")?.addEventListener("change", (event) => {
  state.stage = event.currentTarget.value;
  loadJobs();
});

loadAll().catch((error) => {
  console.error(error);
  document.body.insertAdjacentHTML(
    "afterbegin",
    '<div class="shell"><div class="panel">Dashboard 載入失敗，請確認 DATABASE_URL 與資料表 migration 已完成。</div></div>',
  );
});
