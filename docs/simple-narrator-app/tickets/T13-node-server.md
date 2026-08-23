# T13 — Node server wrapper

**Depends on:** T09. **Blocks:** T14.
**Read:** `BUILD-PROMPT.md` §11 (lines 958–1030). **Nothing else.**

## Why this ticket exists

An optional browser UI that makes the CLI clickable. The entire risk in this ticket is
scope: it is very tempting to "improve" the UI by computing something in JavaScript. Every
such improvement is a second implementation of pipeline logic that will drift from the
Python one.

**~150 lines. `express` only. No build step, no React, no bundler, no TypeScript.**

## Files you own

`server/index.js` and one static HTML page.

## Files you must NOT touch

Any Python file. If the UI needs a piece of information, it must already be a field in an
event Python emits. If it isn't, **report that** — the fix is a new field in T01's event
contract, not a calculation in JS.

## Task

**1. `GET /`** — one static HTML page (inline `<style>` / `<script>`, no external assets):

- a file picker for the input `.txt`
- a picker for the reference clip (`reference/narrator.wav`)
- numeric fields for `gap_ms` and `concurrency`
- a tagger selector (`none` / `claude` / `codex`)
- Start and Cancel buttons
- a live-scrolling log panel
- an `<audio>` element to preview each chapter mp3 as it finishes

**2. `POST /api/run`** — accepts the form fields as JSON and spawns
`python narrate.py run <args...>` as a child process. **Only one run at a time**; a second
`POST /api/run` while one is active returns **409**.

**3. `GET /api/events`** — a Server-Sent-Events stream forwarding the child's **stdout**,
line by line, as SSE `data:` frames. It does **not** parse or interpret the JSON — pass the
line through and let the front-end decide how to render it.

Forward **stdout only**. Human/debug logging goes to stderr and is not part of the UI
contract; the server may echo stderr to its own console for debugging but must never feed
it to `/api/events`.

**4. `POST /api/cancel`** — kills the running child (`SIGTERM`, then `child.kill()`, which
maps to `TerminateProcess` on Windows).

**5. The contract.** Node's entire job is: spawn, read stdout lines, `JSON.parse` each,
forward to SSE. **It re-implements none of the chunking, retry, or stitching logic.**

Event types available (frozen in T01): `run_started`, `chunked`, `tagged`, `chunk_done`,
`chunk_failed`, `concurrency_changed`, `stitched`, `done`, `error`.

**6. Windows spawn detail — get this right.** Spawn the interpreter **explicitly**:

```js
spawn(pythonExePath, ["narrate.py", "run", ...args])
```

where `pythonExePath` resolves to the venv's `python.exe` (or plain `"python"` if no venv).

- **Never rely on a shebang** — Windows does not execute `.py` files via one.
- **Never pass `{ shell: true }`** — it invokes `cmd.exe`, which mangles argument quoting
  differently than a direct spawn, and it is an unnecessary injection surface for
  user-supplied file paths.

**7. Unbuffered stdout.** If the Python child buffers, the SSE stream appears frozen and
the UI looks broken. Spawn with `-u` (or ensure T01's emitter flushes every line). Verify
this with a real run, not by reading the code.

**8. Both halves stand alone.** The CLI is fully usable with the server never started, and
the server is fully usable without the CLI being invoked by hand. Neither is a required
path through the other.

## Invariants at risk in this ticket

- **#9** — never render or log an API key. The `run_started` event carries `config`; that
  is already sanitised upstream, but do not add anything that reads `.env` in Node.
- **The no-logic-in-JS rule** — the single most likely drift in this ticket.

## Definition of done

```bash
node server/index.js
# then, in a browser: start a run on the two-chapter fixture and watch it complete
```

Ship at least these tests (a light `node:test` suite or a documented manual script is
acceptable here — this is a thin UI layer, not core logic):

1. `POST /api/run` spawns the child with the interpreter as argv[0] and **no**
   `shell: true`. Assert on the spawn arguments.
2. A second `POST /api/run` during an active run returns **409**.
3. `GET /api/events` forwards a line from the child's stdout **verbatim** — feed a fake
   child a known NDJSON line and assert the SSE frame matches byte for byte.
4. stderr from the child does **not** appear in the SSE stream.
5. `POST /api/cancel` terminates the child; a subsequent `POST /api/run` succeeds.
6. **The live buffering check**: a real end-to-end run streams events *as they happen*, not
   in one burst at the end. This is the one that catches an unbuffered-stdout mistake, and
   it cannot be verified by reading code.
7. A malformed (non-JSON) stdout line does not crash the server.

## Report back

- The spawn call, verbatim.
- Confirmation that no pipeline logic exists in JS — list every computation the front end
  performs, so the orchestrator can confirm each is pure rendering.
- Confirmation that test 6 was run for real and events arrived incrementally.
- Any information the UI wanted that no event currently carries (report it; do not compute
  it).
- The line count of `server/index.js`.
