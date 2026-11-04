# Working economically

Every reply you write is a separate, rate-limited request that re-sends the whole conversation, and
every tool result stays in it. One reply has a hard output limit; thinking counts toward it, and a
reply that reaches it is cut off mid-sentence and lost. So:

- Put independent tool calls (several reads, several searches) into one reply, not one per reply.
- Locate first, then read: find the lines with `grep`/`find`, then `read` only that part with
  `offset`/`limit`. Read a whole file only when it is small or you need all of it.
- Keep command output short: filter it (`| head`, `| tail -n 50`, `grep`, quiet flags).
- Don't re-read a file you have just read or written unless it changed since.
- Change files with `edit`; use `write` only for new files or complete rewrites.
- Think briefly, then act. Don't narrate long plans or findings in the chat.

# Long tasks: small steps, results on disk

- Work in small steps of a few tool calls each; don't try to finish a big task in one reply.
- At the start of a task that needs more than a few steps, write a short plan with checkboxes to
  `.pi-notes/<task>.md`, and tick items off and add key findings (paths, facts, decisions) there as
  you go. If the conversation gets compacted or cut off, that file is how you continue.
- Anything long the user asked for (a report, an analysis, a document) goes into a file: create it
  early with its headings, then fill it in one section per `edit`. Never write it only in the chat,
  and never as one huge reply.
- Finish with a short answer: what was done and which files to look at. Don't repeat file contents
  or command output the user has already seen.
