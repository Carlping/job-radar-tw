import { APPLICATION_STAGES, jsonError, supabaseFetch } from "../../../_lib/supabase.js";

const ADVANCED_STAGES = new Set(["saved", "applied", "interview", "offer"]);
const APPLIED_STAGES = new Set(["applied", "interview", "offer"]);
const INTERVIEW_STAGES = new Set(["interview", "offer"]);

export async function onRequestPatch({ env, params, request }) {
  try {
    const jobId = params.jobId;
    const payload = await request.json();
    const stage = String(payload.stage || "");
    if (!APPLICATION_STAGES.has(stage)) {
      return jsonError(`stage must be one of: ${[...APPLICATION_STAGES].sort().join(", ")}`, 422);
    }

    const existing = await supabaseFetch(
      env,
      `applications?job_id=eq.${encodeURIComponent(jobId)}&select=job_id,first_saved_at,first_applied_at,first_interview_at`,
    );
    const current = existing[0] || {};
    const now = new Date().toISOString();
    const body = {
      job_id: jobId,
      stage,
      updated_at: now,
    };
    if (Object.hasOwn(payload, "notes")) body.notes = payload.notes;
    if (ADVANCED_STAGES.has(stage) && !current.first_saved_at) body.first_saved_at = now;
    if (APPLIED_STAGES.has(stage) && !current.first_applied_at) body.first_applied_at = now;
    if (INTERVIEW_STAGES.has(stage) && !current.first_interview_at) body.first_interview_at = now;

    const result = await supabaseFetch(env, "applications?on_conflict=job_id", {
      method: "POST",
      headers: { Prefer: "resolution=merge-duplicates,return=representation" },
      body: JSON.stringify(body),
    });
    return Response.json({ application: result[0] });
  } catch (error) {
    if (error instanceof Response) return error;
    return jsonError(error.message);
  }
}
