# Provider, model and context settings

The /next/ workbench now has a functional settings dialog. Providers support
DeepSeek and OpenAI-compatible Chat Completions, arbitrary base URLs and managed
credentials. Models declare IDs, input/output limits, image/tool/streaming support,
reasoning parameter mapping/levels, and the output-token parameter name. Unsupported
Responses/Anthropic protocols are not shown as implemented.

Credentials are written as mode-600 files under the private data root, never returned
by APIs or embedded in configuration snapshots. Editing a credential creates a new
reference; active runs retain their original provider/model versions and references.
Validation errors omit request inputs so invalid credentials are not echoed back.
The connection button reads /models without generating model output; it verifies
connectivity, not all declared capabilities. Custom providers can run without a
DeepSeek credential. External requests use the home proxy; local endpoints bypass it.

Policies inherit system -> workspace -> session -> run model override. The selected
policy/profile/provider versions are frozen per run. Settings include context budget,
output reserve, compression trigger/target, recent message/image retention, model-call
budget (including summary calls), process timeout and tool timeout. 1K = 1024 tokens.
Unsupported image/reasoning requests are rejected before model execution. For generic
reasoning_effort providers, off omits the parameter unless an explicit off value is
configured. DeepSeek off explicitly disables thinking.

Context middleware replaces the harness default summarizer. It uses tool-safe
partitioning, writes compacted messages to LangGraph checkpoints, retains the original
message archive, and records summaries plus image references. Long summary input is
processed in bounded chunks instead of silently trimming the oldest history. Recent
image retention controls actual multimodal blocks; older attachment references and
stored images remain available. Both automatic and manual compression are supported.

Occupancy uses a conservative estimate (UTF-8 bytes/3 plus image reserves and actual
system/tool schemas), clearly separated from cumulative provider-reported usage.
Compression targets are best-effort within required recent-message/image retention;
if the resulting request still exceeds the configured budget, execution stops with
an actionable error instead of silently dropping messages. Changing a model to one
with smaller capacity requires a valid budget; the workbench selector clamps it.

## API additions

GET/POST /next/api/providers; POST /providers/{id}/test; GET/POST /models;
GET /configuration?workspace_id=... or session_id=...;
POST /settings/{global|workspace|session}/{identifier};
POST /sessions/{id}/compact; GET /sessions/{id}/context.
Messages additionally accept model_profile_id. Runs expose sanitized configuration
snapshots. Secret values/references are not returned. Provider/model IDs can be
supplied to save a new version of an existing entry. Existing runs are retained by
additive migration; nullable historical configuration means no original snapshot was
recorded. New runs always have snapshots.

## Validation, 2026-10-29

37 tests pass on home, including actual custom-provider HTTP request capture,
credential isolation/version freezing, policy inheritance, image/reasoning rejection,
model-call budget, tool-safe/image-aware compaction, automatic compression of a long
checkpoint and the previous runtime/API/container cases. Ruff and TypeScript/Vite
production builds pass. Browser QA covers provider/model creation and editing, no key
echo, scoped policy persistence, model capability switching, workspace/session refresh,
compression record expansion, and mobile settings. No runtime secrets/test data are
included in Git.

A real DeepSeek manual compression test on a separate copy of preview data completed
successfully (estimated 9,336 -> 7,940 tokens). Official provider connection discovery
also succeeded. That copied QA dataset is not deployed over live user data.

Before deployment, back up the live preview DB/checkpoints consistently, then start
this code against the existing preview data. The legacy root application/DB are not
changed. Older preview code uses positional inserts and cannot write the new run
columns: rollbacks require compatible code, or carefully coordinated restoration of
a pre-deployment backup before accepting new work. Never discard post-deployment data.

Next: workspace import/file artifacts and network/browser/MCP tools, then the contest's
multi-Agent registry, control structures, scheduling and independent verification.
Visual redesign remains deferred while functionality is built.
