/**
 * Sandbox guard (defense in depth, not a security boundary):
 *  - file tools (read/write/edit/ls/grep/find) may only touch paths inside PI_SANDBOX_ROOTS
 *    (folders separated by ":", default: the current directory)
 *  - write/edit may not touch files that make other programs run code later:
 *    .git/hooks, .git/config, .obsidian (Obsidian settings and plugins)
 *  - bash commands need approval: PI_BASH_APPROVAL = "risky" (default) | "all" | "off".
 *    "risky" parses the command (pipes, &&, $(…), sh -c, xargs, find -exec…) and asks only when a
 *    command of it goes to the network, deletes recursively, changes rights or the system, runs
 *    text as code, touches credentials, or writes outside the project folders. Reading never asks,
 *    whatever words its arguments contain.
 *  - bash commands get a timeout of PI_BASH_TIMEOUT seconds (default 600) unless the model sets one,
 *    so a command that hangs (a network call that never answers) can't block the agent for ever
 *  - without a UI (pi -p), anything that would need approval is blocked
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { realpathSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, relative, resolve } from "node:path";

const ROOTS = (process.env.PI_SANDBOX_ROOTS || process.cwd()).split(":").filter(Boolean)
  .map((p) => realpathSync(p));
const MODE = (process.env.PI_BASH_APPROVAL ?? "risky").toLowerCase();
const TIMEOUT = Number(process.env.PI_BASH_TIMEOUT ?? "600");
const PATH_TOOLS = new Set(["read", "write", "edit", "ls", "grep", "find"]);
const WRITE_TOOLS = new Set(["write", "edit"]);
const PROTECTED = /(^|\/)(\.git\/(hooks|config)|\.obsidian)(\/|$)/;

// ------------------------------------------------------------------ what makes a command risky

const NETWORK = new Set(["curl", "wget", "nc", "ncat", "netcat", "socat", "telnet", "ssh", "scp", "sftp",
  "rsync", "ftp", "lftp", "http", "https", "xh", "aria2c", "gh", "glab"]);
const SYSTEM = new Set(["sudo", "su", "doas", "chown", "chgrp", "chflags", "xattr", "setfacl", "osascript",
  "security", "launchctl", "crontab", "defaults", "pmset", "networksetup", "tccutil", "systemctl",
  "mount", "umount", "chroot", "mkfs", "shred"]);
const SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh", "fish"]);
const GIT_NETWORK = new Set(["push", "fetch", "pull", "clone", "ls-remote", "submodule", "send-email",
  "request-pull"]);
const PUBLISH = new Set(["publish", "login", "adduser", "token", "owner", "unpublish", "deprecate", "logout"]);
// Credentials: the Maven/npm/pip/Docker logins of the sandbox, Pi's sessions, the environment.
const SECRETS = /(^|[\/~])\.(ssh|netrc|git-credentials|npmrc|pypirc|docker\/config\.json)\b|\.m2\/(settings[^\/]*|settings-security)\.xml|\.config\/pi-sandbox|\.pi\/agent|\/proc\/[^\/]+\/environ/;
// Commands that write to their (non-option) arguments.
const WRITERS = new Set(["cp", "mv", "install", "ln", "tee", "touch", "mkdir", "rm", "rmdir", "truncate",
  "chmod", "unlink"]);
const WRAPPERS = new Set(["time", "nice", "nohup", "command", "builtin", "exec", "stdbuf", "ionice"]);
const KEYWORDS = new Set(["if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}"]);
const SAFE_TARGETS = /^\/(dev\/(null|stdout|stderr|fd\/\d+|tty)$|tmp(\/|$)|var\/tmp(\/|$))/;

interface Simple { words: string[]; writes: string[] }

/** Split a shell command into simple commands (also those inside $(…), `…`, <(…)): each with its
 *  words and the files it redirects output to. Returns null when it can't be parsed (unbalanced
 *  quotes or parentheses): such a command asks. */
