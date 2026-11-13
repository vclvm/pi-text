---
description: Review what the current task names, or else the current changes, report in the project's notes
argument-hint: "[focus]"
---
Do the review the current task from `<project_notes>` asks for. Extra focus from the user: ${@:-none}.
The current task decides what to work on and how; where it says anything different from the
defaults below, the task wins. This command only sets the kind of work and the result expected.
If the task names Jira tickets (ABC-123) or says to take something from Jira, and the `jira_issue`
tool is there, read them first, with `linked: true` when the task points at related tickets: they
are part of the task.

What to review is what the task names: a branch against another, a range of commits, a module, a
set of files. Only if the task names nothing:
all the work of the current branch against the repository's default branch (from their merge base),
plus uncommitted changes and new files; on the default branch itself, the uncommitted changes.

Don't change any file of the repository. Look for: correctness, the task's acceptance criteria,
fit with the architecture and its layers, error handling and validation, API and persistence
compatibility, concurrency, test quality (does a test really check the behaviour, is it isolated),
regression risk. Skip cosmetic remarks unless they matter for correctness or maintainability.

Write `reviews/<YYYY-MM-DD_HH-MM-SS>_<2-3-words>_review.md` in the notes folder:
1. Context, before any finding:
   - What was compared: the refs or paths, the merge base, the commits, uncommitted changes in or out
   - What the task asks: the goal and acceptance criteria, from the task and the Jira tickets it names
   - What the changes do: what this branch (the MR) changes, by area, in a few lines
   - What was verified: what you read, the commands you ran (build, tests) and their result, what
     you could not run and why
2. Findings by severity (critical, major, minor); each: problem, why it matters, file:line, fix
3. Risks you could not verify
4. What looks right

Reply with only: the verdict, the number of critical findings, and the full path of the file.
