import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readdirSync, readFileSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PromptLog, promptPath, stamp } from "../extensions/lib/prompts.ts";

const folder = () => mkdtempSync(join(tmpdir(), "prompts-"));
const history = (notes: string) => (existsSync(join(notes, "history")) ? readdirSync(join(notes, "history")).sort() : []);
const read = (notes: string, name: string) => readFileSync(join(notes, "history", name), "utf8");

test("a prompt is current while it runs, then history", () => {
  const notes = folder(), log = new PromptLog(notes);
  log.submit("fix the login", 0, false);
  assert.equal(readFileSync(promptPath(notes), "utf8"), "fix the login\n");
  assert.deepEqual(history(notes), []);
  log.finish(true);
  assert.equal(existsSync(promptPath(notes)), false);
  const [name] = history(notes);
  assert.match(name, /^\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d_prompt\.txt$/);
  assert.equal(read(notes, name), "fix the login\n");
});

test("what is sent during a run belongs to it", () => {
  const notes = folder(), log = new PromptLog(notes);
  log.submit("one", 0, false);
  log.submit("and also two", 1, true);
  log.finish(true);
  assert.equal(history(notes).length, 1);
  assert.equal(read(notes, history(notes)[0]), "one\n\nand also two\n[1 image attached]\n");
});

test("a run that didn't finish says so, and a second finish does nothing", () => {
  const notes = folder(), log = new PromptLog(notes);
  log.submit("one", 0, false);
  log.finish(false);
  log.finish(true);
  assert.equal(history(notes).length, 1);
  assert.match(history(notes)[0], /_prompt_unfinished\.txt$/);
});

test("prompts within one second don't overwrite each other", () => {
  const notes = folder(), log = new PromptLog(notes);
  for (const p of ["a", "b", "c"]) { log.submit(p, 0, false); log.finish(true); }
  assert.deepEqual(history(notes).map((n) => read(notes, n)).sort(), ["a\n", "b\n", "c\n"]);
});

test("what a killed process left is filed as unfinished, under its own time", () => {
  const notes = folder(), at = new Date(2026, 0, 2, 3, 4, 5);
  writeFileSync(promptPath(notes), "killed\n");
  utimesSync(promptPath(notes), at, at);
  new PromptLog(notes).submit("next", 0, false);
  assert.equal(read(notes, `${stamp(at)}_prompt_unfinished.txt`), "killed\n");
  assert.equal(readFileSync(promptPath(notes), "utf8"), "next\n");
});

test("another process's prompt is not taken from under it", () => {
  const notes = folder(), mine = new PromptLog(notes);
  mine.submit("mine", 0, false);
  writeFileSync(promptPath(notes), "theirs\n");
  mine.finish(true);
  assert.equal(readFileSync(promptPath(notes), "utf8"), "theirs\n");
  assert.equal(read(notes, history(notes)[0]), "mine\n");
});