export function parseCommand(src: string): Simple[] | null {
  const out: Simple[] = [];
  let words: string[] = [], writes: string[] = [], word = "", inWord = false;
  let redirect = false;          // the next word is a redirection target
  const heredocs: string[] = [];
  let i = 0;
  const endWord = () => {
    if (!inWord) return;
    if (redirect) { writes.push(word); redirect = false; } else words.push(word);
    word = ""; inWord = false;
  };
  const endCommand = () => {
    endWord();
    if (words.length || writes.length) out.push({ words, writes });
    words = []; writes = [];
  };
  // The text inside a balanced (…) whose "(" is at src[from], and the index of its ")".
  const balanced = (from: number): [string, number] | null => {
    let depth = 0;
    for (let j = from; j < src.length; j++) {
      const c = src[j];
      if (c === "\\") { j++; continue; }
      if (c === "'") { const k = src.indexOf("'", j + 1); if (k < 0) return null; j = k; continue; }
      if (c === "(") depth++;
      else if (c === ")" && --depth === 0) return [src.slice(from + 1, j), j];
    }
    return null;
  };
  const nested = (text: string): boolean => {
    const inner = parseCommand(text);
    if (!inner) return false;
    out.push(...inner);
    return true;
  };
  while (i < src.length) {
    const c = src[i];
    if (c === "\n" && heredocs.length) {                     // skip heredoc bodies
      endCommand();
      let j = i + 1;
      while (heredocs.length && j < src.length) {
        const nl = src.indexOf("\n", j);
        const line = src.slice(j, nl < 0 ? src.length : nl);
        if (line.trim() === heredocs[0]) heredocs.shift();
        j = nl < 0 ? src.length : nl + 1;
      }
      i = j; continue;
    }
    if (c === " " || c === "\t") { endWord(); i++; continue; }
    if (c === "\\") {
      if (src[i + 1] !== "\n") { word += src[i + 1] ?? ""; inWord = true; }
      i += 2; continue;
    }
    if (c === "#" && !inWord) { const nl = src.indexOf("\n", i); i = nl < 0 ? src.length : nl; continue; }
    if (c === "'") {
      const k = src.indexOf("'", i + 1);
      if (k < 0) return null;
      word += src.slice(i + 1, k); inWord = true; i = k + 1; continue;
    }
    if (c === '"') {
      let j = i + 1;
      for (; j < src.length && src[j] !== '"'; j++) {
        if (src[j] === "\\") { word += src[j + 1] ?? ""; j++; continue; }
        if (src[j] === "$" && src[j + 1] === "(" && src[j + 2] !== "(") {
          const b = balanced(j + 1); if (!b || !nested(b[0])) return null;
          word += "$(…)"; j = b[1]; continue;
        }
        if (src[j] === "`") {
          const k = src.indexOf("`", j + 1); if (k < 0 || !nested(src.slice(j + 1, k))) return null;
          word += "$(…)"; j = k; continue;
        }
        word += src[j];
      }
      if (j >= src.length) return null;
      inWord = true; i = j + 1; continue;
    }
    if (c === "$" && src[i + 1] === "(") {
      const b = balanced(i + 1); if (!b) return null;
      if (src[i + 2] !== "(" && !nested(b[0])) return null;   // $((…)) is arithmetic
      word += "$(…)"; inWord = true; i = b[1] + 1; continue;
    }
    if (c === "`") {
      const k = src.indexOf("`", i + 1); if (k < 0 || !nested(src.slice(i + 1, k))) return null;
      word += "$(…)"; inWord = true; i = k + 1; continue;
    }
    if ((c === "<" || c === ">") && src[i + 1] === "(") {          // process substitution
      const b = balanced(i + 1); if (!b || !nested(b[0])) return null;
      endWord(); words.push("/dev/fd/63"); i = b[1] + 1; continue;
    }
    if (c === ">" || c === "<" || (c === "&" && src[i + 1] === ">")) {
      if (inWord && !/^\d+$/.test(word)) endWord();
      word = ""; inWord = false;
      let op = c; i++;
      while (i < src.length && /[<>&|-]/.test(src[i]) && op.length < 3) op += src[i++];
      if (op.startsWith("<<") && !op.startsWith("<<<")) {          // heredoc: remember its end word
        while (src[i] === " ") i++;
        const m = /^(['"]?)([\w.-]+)\1/.exec(src.slice(i));
        if (m) { heredocs.push(m[2]); i += m[0].length; }
        continue;
      }
      if (op.startsWith("<")) { redirect = false; continue; }      // input: reading is fine
      while (src[i] === " ") i++;
      if (op.endsWith("&") && /^[\d-]/.test(src[i] ?? "")) {       // 2>&1, >&2
        while (/[\d-]/.test(src[i] ?? "")) i++;
        continue;
      }
      redirect = true; continue;
    }
    if (c === ";" || c === "&" || c === "|" || c === "\n" || c === "(" || c === ")") {
      endCommand(); i++; continue;
    }
    word += c; inWord = true; i++;
  }
  endCommand();
  return out;
}

const home = homedir();

function expand(path: string): string {
  return path.replace(/^(~|\$HOME|\$\{HOME\})(?=\/|$)/, home);
}

function insideRoots(path: string, cwd: string | null, roots: string[]): boolean {
  const p = expand(path);
  if (SAFE_TARGETS.test(p)) return true;
  if (p.includes("$")) return false;                       // depends on a variable: can't tell
  if (!isAbsolute(p) && cwd === null) return false;        // after a cd we couldn't follow
  const abs = resolve(cwd ?? "/", p);
  if (SAFE_TARGETS.test(abs)) return true;
  return roots.some((root) => inside(canonical(abs, "/"), root));
}

/** Strip assignments, keywords and wrappers (env, timeout, xargs, nice, jdk 17 …): [command, ...args]. */
function core(words: string[]): string[] {
  let w = [...words];
  for (;;) {
    while (w.length && (/^[A-Za-z_]\w*=/.test(w[0]) || KEYWORDS.has(w[0]))) w.shift();
    if (!w.length) return w;
    const name = basename(w[0]);
    if (WRAPPERS.has(name)) { w.shift(); while (w[0]?.startsWith("-")) w.shift(); continue; }
    if (name === "timeout") { w.shift(); while (w[0]?.startsWith("-")) w.shift(); w.shift(); continue; }
    if (name === "env") {
      w.shift();
      while (w.length && (w[0].startsWith("-") || /^[A-Za-z_]\w*=/.test(w[0]))) w.shift();
      if (!w.length) return ["env"];
      continue;
    }
    if (name === "jdk" && /^\d+$/.test(w[1] ?? "")) { w = w.slice(2); continue; }   // `jdk 17 mvn test`
    if (name === "xargs") {
      w.shift();
      while (w[0]?.startsWith("-")) { const o = w.shift()!; if (/^-[nIPdLsEa]$/.test(o)) w.shift(); }
      if (!w.length) return ["echo"];                                                // xargs alone: echo
      continue;
    }
    return w;
  }
}

function gitSub(args: string[]): { sub: string; rest: string[] } {
  let k = 0;
  while (k < args.length && args[k].startsWith("-")) {
    k += /^(-C|-c|--git-dir|--work-tree|--namespace)$/.test(args[k]) ? 2 : 1;   // options with a value
  }
  return { sub: args[k] ?? "", rest: args.slice(k + 1) };
}

/** Why `command` needs approval in "risky" mode; empty = it doesn't. */
export function riskReasons(command: string, roots: string[] = ROOTS, cwd: string = process.cwd()): string[] {
  const parsed = parseCommand(command);
  if (!parsed) return ["the command can't be parsed (unbalanced quotes or parentheses)"];
  const reasons = new Set<string>();
  let dir: string | null = cwd;
  const queue = [...parsed];
  const outside = (path: string) => !insideRoots(path, dir, roots);
  while (queue.length) {
    const { words, writes } = queue.shift()!;
    const all = [...words, ...writes];
    if (all.some((w) => SECRETS.test(expand(w)))) reasons.add("touches credentials or Pi's own files");
    if (all.some((w) => PROTECTED.test(w))) reasons.add("touches .git/hooks, .git/config or .obsidian");
    for (const target of writes) if (outside(target)) reasons.add(`writes outside the project folders (${target})`);
    const w = core(words);
    if (!w.length) continue;
    const name = basename(w[0]);
    const args = w.slice(1);
    const operands = args.filter((a) => !a.startsWith("-"));
    const sub = operands[0] ?? "";
    if (name === "cd" || name === "pushd") {
      const to = operands[0];
      dir = to === undefined ? home : to.includes("$") ? null : resolve(dir ?? "/", expand(to));
      continue;
    }
    if (NETWORK.has(name)) reasons.add(`${name}: network access (web_fetch reads the web with your approval)`);
    if (SYSTEM.has(name)) reasons.add(`${name}: changes rights or the system`);
    if (name === "eval") reasons.add("eval runs text as a command");
    if (SHELLS.has(name) || name === "source" || name === ".") {
      const c = args.indexOf("-c");
      if (c >= 0 && args[c + 1] !== undefined) {
        const inner = parseCommand(args[c + 1]);
        if (inner) queue.push(...inner); else reasons.add(`${name} -c with a command that can't be parsed`);
      } else if (!operands.length) {
        reasons.add(`${name} runs whatever comes from its input`);
      } else if (outside(operands[0])) {
        reasons.add(`${name} runs a script outside the project folders`);
      }
    }
    if (name === "printenv" || (name === "env" && !args.length) || (name === "set" && !args.length)
        || (name === "export" && (!args.length || args[0] === "-p"))) {
      reasons.add("prints the environment (it holds the Maven wrapper's password)");
    }
    if (name === "rm" && args.some((a) => /^-[a-zA-Z]*[rR]/.test(a) || a === "--recursive")) {
      reasons.add("rm -r: deletes recursively");
    }
    if (name === "chmod" && args.some((a) => /^[ugoa]*[+=][rwxX]*[st]|^[1-7][0-7]{3}$/.test(a))) {
      reasons.add("chmod with setuid/setgid/sticky bits");
    }
    if (name === "find") {
      if (args.includes("-delete")) reasons.add("find -delete: deletes files");
      if (args.some((a) => a.startsWith("-fprint") || a === "-fls")) reasons.add("find writes a file");
      args.forEach((a, at) => {
        if (!["-exec", "-execdir", "-ok", "-okdir"].includes(a)) return;
        const end = args.findIndex((x, k) => k > at && (x === ";" || x === "+"));
        queue.push({ words: args.slice(at + 1, end < 0 ? undefined : end).map((x) => x === "{}" ? "./found" : x), writes: [] });
      });
    }
    if (name === "git") {
      const g = gitSub(args);
      const has = (re: RegExp) => g.rest.some((a) => re.test(a));
      if (GIT_NETWORK.has(g.sub)) reasons.add(`git ${g.sub}: network access`);
      if (g.sub === "remote" && has(/^(add|set-url|rm|remove|rename|update|prune|show)$/)) {
        reasons.add("git remote: changes or contacts a remote");
      }
      if (g.sub === "config" && !has(/^(--get|--get-all|--get-regexp|--list|-l|--show-origin|--show-scope|get|list)$/)) {
        reasons.add("git config: changes git's configuration");
      }
      if (g.sub === "reset" && g.rest.includes("--hard")) reasons.add("git reset --hard: discards changes");
      if (g.sub === "clean") reasons.add("git clean: deletes untracked files");
      if (g.sub === "checkout" && has(/^(--|\.|-f|--force)$/)) reasons.add("git checkout of files: discards changes");
      if (g.sub === "restore" && !g.rest.includes("--staged")) reasons.add("git restore: discards changes");
      if (g.sub === "stash" && /^(drop|clear)$/.test(g.rest[0] ?? "")) reasons.add(`git stash ${g.rest[0]}`);
      if (g.sub === "branch" && (g.rest.includes("-D") || (has(/^(-d|--delete)$/) && has(/^(-f|--force)$/)))) {
        reasons.add("git branch -D: deletes a branch");
      }
      if (["filter-branch", "filter-repo", "update-ref", "prune"].includes(g.sub)
          || (g.sub === "reflog" && /^(expire|delete)$/.test(g.rest[0] ?? ""))) {
        reasons.add(`git ${g.sub}: rewrites or drops history`);
      }
    }
    if (["npm", "pnpm", "yarn", "bun", "twine", "cargo"].includes(name) && PUBLISH.has(sub)) {
      reasons.add(`${name} ${sub}: publishes or logs in`);
    }
    if (/^(mvn|mvnw|gradle|gradlew)$/.test(name) && args.some((a) => /^(deploy|publish\w*|release:\w+|[\w.-]+:deploy(-file)?)$/.test(a))) {
      reasons.add(`${name}: deploys or publishes`);
    }
    if (name === "docker" && /^(push|login)$/.test(sub)) reasons.add(`docker ${sub}`);
    if (name === "dd") {
      const of = args.find((a) => a.startsWith("of="));
      if (of && outside(of.slice(3))) reasons.add("dd writes outside the project folders");
    }
    if ((name === "sed" || name === "perl") && args.some((a) => /^-[a-zA-Z]*i/.test(a) || a.startsWith("--in-place"))) {
      for (const a of operands.slice(1)) if (outside(a)) reasons.add(`${name} -i outside the project folders (${a})`);
    }
    if (WRITERS.has(name)) {
      for (const t of name === "chmod" ? operands.slice(1) : operands) {
        if (outside(t)) reasons.add(`${name} outside the project folders (${t})`);
      }
    }
    if (name === "tar" && args.some((a) => /^-?[a-zA-Z]*x/.test(a))) {
      const at = args.findIndex((a) => a === "-C" || a === "--directory");
      if (at >= 0 && outside(args[at + 1] ?? "")) reasons.add("tar extracts outside the project folders");
    }
    if (name === "unzip") {
      const at = args.indexOf("-d");
      if (at >= 0 && outside(args[at + 1] ?? "")) reasons.add("unzip outside the project folders");
    }
  }
  return [...reasons];
}

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
    if (ctx.hasUI) ctx.ui.notify(`sandbox-guard: ${ROOTS.join(", ")}; bash approval: ${MODE}; bash timeout: ${TIMEOUT}s`, "info");
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

    if (event.toolName !== "bash") return undefined;
    // A command that hangs (a network call that never answers) must not block the agent for ever.
    if (TIMEOUT > 0 && (typeof input.timeout !== "number" || input.timeout <= 0)) input.timeout = TIMEOUT;
    if (MODE === "off") return undefined;
    const command = String(input.command ?? "");
    const reasons = MODE === "all" ? [] : riskReasons(command, ROOTS, ctx.cwd);
    if (MODE !== "all" && !reasons.length) return undefined;
    if (approved.has(command)) return undefined;
    if (!ctx.hasUI) {
      return { block: true, reason: `Command needs approval, but no UI is available${reasons.length ? `: ${reasons.join("; ")}` : ""}` };
    }
    const why = reasons.map((r) => `⚠️  ${r}`).join("\n");
    const choice = await ctx.ui.select(
      `${reasons.length ? "Risky" : "Run"} command in ${ctx.cwd}:\n\n${command}\n${why ? `\n${why}\n` : ""}`,
      ["Allow once", "Allow this exact command for the session", "Deny"],
    );
    if (choice === "Allow this exact command for the session") approved.add(command);
    if (choice?.startsWith("Allow")) return undefined;
    return { block: true, reason: "Denied by the user" };
  });
}
