import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const DIR = new URL("../agent/prompts/", import.meta.url).pathname;
const templates = readdirSync(DIR).filter((n) => n.endsWith(".md"));

test("no command replaces the current task", () => {
  assert.ok(templates.length >= 5);
  for (const name of templates) {
    const text = readFileSync(join(DIR, name), "utf8");
    assert.match(text, /<project_notes>/, name);
    assert.match(text, /the task wins/, name);
    assert.match(text, /`jira_issue`/, `${name}: reads the Jira tickets the task names`);
  }
});

test("/review takes what to review from the task, git status is no more than a fallback", () => {
  const text = readFileSync(join(DIR, "review.md"), "utf8");
  assert.doesNotMatch(text, /`git status`|`git diff`/);
  assert.match(text, /What to review is what the task names/);
});
