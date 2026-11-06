---
description: Implement the current task, with tests and a report in the project's notes
argument-hint: "[extra instructions]"
---
Implement the current task from `<project_notes>`. Extra instructions from the user: ${@:-none}.
The current task decides what to work on and how; where it says anything different from the
defaults below, the task wins. This command only sets the kind of work and the result expected.
If there is a design for it in `designs/` of the notes folder, follow the latest one.

1. Read the code the task touches, starting from the most relevant files.
2. Make the change: minimal, in the project's existing style.
3. Add or update tests for the changed behaviour.
4. Run the relevant tests (targeted first, e.g. `-Dtest=…`), and fix what fails.
5. Write `implemented/<YYYY-MM-DD_HH-MM-SS>_<2-3-words>_implement.md` in the notes folder: what
   changed and why, the behaviour now, tests added or changed, commands run and their results,
   remaining risks.

Reply with only: what was implemented, the test result, and the full path of the report.
If blocked: the blocker, why it blocks, and exactly what information is missing.
