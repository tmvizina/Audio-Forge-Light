# Implementation Review — simple-narrator-app doc set

**Date:** 2026-08-23
**Target:** `C:\Users\tmviz\RiderProjects\Audio-Forge-Light\docs\simple-narrator-app\`
**Status:** Complete, uncommitted, ready for manual review and commit.
**Built by:** orchestrator + 4 parallel Sonnet 5 agents, reviewed and corrected by the orchestrator.

---

## What was delivered

| File | Words | Purpose |
|---|---|---|
| `BUILD-PROMPT.md` | 9,612 | The complete build spec. §1–14 + closing checklist. Paste into Claude Code or Codex. |
| `README.md` | 2,196 | Human-facing. Install → key → record clip → run → tune → cost → troubleshoot. |
| `AGENTS.md` | 1,494 | Agent-facing. Repo map, commands, 12 invariants, NDJSON contract, extension points. |
| `CLAUDE.md` | 67 | Pointer to `AGENTS.md`, so one doc stays canonical. |

Plus this review. The plan called for four files; the fifth is the requested review artifact
and should probably **not** be copied into the new repo — it is about the build, not the app.

---

## Verification results (the plan's 6-point checklist)

| # | Check | Result |
|---|---|---|
| 1 | **Self-containment** — no `audio-forge`, `server/src`, `worker/`, `AF_` | ✅ Zero hits across all four files |
| 2 | **Codex-completeness** — no skills, MCP, plan mode, Claude-only affordances | ✅ Zero hits. Tagger ships a real OpenAI path, not a TODO |
| 3 | **Executability** — 200, 900, 3000, 3, 1 literal; 1200 only as upper end | ✅ All present. Fish `model` **header** + raw-bytes msgpack correct |
| 4 | **Anthropic correctness** — `claude-opus-5`, no sampling params, `messages.parse`, `stop_reason` | ✅ All correct; forbidden params appear **only** as warnings |
| 5 | **Spot-check risky claims** against real source | ✅ All five verified — see table below |
| 6 | **Hand files back** | ✅ Delivered |

### Source spot-checks (read before any writing began)

| Claim | Source | Verdict |
|---|---|---|
| `model` in a **header**, msgpack body, raw ref bytes | `server/src/fish.ts:84-93` | ✅ Exact match |
| `anullsrc=r=44100:cl=mono -t <sec> -c:a pcm_s16le` | `worker/audioforge_worker/stitch.py:21-24` | ✅ Exact match |
| Sentence boundary = punct + ws + EOF/opener/capital | `worker/audioforge_worker/chunker.py:61-74` | ✅ Exact match; `_SENT_END`/`_OPENERS` transcribed literally |
| `isSpeakable` = `/[A-Za-z0-9]/` | `server/src/fishBatch.ts:59` | ✅ Exact match |
| Narration defaults `temperature 0.7 / top_p 0.7`, `chunk_length 300` | `server/src/fishBatch.ts:88` | ✅ Exact match |

---

## Defects I found and fixed

These were real, not cosmetic. Listing them because they're the reason a review pass exists.

1. **16 dangling cross-references.** Part 2's agent cited the internal fact sheet's
   lettered sections (`§Q`, `§P`, `§L`, `§G`, `§D`, `§B`, `§N`, `§A`, `§O`) which do not
   exist in `BUILD-PROMPT.md`. A reading agent would have hunted for "§Q" and found
   nothing — a direct hit on the self-containment requirement. All 16 remapped to the real
   numbered sections.

2. **Wrong section numbers + split artifacts.** Part 1 guessed stitching was §9 (it's §10)
   and left six "(Part 2)" references from the parallel-writing split. Fixed; every `§N`
   reference in the document now resolves to a real section.

3. **Unverified API claim, stated as fact.** Both `BUILD-PROMPT.md` and `AGENTS.md`
   asserted that putting `model` in the body makes the request "silently fall back to a
   default" / "be silently ignored or rejected." I verified the header is *required*; I did
   **not** verify what the server does when it's absent. Reworded both to state what is
   known (you lose control of which model runs; the request still returns audio, so it
   fails quietly) without inventing server behaviour.

---

## Judgment calls worth your attention

**The concat demuxer is a deliberate divergence, not a transcription.** Audio Forge itself
uses the concat **filter** (`worker/audioforge_worker/io_utils.py:154`), chosen for
robustness to mixed input codecs. The plan mandates the **demuxer** instead. That's correct
— the filter genuinely does blow Windows' 8191-char command line at 300 chunks — but it
only works because every wav is normalised to 44100/mono/pcm_s16le **on arrival**. I made
sure both `BUILD-PROMPT.md` §10 and `AGENTS.md` invariant 5 state that precondition
alongside the rule, so nobody later "optimises away" the normalisation and silently gets
garbage output. This is the one place the doc set knowingly contradicts the source system,
and it's the right call.

**No fabricated prices.** The README gives arithmetic (~100k chars → ~500 chunks → one call
each → tens of minutes at concurrency 3) and tells the user to price one chapter first,
rather than quoting a dollar figure that would be stale on arrival. Correct instinct.

**The OpenAI gap is deliberate and clearly labelled.** The Anthropic specifics were verified
this session; the OpenAI call shape was not. Rather than guess, the prompt instructs the
builder to check OpenAI's live docs before writing that adapter, *and says why* — same
discipline as the Fish header, opposite conclusion. `OPENAI_TAG_MODEL` correctly has **no
default**, with the reasoning stated so a builder doesn't "helpfully" invent one.

---

## My assessment of the final state

**The doc set is ready to hand to either agent.** It does the thing that actually matters:
it transcribes the expensive lessons rather than citing them. The 47-seconds-of-grunting
guard, the 6.3-minutes-of-stage-directions leak, the sentence-splitter walk, and the exact
msgpack wire format are all present with their real-world consequences attached — which is
what makes a builder take them seriously instead of simplifying them away.

Three things I'd flag honestly:

1. **`BUILD-PROMPT.md` is 9,600 words.** That's long for a single paste. It's justified —
   the density is facts, not padding, and §13's acceptance tests are the backstop against
   an agent skimming. But expect a weaker agent to need the "build in section order, run
   §13 before reporting done" instruction enforced rather than trusted. If you find an
   agent drifting, feed it §1–7 first and §8–14 after the chunker passes its tests.

2. **The tag vocabulary is a judgment call, not a verified list.** Roughly thirty words,
   ten of which came from the plan and twenty of which the agent chose sensibly
   (`tender`, `resigned`, `sardonic`, …). They're reasonable, but they're untested against
   the actual TTS. Expect to tune that list once you hear real output — it's a one-line
   change in `tagger/base.py`.

3. **Nothing here has been executed.** This is a spec, and specs are wrong in ways only a
   build reveals. The highest-risk untested area is the OpenAI adapter (deliberately
   unverified), followed by the concat-demuxer path on a real 300-chunk chapter. The
   acceptance tests in §13 are designed to catch exactly these, which is why "run them
   before reporting done" is stated three times.

**Recommendation:** commit as-is. Copy the four `.md` files (not this review) into the fresh
repo root, hand `BUILD-PROMPT.md` to whichever agent you have, and hold it to §13.

---

## Repo state

Left **uncommitted** for your manual commit, as requested:

```
?? docs/simple-narrator-app/BUILD-PROMPT.md
?? docs/simple-narrator-app/README.md
?? docs/simple-narrator-app/AGENTS.md
?? docs/simple-narrator-app/CLAUDE.md
?? docs/simple-narrator-app/IMPLEMENTATION-REVIEW.md
```

Note: `Audio-Forge-Light` is an empty repo — branch `main` has **no commits yet**, so this
will be the initial commit. `.idea/` is also untracked; you may want a `.gitignore` before
committing. No file in the Audio Forge source repo was modified.
