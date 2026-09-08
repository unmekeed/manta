const routes = [/^matches$/, /^matches\/\d+\/analysis$/, /^matches\/\d+\/timeline$/, /^heroes$/, /^draft\/simulate$/];

async function forward(request: Request, context: { params: Promise<{ path: string[] }> | { path: string[] } }) {
  const params = await context.params;
  const path = params.path.join("/");
  const draft = path === "draft/simulate";
  if (!routes.some(route => route.test(path)) || (request.method === "POST") !== draft) {
    return Response.json({ title: "Not found", status: 404 }, { status: 404 });
  }
  const incoming = new URL(request.url);
  const base = process.env.MANTA_API_BASE || "https://mantaml.com";
  const upstream = new URL(`/api/v1/${path}`, base.endsWith("/") ? base : `${base}/`);
  upstream.search = incoming.search;
  try {
    const response = await fetch(upstream, {
      method: request.method,
      headers: { Accept: "application/json", ...(draft ? { "Content-Type": "application/json" } : {}) },
      body: draft ? await request.text() : undefined,
      signal: request.signal,
    });
    const body = await response.arrayBuffer();
    return new Response(body, { status: response.status, headers: { "Content-Type": response.headers.get("Content-Type") || "application/json", "Cache-Control": response.headers.get("Cache-Control") || "no-store" } });
  } catch {
    return Response.json({ title: "Manta API unavailable", status: 503 }, { status: 503 });
  }
}

export const GET = forward;
export const POST = forward;
