const REST_HEADERS = {
  "Content-Type": "application/json",
};

export const APPLICATION_STAGES = new Set([
  "recommended",
  "saved",
  "applied",
  "interview",
  "offer",
  "rejected",
  "archived",
]);

export function requireEnv(env) {
  const missing = ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"].filter((key) => !env[key]);
  if (missing.length) {
    throw new Response(`Missing Cloudflare secret(s): ${missing.join(", ")}`, { status: 500 });
  }
}

export function jsonError(message, status = 500) {
  return Response.json({ detail: message }, { status });
}

export function daysFromUrl(url) {
  const value = Number(new URL(url).searchParams.get("days") || "30");
  return Math.min(365, Math.max(1, Number.isFinite(value) ? value : 30));
}

export function sinceIso(days) {
  return new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
}

export function supabaseHeaders(env, extra = {}) {
  return {
    ...REST_HEADERS,
    apikey: env.SUPABASE_SERVICE_ROLE_KEY,
    Authorization: `Bearer ${env.SUPABASE_SERVICE_ROLE_KEY}`,
    ...extra,
  };
}

export async function supabaseFetch(env, path, init = {}) {
  requireEnv(env);
  const base = env.SUPABASE_URL.replace(/\/$/, "");
  const response = await fetch(`${base}/rest/v1/${path}`, {
    ...init,
    headers: supabaseHeaders(env, init.headers),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Response(detail || response.statusText, { status: response.status });
  }
  if (response.status === 204) return null;
  return response.json();
}

export async function fetchMatchedJobs(env, days, limit = 200) {
  const params = new URLSearchParams({
    select:
      "id,title,location_raw,canonical_url,first_seen_at,source_posted_at,status,companies(name,industry,ats_type),applications(stage,notes,first_applied_at,first_interview_at,updated_at),match_results!inner(score,eligible)",
    status: "eq.active",
    first_seen_at: `gte.${sinceIso(days)}`,
    "match_results.eligible": "eq.true",
    order: "first_seen_at.desc",
    limit: String(limit),
  });
  const rows = await supabaseFetch(env, `jobs?${params.toString()}`);
  return rows.map(normalizeJob).sort((a, b) => b.score - a.score);
}

export function normalizeJob(row) {
  const scores = Array.isArray(row.match_results)
    ? row.match_results.map((item) => Number(item.score || 0))
    : [];
  const application = Array.isArray(row.applications) ? row.applications[0] : row.applications;
  const company = Array.isArray(row.companies) ? row.companies[0] : row.companies;
  return {
    id: row.id,
    title: row.title,
    location_raw: row.location_raw,
    canonical_url: row.canonical_url,
    first_seen_at: row.first_seen_at,
    source_posted_at: row.source_posted_at,
    status: row.status,
    company: company?.name || "Unknown",
    industry: company?.industry || "unknown",
    source: company?.ats_type || "unknown",
    score: scores.length ? Math.max(...scores) : 0,
    stage: application?.stage || "recommended",
    notes: application?.notes || null,
    first_applied_at: application?.first_applied_at || null,
    first_interview_at: application?.first_interview_at || null,
    application_updated_at: application?.updated_at || null,
  };
}

export function summarize(rows) {
  const recommended = rows.length;
  const applied = rows.filter((job) => job.first_applied_at).length;
  const interviews = rows.filter((job) => job.first_interview_at).length;
  return {
    recommended,
    applied,
    interviews,
    apply_rate: recommended ? applied / recommended : 0,
    interview_rate: applied ? interviews / applied : 0,
    total_rate: recommended ? interviews / recommended : 0,
  };
}

export function groupRows(rows, key) {
  const grouped = new Map();
  for (const job of rows) {
    const name = job[key] || "unknown";
    const item = grouped.get(name) || { [key]: name, recommended: 0, applied: 0, interviews: 0 };
    item.recommended += 1;
    if (job.first_applied_at) item.applied += 1;
    if (job.first_interview_at) item.interviews += 1;
    grouped.set(name, item);
  }
  return [...grouped.values()].sort((a, b) => {
    const ar = a.applied ? a.interviews / a.applied : 0;
    const br = b.applied ? b.interviews / b.applied : 0;
    return br - ar || b.recommended - a.recommended;
  });
}
