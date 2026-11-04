// node --experimental-strip-types --test sandbox/test/*.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { meaningful, section } from "../extensions/project-context.ts";

test("the untouched templates count as not written", () => {
  for (const name of ["current-task.md", "AGENTS.md"]) {
    assert.equal(meaningful(readFileSync(new URL(`../agent/templates/${name}`, import.meta.url), "utf8")), "", name);
  }
});

test("a filled task is kept, without the hints", () => {
  const text = readFileSync(new URL("../agent/templates/current-task.md", import.meta.url), "utf8")
    .replace("- Out of scope:", "- Out of scope: no database in the test");
  const kept = meaningful(text);
  assert.match(kept, /no database in the test/);
  assert.doesNotMatch(kept, /<!--/);
});

test("the section names the folder, the rules and the task", () => {
  const s = section("/n/Projects/api", "Java 21", "Write a test");
  assert.match(s, /notes folder of this project: \/n\/Projects\/api/);
  assert.match(s, /Project rules \(\/n\/Projects\/api\/AGENTS.md\)\n\nJava 21/);
  assert.match(s, /Current task \(\/n\/Projects\/api\/current-task.md\)\n\nWrite a test/);
  assert.match(section("/n/x", "", ""), /No current task is written down/);
});
