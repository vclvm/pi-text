---
description: Review what the current task names, or else the current changes, report in the project's notes
argument-hint: "[focus]"
---
Do the review the current task from `<project_notes>` asks for. Extra focus from the user: ${@:-none}.
The current task decides what to work on and how; where it says anything different from the
defaults below, the task wins. This command only sets the kind of work and the result expected.

What to review is what the task names: a branch against another, a range of commits, a module, a
set of files. Find out exactly that with git, and read it whole. Only if the task names nothing:
all the work of the current branch against the repository's default branch (from their merge base),
plus uncommitted changes and new files; on the default branch itself, the uncommitted changes.
Say in the report what you compared.

Don't change any file of the repository. Look for: correctness, the task's acceptance criteria,
fit with the architecture and its layers, error handling and validation, API and persistence
compatibility, concurrency, test quality (does a test really check the behaviour, is it isolated),
regression risk. Skip cosmetic remarks unless they matter for correctness or maintainability.

Write `reviews/<YYYY-MM-DD_HH-MM-SS>_<2-3-words>_review.md` in the notes folder:
1. Findings by severity (critical, major, minor); each: problem, why it matters, file:line, fix
2. Risks you could not verify
3. What looks right

Reply with only: the verdict, the number of critical findings, and the full path of the file.
