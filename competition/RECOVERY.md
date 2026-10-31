# Human verification, safe continuation and artifact rollback

This increment is backend-first. It is not a visual workflow editor or universal crash replay.
Only `competition/` changes; upstream LangGraph libraries remain unchanged.

## Human gate and explicit decision

A workflow may include a top-level node:

```json
{"id":"review_by_operator","kind":"human","source":"verify_output","depends_on":["verify_output"]}
```

The source must be an independent `verify` node in the same flow. Nested human gates
are rejected rather than attempting to serialize an in-flight recursive stack.
A ready gate drains already scheduled work before pausing; no model/container/tool
operation remains in flight. The source must have successful frozen command evidence
and unchanged artifact fingerprints. A failed independent check cannot become success
through a human decision. A reject decision terminates the run without downstream work.

`PlanInput.require_human_review=true` appends a host-owned `human_gate` after the
host-owned `acceptance_gate`. Both are included in the plan approval digest. The default
is false for backwards compatibility. Planner-generated fields remain schema-validated;
invalid plans never execute. The planner receives no management tools or credentials.

The run enters `waiting_human`; the worker exits and consumes no model calls while
waiting. The checkpoint records the frozen definition digest, exact verification result,
all completed top-level results, full workspace content fingerprint (including `.git`),
step and scheduler-assignment counters, model calls, web requests, MCP calls, usage and
active execution time. Waiting time is excluded from active execution timeout. A waiting
run reserves its workspace against other platform runs; other idle workspaces remain usable.

Endpoints under `/next/api/orchestration`:

- `GET /runs/{run_id}/checkpoints`: immutable checkpoint contents and decision history.
- `POST /runs/{run_id}/decisions`: `request_id`, `checkpoint_id`, exact `digest`,
  `decision` (`approve` or `reject`) and a required bounded `note`.
- Existing `POST /runs/{run_id}/cancel` also cancels waiting runs.

Decisions are transactional and bound to the checkpoint. Same request/content is
idempotent; a reused ID with different content is rejected. The actor is assigned by the
server as `authenticated-shared-operator`, reflecting the existing shared authentication
boundary; this is **not named multi-user identity or RBAC**. The API inherits same-origin,
body-size and authentication/proxy boundaries. Model tools cannot call management APIs.

Approval checks current artifacts and queues the same run. The worker checks artifacts
and frozen definition again, consumes the checkpoint once and resumes after the gate.
Completed work is not replayed; counters and usage are not reset. Cancellation wins over
late decisions. Waiting checkpoints survive service restart. Queued/running crashes,
unknown external write outcomes and already-claimed continuations remain interrupted:
there is no implicit retry and no arbitrary resume of failed tool calls.

## Bounded artifact snapshots and rollback

These operations are explicit and only available for idle workspaces. They do not snapshot
or restore the application database, configuration, credentials or other workspaces.
Snapshots live outside model-visible workspace mounts in the private data directory.

- `GET /workspaces/{workspace_id}/artifacts`: current fingerprint, snapshots, rollback log.
- `POST /workspaces/{workspace_id}/snapshots`: unique `request_id`, required `note`.
- `POST /workspaces/{workspace_id}/rollbacks`: unique `request_id`, `snapshot_id`,
  `expected_current_sha256`, `confirm: "restore"`, required `note`.
- `POST /workspaces/{workspace_id}/rollbacks/{request_id}/reconcile`: observe an interrupted
  exchange; never repeat it. Send an empty JSON body through the same-origin API.

Snapshot inventories include regular files, directories, permission bits and `.git`.
Symlinks, hard-linked files and special files are refused. Limits are 1,000 entries,
16 MiB/file, 32 MiB total, 15 seconds per bounded inventory and 32 private snapshot/staging
directories per data root. The prototype does not implement automatic snapshot deletion;
quota exhaustion fails closed and must not silently erase old artifacts.

After staging and integrity checks, a durable `prepared` record fences platform writers.
Linux `renameat2(RENAME_EXCHANGE)` atomically exchanges the workspace and the staged copy;
there is no unsafe two-rename fallback. The previous workspace is retained as an addressable
snapshot. The code fsyncs files/directories; a private operation lock prevents concurrent
reconciliation from releasing the fence before an active exchange is done.

If the process fails, either directory may have been exchanged: `prepared`/`uncertain`
records continue fencing new writes across restart. Explicit reconciliation compares both
fingerprints and marks `completed`, `not_applied` or `uncertain`, without replay or deleting
either copy. Unexpected host-side edits remain uncertain and require operator inspection.
This is not protection against an administrator actively mutating files outside platform
coordination; intervening content is retained, not silently destroyed. Code-deployment
rollback must likewise never overwrite the production database.

## Evidence and limitations

Regression tests exercise quiescent continuation, no replay, cumulative budgets, approval
staleness, cancellation, actor spoofing refusal, real Docker check failure, atomic exchange,
link refusal, retained displaced files, same-origin checks and interrupted-operation fences.
Fixtures are explicitly labelled; they are not paid-model success-rate or speedup evidence.

The earlier knowledge-aware planner ValidationError remains historical: its original raw
response/precise cause was not retained. This increment adds redacted schema locations and
malformed-JSON diagnostics without retaining model-generated values. A new observation can
show current behavior but cannot retroactively establish that historical cause.

Still required after this backend increment: frozen controlled comparisons, presentation
UI, reproducible main scenario and submission PDF/PPT/video. Do not claim research novelty,
industry-first status, semantic vector retrieval or automatic memory promotion.
