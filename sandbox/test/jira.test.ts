import { test } from "node:test";
import assert from "node:assert/strict";
import { apiUrl, failure, formatIssue, related } from "../extensions/lib/jira.ts";

const ISSUE = {
  key: "PAY-12",
  fields: {
    summary: "Refund fails for split payments", status: { name: "In Progress" }, issuetype: { name: "Bug" },
    assignee: { displayName: "A. Dev" }, labels: ["payments"], description: "Steps: refund a split payment.\nExpected: refunded.",
    parent: { key: "PAY-1", fields: { summary: "Refunds epic", status: { name: "Open" } } },
    subtasks: [{ key: "PAY-13", fields: { summary: "Add a test", status: { name: "Done" } } }],
    issuelinks: [
      { type: { outward: "blocks", inward: "is blocked by" }, outwardIssue: { key: "PAY-20", fields: { summary: "Release", status: { name: "Open" } } } },
      { type: { outward: "blocks", inward: "is blocked by" }, inwardIssue: { key: "CORE-7", fields: { summary: "Ledger API", status: { name: "Done" } } } },
    ],
    comment: { comments: Array.from({ length: 12 }, (_, i) => ({ author: { displayName: `u${i}` }, created: "2026-11-01T10:00", body: `comment ${i}` })) },
    attachment: [{ filename: "trace.log" }],
  },
};

test("the agent always asks plain http, with the context path kept", () => {
  assert.equal(apiUrl("https://jira.corp", "/rest/api/2/issue/A-1"), "http://jira.corp/rest/api/2/issue/A-1");
  assert.equal(apiUrl("https://corp.example/jira/", "/rest/x"), "http://corp.example/jira/rest/x");
  assert.equal(apiUrl("https://jira.corp:8443", "/r"), "http://jira.corp:8443/r");
});

test("an issue as text: description, parent, subtasks, links both ways, the last comments", () => {
  const text = formatIssue(ISSUE, "https://jira.corp/jira");
  for (const want of ["# PAY-12: Refund fails for split payments", "Status: In Progress", "Steps: refund a split payment.",
    "URL: https://jira.corp/jira/browse/PAY-12", "PAY-1: Refunds epic (Open)", "PAY-13: Add a test (Done)",
    "blocks PAY-20: Release (Open)", "is blocked by CORE-7: Ledger API (Done)", "trace.log", "the last 10 of 12", "comment 11"]) {
    assert.ok(text.includes(want), want);
  }
  assert.ok(!text.includes("comment 1:") && !text.includes(": comment 0"), "the oldest comments are left out");
  assert.ok(!formatIssue(ISSUE, "https://jira.corp", false).includes("## Comments"), "a linked issue has no comments");
});

test("related issues: parent, subtasks, links, once each", () => {
  assert.deepEqual(related(ISSUE), ["PAY-1", "PAY-13", "PAY-20", "CORE-7"]);
  assert.deepEqual(related({ key: "X-1", fields: {} }), []);
});

test("failures say what to do", () => {
  assert.match(failure(401, "", "A-1"), /jira_token/);
  assert.match(failure(404, "", "A-1"), /doesn't exist/);
  assert.match(failure(403, "egress policy: jira.corp is a private/internal address", "A-1"), /proxy refused/);
});
