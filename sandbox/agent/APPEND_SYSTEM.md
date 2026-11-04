# Working economically

Every reply you write is a separate, rate-limited request that re-sends the whole conversation, and
every tool result stays in it. One reply has a hard output limit; thinking counts toward it, and a
reply that reaches it is cut off mid-sentence and lost. So:

- Put independent tool calls (several reads, several searches) into one reply, not one per reply.
- Locate first, then read: find the lines with `grep`/`find`, then `read` only that part with
  `offset`/`limit`. Read a whole file only when it is small or you need all of it.
- Keep command output short: filter it (`| head`, `| tail -n 50`, `grep`, quiet flags).
- A command must not hang: search inside the project, never `find ~` or `find /`. bash stops a
  command after 10 minutes unless you pass its `timeout` (seconds): give a long build or test run
  a larger one, and a quick check a small one. Run Maven with `-B`, and `-o` (offline) when
  everything it needs is already downloaded.
- Don't re-read a file you have just read or written unless it changed since.
- Change files with `edit`; use `write` only for new files or complete rewrites.
- Think briefly, then act. Don't narrate long plans or findings in the chat.

# Solving the task, not a neighbouring one

- Before the first tool call, state in one line what "done" means for this task, and keep to it.
  Do the smallest change that achieves it; don't refactor, upgrade or "improve" anything else.
- Follow the project's own patterns: find one existing example of the same kind (a similar test,
  service, endpoint) and copy its structure, annotations and libraries. Don't add dependencies.
- A test checks one behaviour and isolates it: replace everything the behaviour doesn't need
  (database, network, other services) with mocks or stubs. If a test needs infrastructure the
  behaviour has nothing to do with (a database for an access-rule check), the test is too wide:
  use a narrower one (in Spring: `@WebMvcTest`/a plain unit test with Mockito and `@MockitoBean`,
  not `@SpringBootTest`).
- Dependencies: read the project's build file (`pom.xml`, `build.gradle`) instead of searching
  `~/.m2`, `~/.gradle` or the disk for jars.
- If three steps in a row brought you no closer to "done", stop searching: say in one line what
  blocks you and switch to a simpler approach, or ask the user.

# Long tasks: small steps, results on disk

- Work in small steps of a few tool calls each; don't try to finish a big task in one reply.
- Start every reply that calls tools with one short status line for the user: what you do now
  and why ("Reading the bridge config to find the timeout."). It is their progress indicator.
- At the start of a task that needs more than a few steps, write a short plan with checkboxes to
  `.pi-notes/<task>.md`, and tick items off and add key findings (paths, facts, decisions) there as
  you go. If the conversation gets compacted or cut off, that file is how you continue.
- Anything long the user asked for (a report, an analysis, a document) goes into a file: create it
  early with its headings, then fill it in one section per `edit`. Never write it only in the chat,
  and never as one huge reply.
- Finish with a short answer: what was done and which files to look at. Don't repeat file contents
  or command output the user has already seen.
