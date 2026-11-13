/**
 * Read-only web tools. Every request is a GET made by curl (through PI_WEB_PROXY or the
 * https_proxy environment), shown to the user first: Allow once / Allow host for the session / Deny.
 * `pibox run --allow-web` sets PI_WEB_TOKEN: no question here, and the proxy doesn't ask either.
 * Search backends: SearXNG (WEB_SEARCH_SEARXNG=http://searxng:8080) or DuckDuckGo's HTML endpoint.
 * PI_WEB_PLAIN_HTTP=1: send https:// URLs to the proxy as plain http:// requests (no tunnel), so a
 * filtering proxy can see the method, marked with X-Pibox-Https: 1 so that the proxy fetches them
 * over HTTPS. Redirects are followed here, each as such a request.
 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const PROXY = process.env.PI_WEB_PROXY ?? process.env.https_proxy ?? process.env.HTTPS_PROXY ?? "";
const SEARXNG = process.env.WEB_SEARCH_SEARXNG ?? "";
const PLAIN_HTTP = process.env.PI_WEB_PLAIN_HTTP === "1";
const MAX_CHARS = Number(process.env.WEB_FETCH_MAX_CHARS ?? 20000);
// pibox run --allow-web: no approval here, and the proxy lets requests with this token pass too
const WEB_TOKEN = process.env.PI_WEB_TOKEN ?? "";
const PRIVATE_HOST = /^(localhost|.*\.local|.*\.internal|bridge|proxy|metadata(\.google\.internal)?)$|^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|0\.|\[?::1\]?$|\[?f[cd])/i;

export default function (pi: ExtensionAPI) {
  const allowedHosts = new Set<string>();

  function check(url: URL): void {
    if (!/^https?:$/.test(url.protocol)) throw new Error(`Only http(s) URLs are allowed, got ${url.protocol}`);
    if (PRIVATE_HOST.test(url.hostname)) throw new Error(`Refusing private/internal host ${url.hostname}`);
  }

  async function approve(url: URL, why: string, ctx: ExtensionContext): Promise<void> {
    check(url);
    if (WEB_TOKEN || allowedHosts.has(url.hostname)) return;
    if (!ctx.hasUI) throw new Error("Web access needs approval, but no UI is available");
    const choice = await ctx.ui.select(`🌐 ${why}\n\nGET ${url.href}\n`,
      ["Allow once", `Allow ${url.hostname} for this session`, "Deny"]);
    if (choice?.startsWith(`Allow ${url.hostname}`)) allowedHosts.add(url.hostname);
    else if (choice !== "Allow once") throw new Error("Request denied by the user");
  }

  type Reply = { status: number; type: string; location: string; body: string };

  // One request, no redirects. PLAIN_HTTP: an https:// page goes to the proxy as "GET http://…"
  // with X-Pibox-Https: 1, and the proxy fetches it over HTTPS (egress_policy.py).
  async function getOnce(url: URL, signal?: AbortSignal): Promise<Reply> {
    const marker = "\n__PI_WEB__";
    const args = ["--silent", "--show-error", "--max-time", "30", "--max-filesize", "5000000",
      "--proto", "=http,https", "--user-agent", "pi-web/1.0",
      "--write-out", `${marker}%{http_code}|%header{location}|%{content_type}`];
    if (PROXY) args.push("--proxy", PROXY);
    const plain = PLAIN_HTTP && url.protocol === "https:";
    if (plain) args.push("--header", "X-Pibox-Https: 1");
    if (WEB_TOKEN) args.push("--header", `X-Pibox-Web-Token: ${WEB_TOKEN}`);
    args.push("--", plain ? "http:" + url.href.slice(6) : url.href);
    const r = await pi.exec("curl", args, { signal, timeout: 35000, maxBuffer: 8 * 1024 * 1024 });
    const at = r.stdout.lastIndexOf(marker);
    if (at < 0) throw new Error(`curl failed: ${r.stderr.trim() || `exit code ${r.code}`}`);
    const meta = r.stdout.slice(at + marker.length).trim();
    const a = meta.indexOf("|"), b = meta.lastIndexOf("|");
    const status = Number(meta.slice(0, a));
    // 000: no answer at all (the proxy refused the connection, a timeout): say why, not "empty page"
    if (!status) throw new Error(`curl failed: ${r.stderr.trim() || `exit code ${r.code}`} (${url.href})`);
    return { status, location: meta.slice(a + 1, b), type: meta.slice(b + 1), body: r.stdout.slice(0, at) };
  }

  // Redirects are followed here, not by curl: curl would follow an https:// Location through a
  // tunnel, which the proxy refuses. Another host needs approval as the first one did.
  async function get(url: URL, ctx: ExtensionContext, signal?: AbortSignal): Promise<Reply & { url: URL }> {
    for (let hops = 0; ; hops++) {
      const r = await getOnce(url, signal);
      if (r.status < 300 || r.status >= 400 || !r.location) return { ...r, url };
      if (hops >= 5) throw new Error(`too many redirects, the last to ${r.location}`);
      const next = new URL(r.location, url);
      if (next.hostname === url.hostname) check(next);
      else await approve(next, `The page redirects from ${url.href}`, ctx);
      url = next;
    }
  }

  // A search engine's answer other than 200 (the proxy's refusal, a rate limit, a captcha page) is
  // an error with its text, not "No results."
  function answered(r: Reply): Reply {
    if (r.status !== 200) throw new Error(`search failed: HTTP ${r.status}: ${toText(r.body).slice(0, 300)}`);
    return r;
  }

  function toText(html: string): string {
    return html
      .replace(/<(script|style|noscript|svg)[\s\S]*?<\/\1>/gi, " ")
      .replace(/<br\s*\/?>|<\/(p|div|li|h[1-6]|tr)>/gi, "\n")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
      .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
      .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
      .replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n\n").trim();
  }

  pi.registerTool({
    name: "web_fetch",
    label: "Web fetch",
    description: "Fetch a public web page or file with an HTTP GET (read-only; the user approves each request). Returns text.",
    promptSnippet: "Read a public web page (GET only, user-approved)",
    parameters: Type.Object({ url: Type.String({ description: "Absolute http(s) URL" }) }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const url = new URL(params.url);
      await approve(url, "The agent wants to read a web page", ctx);
      const r = await get(url, ctx, signal);
      let text = /html/i.test(r.type) ? toText(r.body) : r.body;
      if (text.length > MAX_CHARS) text = text.slice(0, MAX_CHARS) + `\n… [truncated, ${text.length} chars]`;
      const from = r.url.href !== url.href ? ` (redirected to ${r.url.href})` : "";
      return { content: [{ type: "text", text: `HTTP ${r.status} ${r.type}${from}\n\n${text || "(empty body)"}` }],
               details: { url: r.url.href, status: r.status } };
    },
  });

  pi.registerTool({
    name: "web_search",
    label: "Web search",
    description: "Search the web (GET only; the user approves the query). Returns titles, URLs and snippets.",
    promptSnippet: "Search the web (user-approved)",
    parameters: Type.Object({ query: Type.String({ description: "Search query" }) }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const results: { title: string; url: string; snippet: string }[] = [];
      if (SEARXNG) {
        const url = new URL("/search", SEARXNG);
        url.search = new URLSearchParams({ q: params.query, format: "json" }).toString();
        await approve(url, `Search: ${params.query}`, ctx);
        const r = answered(await get(url, ctx, signal));
        for (const item of (JSON.parse(r.body).results ?? []).slice(0, 10)) {
          results.push({ title: item.title, url: item.url, snippet: item.content ?? "" });
        }
      } else {
        const url = new URL("https://html.duckduckgo.com/html/");
        url.search = new URLSearchParams({ q: params.query }).toString();
        await approve(url, `Search: ${params.query}`, ctx);
        const r = answered(await get(url, ctx, signal));
        const re = /class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>[\s\S]*?class="result__snippet"[^>]*>([\s\S]*?)<\/a>/g;
        for (const m of r.body.matchAll(re)) {
          let href = m[1].replace(/&amp;/g, "&");
          const uddg = /[?&]uddg=([^&]+)/.exec(href);
          if (uddg) href = decodeURIComponent(uddg[1]);
          results.push({ title: toText(m[2]), url: href, snippet: toText(m[3]) });
          if (results.length >= 10) break;
        }
      }
      const text = results.map((r, i) => `${i + 1}. ${r.title}\n   ${r.url}\n   ${r.snippet}`).join("\n")
        || "No results.";
      return { content: [{ type: "text", text }], details: { count: results.length } };
    },
  });
}
