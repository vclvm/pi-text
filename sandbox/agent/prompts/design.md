---
description: Design the current task (no code changes), saved in the project's notes
argument-hint: "[focus]"
---
Design the current task from `<project_notes>`. Extra focus from the user: ${@:-none}.
The current task decides what to work on and how; where it says anything different from the
defaults below, the task wins. This command only sets the kind of work and the result expected.
If the task names Jira tickets (ABC-123) or says to take something from Jira, and the `jira_issue`
tool is there, read them first, with `linked: true` when the task points at related tickets: they
are part of the task.

Don't change any file of the repository. Read the code the task touches, then write exactly one
file `designs/<YYYY-MM-DD_HH-MM-SS>_<2-3-words>_design.md` in the project's notes folder with:

1. Task understanding
2. Current state (the code as it is, with file paths)
3. Options and trade-offs (only real ones)
4. Chosen design: the simplest one that fits the existing architecture
5. Files and code areas to change
6. Test strategy (which tests, isolated how)
7. Risks and failure modes
8. Implementation steps
9. Acceptance criteria
10. Open questions and assumptions

Reply with only: what was designed, and the full path of the file.
