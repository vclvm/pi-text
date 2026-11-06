/**
 * What the user sent to Pi, kept like tasks are, in the project's notes folder (project-context.ts
 * wires this in; this folder has no index.ts, so Pi doesn't load it as an extension):
 *  - current-prompt.txt   the prompt being worked on, written as soon as it is submitted, together
 *                         with what the user sent while it ran
 *  - history/<ts>_prompt.txt             it moves here when the run is over, <ts> = when submitted
 *  - history/<ts>_prompt_unfinished.txt  the same for a run stopped, failed or killed
 * A file found at the next prompt belongs to a process that died, so it is filed as unfinished.
 * Another Pi running in the same project looks the same: its prompt is then filed twice, never lost.
 */
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

export const promptPath = (notes: string) => join(notes, "current-prompt.txt");

export const stamp = (at: Date) => {
  const n = (x: number) => String(x).padStart(2, "0");
  return `${at.getFullYear()}-${n(at.getMonth() + 1)}-${n(at.getDate())}_${n(at.getHours())}-${n(at.getMinutes())}-${n(at.getSeconds())}`;
};

function write(path: string, data: string) {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.tmp`;
  writeFileSync(tmp, data);
  renameSync(tmp, path);
}

/** A free name in history/: two prompts can fall into one second. */
function archive(notes: string, at: Date, finished: boolean): string {
  const name = `${stamp(at)}_prompt${finished ? "" : "_unfinished"}`;
  let path = join(notes, "history", `${name}.txt`);
  for (let n = 2; existsSync(path); n++) path = join(notes, "history", `${name}-${n}.txt`);
  return path;
}

export class PromptLog {
  private notes: string;
  private text = "";
  private at = new Date();

  constructor(notes: string) { this.notes = notes; }

  /** A submitted prompt: the start of a run, or an addition to the one running (`during`). */
  submit(prompt: string, images: number, during: boolean) {
    const entry = images ? `${prompt}\n[${images} image${images === 1 ? "" : "s"} attached]` : prompt;
    if (!(during && this.text)) {
      if (this.text) this.finish(false);        // it never ran: no model, a failed request
      this.adopt();
      this.at = new Date();
    }
    this.text = this.text ? `${this.text}\n${entry}\n` : `${entry}\n`;
    write(promptPath(this.notes), this.text);
  }

  /** The run is over, retries included. */
  finish(finished: boolean) {
    if (!this.text) return;
    write(archive(this.notes, this.at, finished), this.text);
    let current = "";
    try { current = readFileSync(promptPath(this.notes), "utf8"); } catch { /* gone */ }
    if (current === this.text) rmSync(promptPath(this.notes), { force: true });
    this.text = "";
  }

  private adopt() {
    const path = promptPath(this.notes);
    try {
      const to = archive(this.notes, statSync(path).mtime, false);
      mkdirSync(dirname(to), { recursive: true });
      renameSync(path, to);
    } catch { /* nothing left behind */ }
  }
}
