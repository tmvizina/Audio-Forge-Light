# Orchestration protocol — concurrent writes, merges, and shared files

The waves in [README.md](README.md) say *which* tickets can run together. This document
says *how* to run them together without agents overwriting each other.

Read this before dispatching Wave 1, which is the first time six agents touch the repo at
once.

---

## 1. The core rule: one branch per ticket

Never let two agents write to the same working tree. Each ticket gets its own branch, and
the orchestrator is the only party that merges.

```bash
git switch -c ticket/T03-fish-client main
```

If your tooling supports it, give each parallel agent its own **git worktree**, so there is
no shared checkout to race on at all:

```bash
git worktree add ../wt-T03 -b ticket/T03-fish-client main
```

Agents never run `git merge`, never `git rebase`, and never touch `main`. An agent that
finishes reports back; the orchestrator merges.

## 2. The shared-file freeze list

These files are **owned by T01 and frozen after it lands**. No other ticket may edit them
directly, because every parallel ticket depends on them and a concurrent edit is both a
merge conflict and a broken contract:

| File | Why it's frozen |
|---|---|
| `types.py` | The `Chunk` shape and `compute_text_hash` — every ticket imports these |
| `events.py` | The NDJSON event names — the Node UI and T09 both bind to them |
| `config.json` | Every module reads its defaults from here |
| `requirements.txt` | Two agents adding a dependency is a guaranteed conflict |
| `.env.example` | Same |
| `.gitignore` | Same, and it must be correct from the first commit |
| `tests/conftest.py` | Shared fixtures; several Wave-1 tickets want to add to it |

**If a ticket needs one of these changed, it must not change it.** It reports a
**contract-change request** in its Report back, and the orchestrator applies the edit
centrally, between waves, on `main`. Then the affected branches rebase.

This costs one round trip and saves a class of silent breakage where two agents each add a
`pydantic` line with different version pins.

## 3. Pre-empt the three known collisions

These are already-known hazards in this ticket set. Handle them in **T01**, before Wave 1
starts, and they never arise:

| Hazard | Fix, applied in T01 |
|---|---|
| T10 needs `pydantic` (for `TagBatch`), T01 doesn't know | Add `pydantic` to `requirements.txt` up front |
| Several Wave-1 tickets want shared test fixtures | Create `tests/conftest.py` with the fake-HTTP-transport and ffmpeg-fixture helpers |
| T11 and T12 both need to register a `--tagger` backend, and they run in the same wave | T10 owns a **name→loader registry** with lazy import. Adapters are standalone files; adding one edits nothing shared |

The third one is the important one: without the registry, T11 and T12 are a guaranteed
conflict on whatever file holds the backend list. With it, they are genuinely independent.

## 4. Merge order and post-merge verification

Within a wave, merge in **ticket-number order**. It is arbitrary but deterministic, which
is what matters when you are reconstructing what happened.

After **every** merge, on `main`:

```bash
pytest tests/ -v
```

The full suite, not just the new ticket's file. A green ticket branch that goes red on
`main` means it silently depended on something another branch changed — catch it at the
merge, not three tickets later.

If the suite goes red after a merge:

1. **Do not fix it on `main`.**
2. Identify which ticket owns the failing behaviour.
3. Send it back to that ticket's agent with the failure output.

Fixing forward on `main` is exactly the drift this structure exists to prevent — it
launders a contract violation into "just a small fix" and the next agent inherits it.

## 5. Conflict resolution

If two branches conflict despite the freeze list, that is a **scoping bug in the tickets**,
not a merge problem. Resolve it by deciding who owns the file, not by hand-merging both
edits:

1. Determine which ticket legitimately owns the file per README's ownership table.
2. Keep that ticket's version.
3. Send the other ticket back with a note that it wrote outside its scope.
4. Record the incident — if it happens twice on the same file, the ticket boundary is wrong
   and needs redrawing before you continue.

Hand-merging two agents' edits to one file produces code neither agent tested.

## 6. Serial chains — do not parallelise these

Three files have multiple owners across the set. Their tickets must run **strictly in
order**, each starting from a tree that already contains the previous one:

| File | Order | Extra requirement |
|---|---|---|
| `chunker.py` | T02 → T06 → T07 | Each later ticket must re-run the earlier ones' tests and report them green |
| `narrate.py` | T08 → T09 | T08 adds only `prep-ref` |
| `README.md` | T14 → T15 | T14 adds the troubleshooting table only |

For these, branch from the **previous ticket's merged result**, not from the `main` that
existed when the wave started.

## 7. Dispatch checklist

Before handing a ticket to an agent:

- [ ] Its dependencies are merged to `main` and the suite is green there.
- [ ] It has its own branch or worktree, created from current `main`.
- [ ] The agent has: the ticket file, `BUILD-PROMPT.md` §1 (lines 24–59), and its own
      named line range. **Not** the whole build prompt.
- [ ] No other in-flight ticket owns any file this one owns (check README's table).
- [ ] The agent has been told it may not edit anything on the freeze list, and must file a
      contract-change request instead.

On receipt:

- [ ] The agent's Report back is complete, in the ticket's specified format.
- [ ] Its Definition-of-Done command was actually run, and its output was shown.
- [ ] The diff touches **only** files that ticket owns.
- [ ] Merge, then run the full suite on `main`.

## 8. What "done" means for a wave

A wave is complete when every ticket in it is merged and `pytest tests/ -v` is green on
`main` — not when every agent has reported success. Agents report on their own branch, in
isolation; the wave is only real once the pieces are together.

## 9. If you are not using git

The protocol degrades to: **run the wave serially**. One agent at a time, full test suite
between each. It is slower, but the failure mode of concurrent writes without version
control is losing work silently, which is strictly worse than being slow.

Do not attempt parallel agents against a single shared directory.
