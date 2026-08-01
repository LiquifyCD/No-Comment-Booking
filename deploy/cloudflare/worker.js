const DEFAULT_ORIGIN = "https://frostbyte.158-179-207-206.sslip.io";

export default {
  async fetch(request, env) {
    const incoming = new URL(request.url);
    if (incoming.protocol !== "https:") {
      incoming.protocol = "https:";
      return Response.redirect(incoming, 308);
    }

    const origin = new URL(env.ORIGIN_URL || DEFAULT_ORIGIN);
    origin.pathname = incoming.pathname;
    origin.search = incoming.search;

    const headers = new Headers(request.headers);
    headers.delete("host");

    const init = {
      method: request.method,
      headers,
      redirect: "manual",
    };
    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = request.body;
    }

    const upstream = await fetch(origin, init);
    const responseHeaders = new Headers(upstream.headers);
    responseHeaders.set("Cache-Control", "no-store");

    const location = responseHeaders.get("Location");
    if (location?.startsWith(origin.origin)) {
      responseHeaders.set("Location", location.replace(origin.origin, incoming.origin));
    }

    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  },
};
