/**
 * Sandbox guard (defense in depth, not a security boundary):
 *  - file tools (read/write/edit/ls/grep/find) may only touch paths inside PI_SANDBOX_ROOTS
 *    (folders separated by ":", default: the current directory)
 *  - write/edit may not touch files that make other programs run code later:
 *    .git/hooks, .git/config, .obsidian (Obsidian settings and plugins)
 *  - bash commands need approval: PI_BASH_APPROVAL = "risky" (default) | "all" | "off"
 *  - without a UI (pi -p), anything that would need approval is blocked
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { realpathSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";

const ROOTS = (process.env.PI_SANDBOX_ROOTS || process.cwd()).split(":").filter(Boolean)
  .map((p) => realpathSync(p));
const MODE = (process.env.PI_BASH_APPROVAL ?? "risky").toLowerCase();
const PATH_TOOLS = new Set(["read", "write", "edit", "ls", "grep", "find"]);
const WRITE_TOOLS = new Set(["write", "edit"]);
const PROTECTED = /(^|\/)(\.git\/(hooks|config)|\.obsidian)(\/|$)/;

const RISKY = [
  /\b(curl|wget|nc|ncat|socat|telnet|ssh|scp|sftp|rsync|ftp)\b/,  // network
  /\bgit\s+(push|remote|config\s+--global|credential)/,
  /\b(npm|pnpm|yarn)\s+(publish|login|adduser|token)/,
  /\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|--recursive)/i,
  /\b(sudo|su|doas|chmod|chown|chflags|xattr)\b/,
  /\b(osascript|security|launchctl|crontab|defaults|pmset|networksetup|tccutil)\b/,
  /\b(base64|xxd)\b.*\|\s*(ba|z)?sh\b/, /\|\s*(ba|z)?sh\b/, /\beval\b/,
  /(^|[\s"'=])(~|\$HOME|\/etc\/|\/private\/)/,                      // home and system paths
  /\.git\/(hooks|config)|\.obsidian\//,                               // see PROTECTED
];

/** Resolve symlinks of the longest existing prefix, so "./link/../../x" can't escape. */
function canonical(path: string, cwd: string): string {
  const abs = resolve(cwd, path);
  let probe = abs;
  for (;;) {
    try {
      return resolve(realpathSync(probe), relative(probe, abs));
    } catch {
      const parent = dirname(probe);
      if (parent === probe) return abs;
      probe = parent;
    }
  }
}

function inside(target: string, root: string): boolean {
  const rel = relative(root, target);
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

export default function (pi: ExtensionAPI) {
  const approved = new Set<string>();

  pi.on("session_start", async (_event, ctx) => {
    if (ctx.hasUI) ctx.ui.notify(`sandbox-guard: ${ROOTS.join(", ")}; bash approval: ${MODE}`, "info");
  });

  pi.on("tool_call", async (event, ctx) => {
    const input = event.input as Record<string, unknown>;

    if (PATH_TOOLS.has(event.toolName)) {
      const path = typeof input.path === "string" && input.path ? input.path : ".";
      const target = canonical(path, ctx.cwd);
      if (!ROOTS.some((root) => inside(target, root))) {
        return { block: true, reason: `Path "${path}" is outside ${ROOTS.join(", ")}` };
      }
      if (WRITE_TOOLS.has(event.toolName) && PROTECTED.test(target)) {
        return { block: true, reason: `"${path}" is protected: hooks, git config and .obsidian are edited by the user only` };
      }
      return undefined;
    }

    if (event.toolName !== "bash" || MODE === "off") return undefined;
    const command = String(input.command ?? "");
    const risky = RISKY.some((re) => re.test(command));
    if (MODE !== "all" && !risky) return undefined;
    if (approved.has(command)) return undefined;
    if (!ctx.hasUI) return { block: true, reason: "Command needs approval, but no UI is available" };

    const choice = await ctx.ui.select(
      `${risky ? "⚠️  Risky" : "Run"} command in ${ctx.cwd}:\n\n${command}\n`,
      ["Allow once", "Allow this exact command for the session", "Deny"],
    );
    if (choice === "Allow this exact command for the session") approved.add(command);
    if (choice?.startsWith("Allow")) return undefined;
    return { block: true, reason: "Denied by the user" };
  });
}
