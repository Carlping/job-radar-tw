import { daysFromUrl, fetchMatchedJobs, groupRows, jsonError, summarize } from "../_lib/supabase.js";

export async function onRequestGet({ env, request }) {
  try {
    const days = daysFromUrl(request.url);
    const rows = await fetchMatchedJobs(env, days, 200);
    return Response.json({
      window_days: days,
      generated_at: new Date().toISOString(),
      kpis: summarize(rows),
      industries: groupRows(rows, "industry"),
      stages: groupRows(rows, "stage"),
      sources: groupRows(rows, "source").map(({ source, ...rest }) => ({ source, ...rest })),
      queue: rows.filter((job) => job.stage !== "archived").slice(0, 8),
    });
  } catch (error) {
    if (error instanceof Response) return error;
    return jsonError(error.message);
  }
}
