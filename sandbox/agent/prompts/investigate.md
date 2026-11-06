---
description: Find a bug's cause or explain how something works (no code changes), report in the project's notes
argument-hint: "[question; empty = the current task]"
---
Investigate: ${@:-the current task from `<project_notes>`}.
If a current task is written in `<project_notes>`, it is the context of this question: keep to it.
The current task decides what to work on and how; where it says anything different from the
defaults below, the task wins. This command only sets the kind of work and the result expected.

Don't change the repository's code. A temporary test or script to reproduce or check something is
fine; remove it when done, unless the user wants it kept.

Work from evidence, not plausibility: every claim cites `file:line`, a command's output, a log
line or a commit. Mark clearly what you verified (read or ran) and what is an assumption.

- **A bug or wrong behaviour:** reproduce it first (a command, a request, a failing test). Trace the
  data from the entry point to where it goes wrong. Check each hypothesis by running something, not
  only by reading. Find the root cause, not the first place that looks odd. Propose the smallest
  correct fix and the test that would pin it, but don't apply the fix.
- **How or why something works:** start at the entry point (endpoint, listener, scheduler, main),
  follow the calls and the configuration that steers them. For "why is it built so", look at the
  history: `git log -S'<code>' --oneline`, `git log -L`, `git blame` and the commit messages.

If three steps in a row brought no new facts, stop: say what is missing and what would find it.

Write `investigations/<YYYY-MM-DD_HH-MM-SS>_<2-3-words>_investigation.md` in the notes folder:
1. Answer in at most three lines
2. Evidence: the chain with `file:line`, commands and outputs
3. Root cause (for a bug) / how it works and why (for a question)
4. Proposed fix and test (for a bug)
5. Open points: what is assumed, what could not be checked

Reply with the three-line answer and the full path of the file.
