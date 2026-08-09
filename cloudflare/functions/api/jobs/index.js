import { daysFromUrl, fetchMatchedJobs, jsonError } from "../../_lib/supabase.js";

export async function onRequestGet({ env, request }) {
  try {
    const url = new URL(request.url);
    const days = daysFromUrl(request.url);
    const limit = Math.min(200, Math.max(1, Number(url.searchParams.get("limit") || "80")));
    const stage = url.searchParams.get("stage");
    const industry = url.searchParams.get("industry");
    let jobs = await fetchMatchedJobs(env, days, limit);
    if (stage) jobs = jobs.filter((job) => job.stage === stage);
    if (industry) jobs = jobs.filter((job) => job.industry === industry);
    return Response.json({ jobs: jobs.slice(0, limit) });
  } catch (error) {
    if (error instanceof Response) return error;
    return jsonError(error.message);
  }
}
