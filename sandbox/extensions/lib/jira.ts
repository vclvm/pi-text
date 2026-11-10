/**
 * Jira issues as text for the model (jira.ts wires this into Pi; this folder has no index.ts, so Pi
 * doesn't load it as an extension). REST API v2: Jira Server, Data Center and Cloud all have it,
 * and its description is plain wiki text.
 */

export const KEY = /^[A-Z][A-Z0-9_]*-\d+$/;
export const FIELDS = ["summary", "status", "issuetype", "priority", "assignee", "reporter", "labels", "components",
  "fixVersions", "created", "updated", "description", "parent", "subtasks", "issuelinks", "comment", "attachment"];
const DESCRIPTION_CHARS = 12000;
const LINKED_DESCRIPTION_CHARS = 3000;
const COMMENTS = 10;
const COMMENT_CHARS = 2000;
export const MAX_LINKED = 15;

/** The address the agent asks for: always plain http:// to the filtering proxy, which goes on with
 *  HTTPS when jira_url says so, and adds the token. Keeps a context path (https://corp/jira). */
export function apiUrl(jiraUrl: string, path: string): string {
  const base = new URL(jiraUrl);
  return `http://${base.host}${base.pathname.replace(/\/+$/, "")}${path}`;
}

const cut = (s: string, n: number) => (s.length > n ? `${s.slice(0, n)}\n… [cut, ${s.length} chars]` : s);
const name = (x: any) => x?.displayName ?? x?.name ?? "";
const names = (xs: any) => (Array.isArray(xs) ? xs.map((x) => x?.name ?? x).filter(Boolean).join(", ") : "");

/** Keys of every issue this one points to: links, subtasks, parent. */
export function related(issue: any): string[] {
  const f = issue?.fields ?? {};
  const keys = [
    f.parent?.key,
    ...(f.subtasks ?? []).map((s: any) => s?.key),
    ...(f.issuelinks ?? []).map((l: any) => (l?.outwardIssue ?? l?.inwardIssue)?.key),
  ].filter((k): k is string => typeof k === "string" && KEY.test(k));
  return [...new Set(keys)];
}

const line = (i: any) => `${i.key}: ${i.fields?.summary ?? ""}${i.fields?.status?.name ? ` (${i.fields.status.name})` : ""}`;

/** One issue as text. `full` = the main issue (comments too); otherwise a shorter linked one. */
export function formatIssue(issue: any, jiraUrl: string, full = true): string {
  const f = issue?.fields ?? {};
  const out: string[] = [`# ${issue.key}: ${f.summary ?? ""}`];
  const meta = [
    ["Type", f.issuetype?.name], ["Status", f.status?.name], ["Priority", f.priority?.name],
    ["Assignee", name(f.assignee)], ["Reporter", name(f.reporter)], ["Labels", (f.labels ?? []).join(", ")],
    ["Components", names(f.components)], ["Fix versions", names(f.fixVersions)],
    ["Created", f.created?.slice(0, 10)], ["Updated", f.updated?.slice(0, 10)],
  ].filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`);
  out.push(meta.join(" | "), `URL: ${new URL(jiraUrl).origin}${new URL(jiraUrl).pathname.replace(/\/+$/, "")}/browse/${issue.key}`);
  out.push("", "## Description", cut(String(f.description ?? "").trim() || "(none)", full ? DESCRIPTION_CHARS : LINKED_DESCRIPTION_CHARS));
  if (f.parent?.key) out.push("", "## Parent", line(f.parent));
  if (f.subtasks?.length) out.push("", "## Subtasks", ...f.subtasks.map(line));
  if (f.issuelinks?.length) {
    out.push("", "## Links");
    for (const l of f.issuelinks) {
      const other = l.outwardIssue ?? l.inwardIssue;
      if (other) out.push(`${l.outwardIssue ? l.type?.outward : l.type?.inward} ${line(other)}`);
    }
  }
  if (f.attachment?.length) out.push("", "## Attachments", f.attachment.map((a: any) => a.filename).join(", "));
  const comments = f.comment?.comments ?? [];
  if (full && comments.length) {
    out.push("", `## Comments (${comments.length > COMMENTS ? `the last ${COMMENTS} of ${comments.length}` : comments.length})`);
    for (const c of comments.slice(-COMMENTS)) {
      out.push(`- ${name(c.author)}, ${String(c.created ?? "").slice(0, 10)}: ${cut(String(c.body ?? "").trim(), COMMENT_CHARS)}`);
    }
  }
  return out.join("\n");
}

/** Why a request failed, in words the model can act on. */
export function failure(status: number, body: string, key: string): string {
  if (/egress policy:/.test(body)) return `the sandbox's proxy refused it: ${body.trim()}`;
  if (status === 401) return "Jira didn't accept the token: check extensions.jira_token (then pibox setup, pibox up)";
  if (status === 403) return `the token may not read ${key}`;
  if (status === 404) return `${key} doesn't exist, or the token may not see it`;
  return `HTTP ${status}: ${body.trim().slice(0, 300)}`;
}
