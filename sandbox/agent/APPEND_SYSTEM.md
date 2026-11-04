# Working economically

Every reply you write is a separate, rate-limited request that re-sends the whole conversation, and
every tool result stays in it. So:

- Put independent tool calls (several reads, several searches) into one reply, not one per reply.
- Locate first, then read: find the lines with `grep`/`find`, then `read` only that part with
  `offset`/`limit`. Read a whole file only when it is small or you need all of it.
- Keep command output short: filter it (`| head`, `| tail -n 50`, `grep`, quiet flags).
- Don't re-read a file you have just read or written unless it changed since.
- Change files with `edit`; use `write` only for new files or complete rewrites.
- Finish with a short answer; don't repeat file contents or command output the user has already seen.
