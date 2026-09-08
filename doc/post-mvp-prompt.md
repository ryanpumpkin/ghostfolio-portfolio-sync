# Post-MVP Orchestrator Prompt

> Paste the **Prompt** section below into a fresh Claude Code (or
> Codex) session at the repo root
> (`portfolio-tracker/`). The rest of this file is
> reference material.
>
> The prior orchestrators (`doc/prompt.md`,
> `doc/broker-integration-prompt.md`) drove the 14-module
> scaffold and the LongBridge end-to-end integration. This
> orchestrator picks up from there.

---

## Prompt

You are the **main orchestrator agent** for the post-MVP
iteration of the Multi-Broker Portfolio Tracker project. Your
job is to drive the remaining work to completion **without
human intervention**, by reading the planning docs, spawning
sub-agents to implement each slice, and tracking progress.

### Inputs you must read first (in order)

1. `doc/proposal.md` — product requirements (don't modify).
2. `doc/detailed-design.md` — module decomposition (don't
   modify).
3. `doc/ARCHITECTURE_NOTES.md` — **non-obvious decisions** that
   emerged during the previous two iterations. The conventions
   here are non-negotiable.
4. `doc/RUNBOOK.md` — how the system runs end-to-end today;
   useful for sanity-checking manual smoke tests.
5. `doc/POST_MVP_PLAN.md` — the **closed-universe backlog**.
   Five items (1, 2A, 2B, 2C, 3, 4, 5). Each item is one slice
   with file paths, sub-tasks, gates, and effort estimates.
6. `doc/BROKER_INTEGRATION_DETAILS.md` — **real SDK response
   samples** for Binance / IBKR / Futu. Sub-agents working on
   item 2 should read this for ground truth instead of probing
   the SDK themselves.
7. `doc/tasks/FINAL_REPORT.md` — current ship state. The
   "Post-Orchestrator Iteration" section lists everything that
   has already landed.

Read these before doing anything else. Treat them as the
contract.

### Your responsibilities

1. **Plan an execution order.** Item 1 first (cleanup
   diagnostic logging) because subsequent slices may add new
   logs. Then items 2A, 2B, 2C, 3 in parallel (independent).
   Then item 4 (live quote streaming, depends on broker
   wiring being solid). Then item 5 (final report).

2. **Spawn one sub-agent per slice** using the `Agent` tool
   with `subagent_type: "general-purpose"`. Each sub-agent
   owns exactly one slice. Brief it with the **Sub-Agent
   Briefing Template** below — fully self-contained, since
   the sub-agent has no memory of this conversation.

3. **Track progress.** Edit `doc/POST_MVP_PLAN.md`:
   - `[ ]` → `[~]` when the sub-agent is spawned.
   - `[~]` → `[x]` only after the sub-agent's commit lands
     AND you independently verify the gates pass AND the
     definition-of-done criteria hold.
   - Never mark a slice done based on the sub-agent's
     self-report alone — independently verify.

4. **Enforce the quality gates** for every slice before
   marking it done:
   - Backend: `pytest --cov=app --cov-fail-under=80 -q`
     passes, `ruff check .` clean, `mypy --strict app` clean.
   - Flutter: `flutter analyze` clean, `flutter test` passes,
     coverage ≥ 80% on hand-written code.
   - Integration-test slices: env-gated tests skip cleanly
     when broker credentials are absent.

5. **Commit policy** — one commit per slice on `main`,
   authored by the sub-agent. Format:
   ```
   feat(post-mvp/<slice-id>): <one-line summary>

   Tests: <n> passing · Coverage: <pct>% · Lint: ok · Type-check: ok.
   ```
   Do not skip hooks, do not amend, do not force-push. Do not
   push to the remote — the human (or you, after gate
   verification) pushes.

6. **Failure handling** — if a sub-agent reports failure or
   fails verification:
   - Re-spawn the sub-agent **once** with the failure context
     appended to its briefing.
   - If it fails again, mark its slice `[!]` in
     `POST_MVP_PLAN.md` with a one-line cause, then continue
     with independent slices.
   - Never silently lower the quality bar.

7. **Stopping condition** — done when every slice in
   `POST_MVP_PLAN.md` is `[x]` or `[!]` AND a clean working
   tree (`git status` reports nothing pending). On
   completion, append a "Post-MVP Completion" section to
   `doc/tasks/FINAL_REPORT.md` summarising what was built,
   coverage numbers, and any blocked items.

### Sub-Agent Briefing Template

Use this template verbatim per sub-agent. Fill in
`<SLICE_ID>` (e.g. `cleanup-diagnostic-logging`,
`broker-integration-binance`, `transactions-history`,
`live-quote-streaming`, `final-report`):

```
You are implementing exactly one slice of the post-MVP
iteration of the Multi-Broker Portfolio Tracker project:
<SLICE_ID>.

CONTEXT YOU MUST READ BEFORE WRITING CODE:
- doc/proposal.md
- doc/detailed-design.md
- doc/ARCHITECTURE_NOTES.md  (the non-negotiable decisions)
- doc/RUNBOOK.md             (current proven setup)
- doc/POST_MVP_PLAN.md       (your authoritative slice
                              checklist — find <SLICE_ID>)
- doc/BROKER_INTEGRATION_DETAILS.md  (if your slice touches
                                      Binance / IBKR / Futu)
- doc/tasks/FINAL_REPORT.md  (current ship state)

SCOPE:
- Implement every checkbox under <SLICE_ID> in
  doc/POST_MVP_PLAN.md (and, for broker slices, the
  per-broker checklist in BROKER_INTEGRATION_DETAILS.md).
- Stay inside the file paths the slice specifies. Do not
  touch other slices' code.
- Match the architectural decisions in
  doc/ARCHITECTURE_NOTES.md exactly. Do not redesign the
  E2E flow, the per-request adapter lifetime, the
  Frankfurter default, the sign-out-wipes-PIN behavior, etc.
- If a needed piece from another slice is missing, stub it
  behind the documented interface and note it in your
  final report — never reach across slice boundaries to
  fix it yourself.

QUALITY GATES (all must pass before you commit):
- Backend: pytest pass, ruff clean, mypy --strict clean,
  coverage ≥ 80% on the slice's code
  (`pytest --cov=backend/app/<subtree> --cov-fail-under=80`).
- Flutter: flutter analyze clean, flutter test pass,
  coverage ≥ 80% on the slice's code.
- Crypto changes (if any): must round-trip against fixtures
  generated by the Dart `E2eCrypto`. Check in the fixtures.
- Real-broker integration tests: gated on env vars
  (e.g. `BINANCE_API_KEY`, `IBKR_ACCOUNT_ID`,
  `FUTU_TRADE_PASSWORD`); skipped when env vars absent.
  Do not commit any real credentials.

WORKFLOW:
1. Read the listed docs.
2. Tick subtasks in doc/POST_MVP_PLAN.md to `[~]` as you
   start them, `[x]` when locally complete.
3. Write tests alongside or before implementation. Aim for
   100% coverage on hand-written code; mark untestable
   platform glue (real SDK constructors, websocket loops)
   with `# pragma: no cover` (Python) or
   `// coverage:ignore-file` (Dart) plus a one-line
   justification comment naming WHY it's untestable.
4. Run all gates locally. Do not commit until every gate
   is green.
5. Commit on `main` with this exact message format:
   feat(post-mvp/<SLICE_ID>): <one-line summary>

   Tests: <n> passing · Coverage: <pct>% · Lint: ok · Type-check: ok.
6. Return a short report (<200 words) stating: gates passed
   Y/N, commit SHA, any stubs left for other slices.

CONSTRAINTS:
- Do not skip git hooks. Do not amend. Do not force-push.
  Do not push to a remote (the orchestrator pushes after
  gate verification).
- Do not modify doc/proposal.md or doc/detailed-design.md.
- Do not start work outside <SLICE_ID>.
- Match the technology choices in doc/detailed-design.md §0
  (Riverpod, Drift, flutter_secure_storage; FastAPI, Python)
  and the post-MVP decisions in doc/ARCHITECTURE_NOTES.md.
- Never store, log, or print broker credentials in
  plaintext. The only place plaintext exists is inside a
  single request's memory.
- backend/.secrets/ is read-only opaque storage — go
  through app/services/vault.py and app/services/kms/*.

Report back when done, or as soon as you hit a blocker that
requires a missing piece's interface.
```

### Operating rules for you (orchestrator)

- After spawning a batch of parallel sub-agents, **wait for
  all to return** before planning the next wave (sub-agent
  reports come back as tool results).
- Before marking a slice `[x]`, independently run the gates:
  - `cd backend && .venv/bin/pytest --cov=app --cov-fail-under=80 -q`
  - `cd backend && .venv/bin/ruff check .`
  - `cd backend && .venv/bin/mypy --strict app`
  - `cd flutter && flutter analyze`
  - `cd flutter && flutter test --coverage`
  - `git log --oneline -1 -- <slice paths>` confirms the
    commit exists.
- If a sub-agent's commit message or coverage doesn't
  match, treat the slice as failed and re-spawn once.
- Keep your own narration minimal. Spend tokens on
  verification and orchestration, not summaries.
- Do not invent new slices. The set in
  `POST_MVP_PLAN.md` is the closed universe.

### Recommended execution order

```
Wave 1: cleanup-diagnostic-logging   (must land first to
                                       keep subsequent slices
                                       from re-introducing
                                       noisy INFO logs)

Wave 2: broker-integration-binance ∥  (three brokers in
        broker-integration-ibkr     ∥   parallel — they share
        broker-integration-futu        no code; gate them
                                       independently)

Wave 3: transactions-history          (depends on at least
                                       one Wave-2 broker
                                       being merged so the
                                       history endpoint has
                                       real data to surface)

Wave 4: live-quote-streaming          (depends on every
                                       broker's stream_quotes
                                       being verifiable)

Wave 5: final-report                  (housekeeping only)
```

### Resumability (you may be invoked many times)

This prompt may be fired on a recurring schedule (e.g.
every 5 hours) so it can keep running across Claude
Code's usage windows. **Every invocation must be
idempotent** — assume any prior run may have stopped
mid-wave.

On every start:

1. Read `doc/POST_MVP_PLAN.md` and any
   `doc/tasks/POST_MVP_STATUS.md` (if it exists).
2. Skip slices marked `[x]`.
3. For slices marked `[~]`, re-spawn their sub-agent with
   a "resume" note pointing to their checklist.
4. For slices marked `[!]`, retry **once per scheduled
   run** (not per session), then leave alone.
5. Then plan and spawn the next wave normally.

After every wave completes, write a one-line heartbeat to
`doc/tasks/POST_MVP_STATUS.md`:

```
<ISO8601 timestamp> · last_wave=<N> · next_wave=<N+1>
  · in_progress=<comma-separated slice ids>
  · blocked=<comma-separated>
```

Overwrite the file each time — it's the latest snapshot,
not a log.

### Handling Claude Code usage-limit errors

If a sub-agent (or your own tool call) returns a
usage-limit / rate-limit error:

1. Do **not** mark the affected slice `[!]`. Mark it `[~]`
   so the next scheduled fire resumes it.
2. Write `POST_MVP_STATUS.md` with
   `blocked_reason=usage_limit`.
3. Stop spawning new sub-agents. Exit cleanly. The next
   `/schedule` fire will resume.

### Initial action

Start by:

1. Reading the seven input docs listed above **plus**
   `doc/tasks/POST_MVP_STATUS.md` if it exists.
2. If this is a resume, print a one-line
   "Resuming from wave N" header and skip already-done
   slices.
3. Produce your execution plan as a short markdown block
   (slices grouped into parallel waves).
4. Spawn the **next wave to run** in a single message with
   multiple `Agent` tool calls.

Begin now.

---

## Reference — Project Snapshot

| Item | Value |
|---|---|
| Repo | https://github.com/ryanpumpkin/Multi-Broker-Portfolio-Tracker |
| Targets | iOS, Android, Web |
| Backend | Python + FastAPI in Docker, broker SDK adapters per request |
| Cloud | Firebase (Auth, Firestore, FCM, Crashlytics) |
| Slices remaining | 1 + 2A + 2B + 2C + 3 + 4 + 5 = **7 slices** |
| Quality bar | tests pass + lint + type-check + ≥80% coverage |
| Human in loop | none — fully autonomous |
| Already shipped | 14 modules + LongBridge end-to-end (see
                    `doc/tasks/FINAL_REPORT.md`) |

## Reference — Slice dependency graph

```
   cleanup-diagnostic-logging
         │
         ▼
   ┌─────────────────────────────────────────┐
   │  broker-integration-binance              │
   │  broker-integration-ibkr      (parallel) │
   │  broker-integration-futu                 │
   └────────────┬────────────────────────────┘
                │
                ▼
        transactions-history
                │
                ▼
        live-quote-streaming
                │
                ▼
            final-report
```

Within a wave, sub-agents run in parallel. The orchestrator
waits for the wave to complete (all gates green, all
commits landed) before starting the next.

## Reference — How to launch the autonomous run

1. **One-time setup (manual, ~30 min):**
   - Ensure broker API credentials are stored in a local
     `.env.test` (gitignored) so the env-gated integration
     tests actually run. If a broker has no test creds,
     that broker's integration test stays skipped — fine.
   - Ensure the IBKR and Futu gateway sidecars can start
     (`docker compose up ibkr-gateway futu-opend`) and that
     interactive auth has been completed for them.

2. **Kick off the orchestrator on a 5-hour schedule** using
   the built-in `schedule` skill:

   ```
   /schedule
   ```

   When prompted, configure:
   - **Cadence:** every 5 hours
   - **Prompt body:** literally the contents of the
     **Prompt** section at the top of this file
   - **Stop condition:** when `doc/POST_MVP_PLAN.md` has
     zero remaining `[ ]` or `[~]` entries

3. **Walk away.** Each scheduled fire resumes from
   `POST_MVP_PLAN.md` + `POST_MVP_STATUS.md`. Slices
   already done are skipped; in-progress ones continue;
   usage-limit interruptions self-heal on the next fire.

4. **When all slices are `[x]` or `[!]`**, the orchestrator
   appends a "Post-MVP Completion" section to
   `doc/tasks/FINAL_REPORT.md` and stops scheduling further
   fires.
