import cloudflareAccessPlugin from "@cloudflare/pages-plugin-cloudflare-access";

export async function onRequest(context) {
  const domain = context.env.TEAM_DOMAIN;
  const aud = context.env.POLICY_AUD;
  if (!domain || !aud) {
    return new Response("Cloudflare Access is not configured.", { status: 503 });
  }
  if (!/^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/i.test(domain)) {
    return new Response("TEAM_DOMAIN is invalid.", { status: 503 });
  }
  return cloudflareAccessPlugin({ domain, aud })(context);
}
