# Versioned knowledge and reviewed experience

Backend-only increment. Sources and experience are explicitly scoped to a workspace,
selected per Agent, frozen with the team, and retrieved into the existing planning /
execution prompts. No frontend, new model provider, vector service or unrestricted
network/file importer is introduced.

## Data flow and trust boundaries

1. An authenticated operator explicitly submits text and a provenance label to
   `POST /next/api/knowledge/sources`. `reference` is a label, not a fetched URL.
2. An Agent definition opts in with `knowledge_ids` and/or `memory_ids` (up to eight
   of each, no duplicates). The default is no access. References resolve only in
   the submitted session's workspace; disabled Agents receive no grant. There is
   no global-library fallback or implicit memory collection.
3. Team preparation freezes the selected revisions, content/chunk hashes, per-Agent
   grants and a snapshot digest in one knowledge read transaction. A source is
   stored once in the shared team snapshot, not once per role. Credential/config
   metadata is never copied into knowledge prompts.
4. A tool-free planner receives only its own retrieved passages. Each executor and
   verifier separately receives only its own passages. Retrieval uses the first
   2,000 query characters (executor query: first 1,000 node-prompt characters plus
   first 999 user-task characters), at most 128 query terms, six complete hits and
   12,000 JSON characters. Results disclose their query, limits, truncation and
   snapshot digest. `knowledge.retrieved` events retain the exact supplied context
   with Agent/task attribution, including empty results.
5. Existing plan digests include the frozen knowledge. Selected source/memory
   revisions changing before approval invalidate approval; a corrected but not yet
   reapproved experience fails closed. Approved executions keep the exact snapshot,
   including on idempotent resubmission. No moving-target lookup happens in a worker.
6. Retrieved text is untrusted reference data, not instructions or permissions.
   Existing tool policies, sandbox, budgets and frozen independent acceptance stay
   authoritative. Experience cannot mark an output verified or lower acceptance.

This is workspace/Agent isolation within the existing authenticated single-owner
workbench, not a new multi-user/tenant authorization system. The owner can inspect
all its workspace/run audit records. Withdrawal affects future snapshots and pending
approvals, not already frozen executions or historical evidence. Cancel an active
run explicitly if its references must no longer be used.

## Retrieval and citation semantics

`lexical-v1` is a deterministic bounded lexical retriever: Unicode normalization,
case-folded word matches and Chinese bigrams. Body-term matches score one, title-term
matches score two; ties sort by citation. It has no embedding API cost. It does not
claim semantic/vector search or measured recall superiority. Fixed 1,200-character
chunks have exact normalized-text start/end offsets and SHA256 hashes. Splitting
can miss a phrase crossing a chunk boundary; use this as a transparent initial
retrieval baseline rather than claiming comprehensive recall.

Source citation: `K:manual@1#c0001`. Resolve it with
`GET /next/api/knowledge/sources/manual?workspace_id=<id>&version=1`.
The response includes original normalized text, source hash and complete chunks.
Memory citation: `M:<id>@2`; resolve through the corresponding `memories` route.
Each hit includes immutable revision/content provenance. Models are instructed to
retain these identifiers; the platform records what was supplied but does **not**
assert that every generated citation/claim is correct or actually relied upon.

## Source API

```json
{
  "workspace_id": "<32-hex workspace ID>",
  "id": "manual",
  "expected_version": 0,
  "title": "Normalization requirements",
  "text": "Normalize tags by trimming, case-folding, deduplicating and sorting.",
  "reference": "User-supplied requirements, section 2",
  "active": true
}
```

`expected_version=0` creates a source; later writes require the current revision.
Stale concurrent writes return 409 without a partial revision. CRLF/CR is normalized
to LF before hashing/chunking. Whitespace-only, NUL and invalid-Unicode content is
rejected. Maximum source text: 24,000 characters. The JSON request cap is 192 KiB.

The same endpoint with `active=false` and the current `expected_version` withdraws
an existing source by appending a compact tombstone. Supplied title/text/reference
are not new content in that case: the tombstone refers to the last active revision.
Old content remains available by explicit version for audit. Repeated withdrawal
is rejected; ordinary reactivation must supply full content and fit normal quotas.

