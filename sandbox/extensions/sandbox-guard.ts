/**
 * Sandbox guard (defense in depth, not a security boundary):
 *  - file tools (read/write/edit/ls/grep/find) may only touch paths inside PI_SANDBOX_ROOTS
 *    (folders separated by ":", default: the current directory)
 *  - write/edit may not touch files that make other programs run code later:
 *    .git/hooks, .git/config, .obsidian (Obsidian settings and plugins)
 *  - bash commands need approval: PI_BASH_APPROVAL = "risky" (default) | "all" | "off".
 *    "risky" parses the command (pipes, &&, $(…), sh -c, xargs, find -exec…) and asks only when a
 *    command of it sends something to the network, deletes what git can't bring back, changes
 *    rights or the system, runs text as code, touches credentials, or writes outside the project
 *    folders. Reading never asks, whatever words its arguments contain. What git undoes doesn't ask
 *    either: fetching, pulling, deleting files that have no uncommitted work, and anything in the
 *    project's own notes folder.
 *  - bash commands get a timeout of PI_BASH_TIMEOUT seconds (default 600) unless the model sets one,
 *    so a command that hangs (a network call that never answers) can't block the agent for ever
 *  - a new Markdown file in a project (not in the notes folder PI_NOTES_DIR) needs approval: notes
 *    and reports belong in the notes folder, outside the repository, or they end up in git
 *  - the files of the notes folder that go into the system prompt (rules, task, memory) are not
 *    written by the agent: write/edit are refused, a bash command writing to them asks. Otherwise
 *    text the agent picked up somewhere could instruct every later session.
 *  - without a UI (pi -p), anything that would need approval is blocked
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFileSync } from "node:child_process";
import { existsSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, relative, resolve } from "node:path";

const ROOTS = (process.env.PI_SANDBOX_ROOTS || process.cwd()).split(":").filter(Boolean)
  .map((p) => realpathSync(p));
const MODE = (process.env.PI_BASH_APPROVAL ?? "risky").toLowerCase();
const TIMEOUT = Number(process.env.PI_BASH_TIMEOUT ?? "600");
const PATH_TOOLS = new Set(["read", "write", "edit", "ls", "grep", "find"]);
const WRITE_TOOLS = new Set(["write", "edit"]);
const NOTES_DIR = process.env.PI_NOTES_DIR && existsSync(process.env.PI_NOTES_DIR)
  ? realpathSync(process.env.PI_NOTES_DIR) : "";
const PROTECTED = /(^|\/)(\.git\/(hooks|config)|\.obsidian)(\/|$)/;
const PROMPT_NOTES = /(^|\/)(AGENTS\.md|current-task\.md|task-progress\.md|memory\.md|memory(\/[^/]+)?|\.memory(\/.*)?)$/;

// ------------------------------------------------------------------ what makes a command risky

const NETWORK = new Set(["curl", "wget", "nc", "ncat", "netcat", "socat", "telnet", "ssh", "scp", "sftp",
  "rsync", "ftp", "lftp", "http", "https", "xh", "aria2c", "gh", "glab"]);
const SYSTEM = new Set(["sudo", "su", "doas", "chown", "chgrp", "chflags", "xattr", "setfacl", "osascript",
  "security", "launchctl", "crontab", "defaults", "pmset", "networksetup", "tccutil", "systemctl",
  "mount", "umount", "chroot", "mkfs", "shred"]);
const SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh", "fish"]);
// Getting commits is left out: fetch and pull change nothing git can't undo. clone and submodule
// bring in code from wherever they are told.
const GIT_NETWORK = new Set(["push", "clone", "submodule", "send-email", "request-pull"]);
const PUBLISH = new Set(["publish", "login", "adduser", "token", "owner", "unpublish", "deprecate", "logout"]);
// Credentials: the Maven/npm/pip/Docker logins of the sandbox, Pi's sessions, the environment.
const SECRETS = /(^|[\/~])\.(ssh|netrc|git-credentials|npmrc|pypirc|docker\/config\.json)\b|\.m2\/(settings[^\/]*|settings-security)\.xml|\.config\/pi-sandbox|\.pi\/agent|\/proc\/[^\/]+\/environ/;
// Commands that write to their (non-option) arguments.
const WRITERS = new Set(["cp", "mv", "install", "ln", "tee", "touch", "mkdir", "rm", "rmdir", "truncate",
  "chmod", "unlink"]);
const WRAPPERS = new Set(["time", "nice", "nohup", "command", "builtin", "exec", "stdbuf", "ionice"]);
const KEYWORDS = new Set(["if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}"]);
const SAFE_TARGETS = /^\/(dev\/(null|stdout|stderr|fd\/\d+|tty)$|tmp(\/|$)|var\/tmp(\/|$))/;

const FOUND = "./found";            // what find -exec gets for {}: unknown until it runs
const DATE = "date-stamp";          // $(date …) in a word: only digits and separators come out of it

interface Simple { words: string[]; writes: string[] }

const substitution = (inner: string) => (/^\s*date(\s[^|;&<>`$()]*)?$/.test(inner) ? DATE : "$(…)");

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
          word += substitution(b[0]); j = b[1]; continue;
        }
        if (src[j] === "`") {
          const k = src.indexOf("`", j + 1); if (k < 0 || !nested(src.slice(j + 1, k))) return null;
          word += substitution(src.slice(j + 1, k)); j = k; continue;
        }
        word += src[j];
      }
      if (j >= src.length) return null;
      inWord = true; i = j + 1; continue;
    }
    if (c === "$" && src[i + 1] === "(") {
      const b = balanced(i + 1); if (!b) return null;
      if (src[i + 2] !== "(" && !nested(b[0])) return null;   // $((…)) is arithmetic
      word += substitution(b[0]); inWord = true; i = b[1] + 1; continue;
    }
    if (c === "`") {
      const k = src.indexOf("`", i + 1); if (k < 0 || !nested(src.slice(i + 1, k))) return null;
      word += substitution(src.slice(i + 1, k)); inWord = true; i = k + 1; continue;
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

/** A path with the variables we know replaced; those we don't stay, as $NAME. */
function substitute(path: string, vars: Map<string, string>): string {
  return expand(path).replace(/\$(\w+)|\$\{(\w+)\}/g, (raw, a, b) => vars.get(a ?? b) ?? raw);
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

/** Can a recursive delete of `target` be undone? Yes in the project's own notes folder, which is
 *  the agent's to keep, and in a git work tree when nothing under it is uncommitted: git has all of
 *  it, and what it ignores (target/, node_modules/) gets built again. Never the whole work tree or
 *  notes folder, whatever git says: that is not a cleanup. */
function undoable(target: string, cwd: string | null, roots: string[]): boolean {
  if (target === FOUND || target.includes("$") || (cwd === null && !isAbsolute(target))) return false;
  if (!insideRoots(target, cwd, roots)) return false;
  const glob = target.search(/[*?[]/);
  const base = glob < 0 ? target : target.slice(0, target.lastIndexOf("/", glob) + 1) || ".";
  const abs = canonical(resolve(cwd ?? "/", base), "/");
  const notes = process.env.PI_PROJECT_NOTES;
  if (notes) {
    const folder = canonical(notes, "/");
    if (abs !== folder && inside(abs, folder)) return true;
  }
  if (/(^|\/)\.git(\/|$)/.test(abs) || /(^|\/)\.[*?[]/.test(target)) return false;
  let probe = abs;
  while (!(existsSync(probe) && statSync(probe).isDirectory()) && dirname(probe) !== probe) probe = dirname(probe);
  try {
    const git = (from: string, ...args: string[]) => execFileSync("git",
      ["--no-optional-locks", "-c", "core.fsmonitor=false", "-C", from, ...args],
      { encoding: "utf8", timeout: 5000, stdio: ["ignore", "pipe", "ignore"] }).trim();
    const top = canonical(git(probe, "rev-parse", "--show-toplevel"), "/");
    if (abs === top || !inside(abs, top)) return false;
    const spec = relative(top, canonical(resolve(cwd ?? "/", glob < 0 ? target : base), "/")) +
      (glob < 0 ? "" : `/${target.slice(base === "." ? 0 : base.length)}`);
    return git(top, "status", "--porcelain", "--untracked-files=all", "--", spec) === "";
  } catch {
    return false;                                  // no git, or it can't tell
  }
}

/** Does this path in the notes folder end up in the system prompt, or decide what does? */
export function promptNote(target: string, notes: string = NOTES_DIR): boolean {
  return !!notes && inside(target, notes) && PROMPT_NOTES.test(relative(notes, target));
}

/** Why `command` needs approval in "risky" mode; empty = it doesn't. */
export function riskReasons(command: string, roots: string[] = ROOTS, cwd: string = process.cwd(),
                            notes: string = NOTES_DIR): string[] {
  const parsed = parseCommand(command);
  if (!parsed) return ["the command can't be parsed (unbalanced quotes or parentheses)"];
  const reasons = new Set<string>();
  let dir: string | null = cwd;
  const queue = [...parsed];
  const vars = new Map<string, string>([["HOME", home]]);
  for (const name of ["PI_PROJECT_NOTES", "PI_NOTES_DIR"]) {
    const value = process.env[name];
    if (value) vars.set(name, value);
  }
  if (dir) vars.set("PWD", dir);
  const known = (path: string) => substitute(path, vars);
  const outside = (path: string) => {
    const p = known(path);
    if (notes && !p.includes("$") && promptNote(canonical(resolve(dir ?? "/", p), "/"), notes)) {
      reasons.add(`writes ${path}, which goes into the system prompt of later sessions`);
    }
    return !insideRoots(p, dir, roots);
  };
  while (queue.length) {
    const { words, writes } = queue.shift()!;
    const all = [...words, ...writes];
    if (all.some((w) => SECRETS.test(expand(w)))) reasons.add("touches credentials or Pi's own files");
    if (all.some((w) => PROTECTED.test(w))) reasons.add("touches .git/hooks, .git/config or .obsidian");
    for (const target of writes) if (outside(target)) reasons.add(`writes outside the project folders (${target})`);
    const w = core(words);
    if (!w.length) {                                       // F=/some/path, for the commands after it
      for (const word of words) {
        const m = /^([A-Za-z_]\w*)=(.*)$/s.exec(word);
        if (m) vars.set(m[1], known(m[2]));
      }
      continue;
    }
    const name = basename(w[0]);
    const args = w.slice(1);
    const operands = args.filter((a) => !a.startsWith("-"));
    const sub = operands[0] ?? "";
    if (name === "cd" || name === "pushd") {
      const to = operands[0];
      const target = to === undefined ? home : known(to);
      dir = target.includes("$") ? null : resolve(dir ?? "/", target);
      if (dir) vars.set("PWD", dir); else vars.delete("PWD");
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
    const recoverable = (paths: string[]) => paths.length > 0 && paths.every((p) => undoable(known(p), dir, roots));
    if (name === "rm" && args.some((a) => /^-[a-zA-Z]*[rR]/.test(a) || a === "--recursive") && !recoverable(operands)) {
      reasons.add("rm -r: deletes recursively, and git can't bring all of it back");
    }
    if (name === "chmod" && args.some((a) => /^[ugoa]*[+=][rwxX]*[st]|^[1-7][0-7]{3}$/.test(a))) {
      reasons.add("chmod with setuid/setgid/sticky bits");
    }
    if (name === "find") {
      const start = args.slice(0, Math.max(0, args.findIndex((a) => /^[-(!]/.test(a))));
      if (args.includes("-delete") && !recoverable(start)) reasons.add("find -delete: deletes files git can't bring back");
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
      if (g.sub === "remote" && has(/^(add|set-url|rm|remove|rename)$/)) {
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
      if (WRITE_TOOLS.has(event.toolName) && promptNote(target)) {
        return { block: true, reason: `"${path}" goes into the system prompt: only the user and the automatic memory update change it. Keep your own notes in notes/<task>.md` };
      }
      if (NOTES_DIR && event.toolName === "write" && /\.md$/i.test(target) && !inside(target, NOTES_DIR)
          && !existsSync(target)) {
        const notes = process.env.PI_PROJECT_NOTES || NOTES_DIR;
        const reason = `Notes, plans and reports go into the project's notes folder ${notes}, not into the repository`;
        if (!ctx.hasUI) return { block: true, reason };
        const choice = await ctx.ui.select(
          `New Markdown file in the repository:\n\n${target}\n\nNotes and reports belong in ${notes} (outside git).`,
          ["Allow (it is a deliverable, e.g. docs I asked for)", "Deny"]);
        if (!choice?.startsWith("Allow")) return { block: true, reason: `Denied by the user. ${reason}` };
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
