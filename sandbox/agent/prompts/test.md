---
description: Test the current task's implementation, report in the project's notes
argument-hint: "[what to check]"
---
Test the implementation of the current task from `<project_notes>`. What to check, from the
user: ${@:-the task's acceptance criteria}.
The current task decides what to work on and how; where it says anything different from the
defaults below, the task wins. This command only sets the kind of work and the result expected.

Don't change the code under test. Run the tests that cover the change first, then a wider run if
it is cheap. If a test fails, find the cause from the evidence (output, code), not by guessing.

Write `tests/<YYYY-MM-DD_HH-MM-SS>_<2-3-words>_test.md` in the notes folder:
1. Scope chosen and why
2. Commands run
3. Results
4. Failure analysis (if any) and the smallest correct fix
5. Regression risk
6. Verdict

Reply with only: the verdict, what was run, and the full path of the report.
