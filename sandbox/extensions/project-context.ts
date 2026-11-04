/**
 * Project context from outside the repository (so nothing of it ends up in git):
 *  - PI_PROJECT_NOTES (set by `pibox run`) is the project's notes folder:
 *    <folders.notes>/<the project's path under ~>
 *  - before every run its AGENTS.md (the project's rules) and current-task.md (the task) go into
 *    the system prompt as the section <project_notes>, read anew each time, so an edited task
 *    counts from the next message on. Pi patches only a section that changed.
 *  - HTML comments (<!-- … -->) are left out: the templates keep their hints in them, and a file
 *    with nothing but headings and empty bullets counts as not written yet.
 * `pibox task new` creates both files from the templates; `pibox task edit [rules]` opens them.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

const NOTES = process.env.PI_PROJECT_NOTES || "";
const MAX_CHARS = 20000;   // per file: the section goes with every request

/** The file's text without HTML comments, or "" if it has nothing but headings and empty bullets. */
export function meaningful(text: string): string {
  const clean = text.replace(/<!--[\s\S]*?-->/g, "").replace(/\n{3,}/g, "\n\n").trim();
  const filled = clean.split("\n").some((line) => {
    const l = line.trim();
    return l && !l.startsWith("#") && !/^[-*]\s*([\w ]+:)?\s*$/.test(l);
  });
  return filled ? clean : "";
}

function load(name: string): string {
  const path = join(NOTES, name);
  if (!existsSync(path)) return "";
  try {
    const text = meaningful(readFileSync(path, "utf8"));
    return text.length > MAX_CHARS ? `${text.slice(0, MAX_CHARS)}\n… [cut: read ${path} for the rest]` : text;
  } catch {
    return "";
  }
}

export function section(notes: string, rules: string, task: string): string {
  const parts = [
    `The notes folder of this project: ${notes}\n` +
    "Notes, plans and reports go there (designs/, implemented/, reviews/, tests/, investigations/, notes/), never into the repository.",
  ];
  if (rules) parts.push(`## Project rules (${join(notes, "AGENTS.md")})\n\n${rules}`);
  if (task) parts.push(`## Current task (${join(notes, "current-task.md")})\n\n${task}`);
  else parts.push(`No current task is written down (${join(notes, "current-task.md")}): the user's messages are the task.`);
  return parts.join("\n\n");
}

export default function (pi: ExtensionAPI) {
  if (!NOTES) return;

  pi.on("session_start", async (_event, ctx) => {
    if (!ctx.hasUI) return;
    const task = load("current-task.md"), rules = load("AGENTS.md");
    ctx.ui.notify(`project notes: ${NOTES}; ${task ? "current task loaded" : "no current task (pibox task new)"}` +
      `${rules ? "; project rules loaded" : ""}`, "info");
  });

  pi.on("before_agent_start", async (event) => {
    event.systemPromptOptions.sections.project_notes = section(NOTES, load("AGENTS.md"), load("current-task.md"));
  });
}
