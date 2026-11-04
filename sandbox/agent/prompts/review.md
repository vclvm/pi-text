---
description: Review the current changes against the task, report in the project's notes
argument-hint: "[focus]"
---
Review the current changes (`git status`, `git diff`, new files) against the current task from
`<project_notes>`. Extra focus from the user: ${@:-none}.

Don't change any file of the repository. Look for: correctness, the task's acceptance criteria,
fit with the architecture and its layers, error handling and validation, API and persistence
compatibility, concurrency, test quality (does a test really check the behaviour, is it isolated),
regression risk. Skip cosmetic remarks unless they matter for correctness or maintainability.

Write `reviews/<YYYY-MM-DD_HH-MM-SS>_<2-3-words>_review.md` in the notes folder:
1. Findings by severity (critical, major, minor); each: problem, why it matters, file:line, fix
2. Risks you could not verify
3. What looks right

Reply with only: the verdict, the number of critical findings, and the full path of the file.