- `GET /next/api/knowledge/sources?workspace_id=<id>`: latest metadata, not full texts.
- `GET /next/api/knowledge/sources/<id>/versions?workspace_id=<id>`: revision metadata.
- `POST /next/api/knowledge/retrieve`: `{session_id, agent_id, query}` previews exactly
  that Agent's current authorized retrieval; it does not execute a model or tools.

All endpoints reuse the existing gateway authentication, same-origin mutation,
trusted-host, no-store and non-echo validation boundaries.

## Experience lifecycle, not automatic belief accumulation

`POST /next/api/knowledge/memories` takes `{workspace_id, run_id,
verification_path, statement}`. The run must be a completed successful orchestration
in the same workspace; the specified verification node must have independently
covered producer output, passed its frozen command evidence, and retained stable
artifact fingerprints. A model's `passed=true` alone, failed checks, self-verification,
uncovered outputs, failed/rejected parent or another workspace's run is insufficient.

The host constructs the evidence receipt; callers cannot supply one. It binds run,
verification path/Agent, artifact hash, command evidence hash/count, verified producer
paths hash and original execution snapshot hash. The original run retains full
commands and evidence. The new memory is **candidate**, never retrieved automatically.

`POST /next/api/knowledge/memories/<id>/review` takes:

```json
{
  "workspace_id": "<32-hex workspace ID>",
  "expected_version": 1,
  "action": "approve",
  "reason": "Reviewed the claim against the referenced evidence"
}
```

- `approve`: candidate -> approved; still requires explicit per-Agent opt-in.
- `correct`: requires a replacement `statement`; appends a candidate revision and
  requires fresh approval. It keeps the original evidence link, not a fabricated
  new proof. A claim that needs different evidence needs a new proposal.
- `withdraw`: excludes it from future use, retaining history.

The statement is operator-authored and human-reviewed; linked successful execution
is **not proof that every generalization in the statement is true**. There is no
self-learning capability score, automatic promotion, hidden feedback fine-tuning or
cross-workspace sharing. This increment exposes no knowledge-management tools to models.

## Bounds and durability

One additive `knowledge_records` table in the existing SQLite database; no external
index or new dependency. Maximum 64 source IDs and 64 memory IDs per workspace,
64 ordinary revisions each, and 8 MiB total ordinary stored history per workspace
(including duplicated persisted source/chunk text). Append-only revisions and
compare-and-swap writes are transactional. A compact final withdrawal remains
possible when the ordinary byte/version quota is reached; repeating that withdrawal
cannot append unlimited data, and quota-blocked records cannot be reactivated.
Physical deletion/history compaction and a vector index are deliberately not added.

Regression coverage includes revision persistence, concurrent edits, atomic rollback,
quotas and quota-safe withdrawal, CJK retrieval, scope denial, stale plan approvals,
correction/reapproval, API boundaries, exact runtime contexts, and real Docker
acceptance of correct output/rejection of a model-approved wrong output. Tests use
isolated data, never production seeds. Live model observations are reported separately
from deterministic fixtures and do not establish an efficiency or success-rate gain.

## Live observation for this increment (2026-10-31)

One isolated knowledge-aware planning attempt was rejected with `ValidationError`
before execution. The original failure path did not retain the raw proposal or
provider usage, so neither the exact schema cause nor its token total can honestly
be reconstructed. It was not retried. Failure-path accounting now persists observed
usage (with explicit provider-usage availability), even for malformed/truncated or
late-cancelled results; schema diagnostics record error types without echoing values.
Tests cover these paths. Hard process termination can still prevent final accounting.

A separate executor-only integration fixture used the remaining original execution
allowance, not a replacement generated plan. Its explicit code-defined workflow
succeeded: 5 model calls, 10.117 seconds, 8,815 tokens (4,736 cached input). Source
revision citations reached the authorized builder and were preserved in its handoff;
the ungranted verifier received no source context and frozen Docker acceptance passed.
The explicit memory-review API lifecycle was exercised by the test script, not by an
actual user reviewer. Both outcomes are retained separately. This is **not** a claim
of live automatic-planning success, success-rate gain, or a fair speed comparison.
