# General Agent preview

Run `python -m myself_agent.general_web --root /absolute/path/to/preview-data --port 8790`
with the project interpreter and installed dependencies. Build the frontend first:
`cd frontend && npm ci && npm run build`. Run all application checks on home with
`competition/scripts/check-home.sh` from the repository root. Frontend dependencies
must be installed on home; no real model call is made by this check script.

The preview uses `/next/` and `/next/api/` and must be behind the same authenticated
HTTPS gateway as the existing application. Direct access binds only to loopback.
The root directory is a separate preview data directory, never the legacy job DB.

## Implemented

- Create/select real managed workspace directories, each with separate sessions.
- Arbitrary task text; Deep Agents planning, file listing/read/write/edit/search,
  and Shell execute tools use a real isolated container. No fixed task schema.
- Persistent SQLite sessions/events and LangGraph checkpoints across turns/restarts.
- Duplicate request detection; one active process at a time; stop and bounded runtime.
- A stopped tool call is marked outcome-unknown on the next turn, not replayed.
- Durable SSE events with Last-Event-ID recovery, tool output, real text file diffs,
  and plan status. No invented percentage completion for unplanned work.
- React/TypeScript/Vite workbench, Markdown, workspace/session selection, conversation
  group collapse, independently collapsible/resizable sidebars, saved widths/drafts,
  mobile drawers and reduced-motion support.

Containers have only the selected workspace bind-mounted, no model credentials,
no Docker socket, no network, a read-only root filesystem and resource/time/output
limits. Files under `/workspace` persist; `/tmp` is disposable per run. The current
image is the existing pinned Python image, so tools requiring Node/browser/network
are not yet available. Workspace import and choosing arbitrary host paths are not
part of this increment.

The default model remains the existing official DeepSeek Flash configuration.
Custom providers/model controls, user-configured context budgets,
settings, token-by-token rendering, MCP and competition team orchestration remain
subsequent work in REBUILD_PLAN.md. Plain messages and tool events stream as complete
records; this increment does not claim token-level streaming or full crash-resume.

## API

All paths below are under `/next/api`:

- GET/POST `/workspaces`; GET `/sessions?workspace_id=...`; POST `/sessions`.
- GET `/sessions/{id}` returns session, ordered messages and runs.
- POST `/sessions/{id}/messages` accepts request_id (32 hex) and arbitrary text.
- POST `/runs/{id}/stop` with an empty JSON object.
- GET `/sessions/{id}/events?after=N` streams durable ordered events, also honoring
  the Last-Event-ID header. Reading/reconnecting never submits a new run.

Terminal run states mean execution ended, failed, cancelled, or interrupted. A
`succeeded` run means the Agent loop completed; it is not an independent verdict
that the requested software is correct. Tool output provides the validation evidence.

## Verification on home, 2026-10-29

31 automated cases passed, including the previous 24 plus workspace/idempotency,
real Docker tool failure/repair/follow-up memory, container boundary/timeout,
supervisor cancellation/restart, and API origin/body checks. A scripted offline
model drives real Deep Agents/tools in the repair regression; it is not a measured
natural model repair rate. Ruff and the React TypeScript/production build passed.

Browser QA covered 35 layout states at 1920/1440/1280/1100/1024/760/390 widths,
plus drag persistence, reload, live task history and group collapse, with no page
errors. Browser QA performs no real model submissions.

A separate real DeepSeek task generated a duration parser and 12 unittest cases
using ls/write_file/execute (4 tool calls). The platform independently reran the
12 tests inside a fresh container and confirmed exit code 0. Usage was 14,540 input
and 1,756 output tokens (16,296 total, including 10,880 cached input). This is one
smoke-test task, not broad success-rate evidence. Generated workspace data and
credentials are excluded from Git.


## Conversation activity, images and reasoning

The conversation itself displays compact expandable tool activities in execution
order, with structured arguments/commands, actual output, failure state and elapsed
time. Actual provider reasoning is collapsed by default. The visual direction uses
a quiet dark DSH-like layout with original branding and activity components.

Thinking can be off/low/high/max per run (default low). The adapter preserves
reasoning_content in subsequent assistant/tool rounds. Image attachments persist
per session, appear in user messages and use genuine multimodal image_url blocks.
Upload, paste, drag/drop, preview and removal are supported. Up to four 6 MiB
PNG/JPEG/GIF/WebP images per message; uploads have a separate 9 MiB gateway limit.
The existing gateway credentials protect preview and attachment routes.

API additions: POST /sessions/{id}/attachments with filename and data_base64;
GET /attachments/{id}. Messages accept attachment_ids and reasoning_effort.
An additive migration defaults existing runs to no attachments and thinking off.
A consistent preview DB backup was taken first; the legacy DB remains separate.
Attachment/checkpoint/model data stay outside Git. Do not delete data on rollback.

A real vision+thinking task identified ORBIT-6427, a red rectangle and a blue circle,
wrote visual_note.md, and verified it with Shell. The model returned reasoning and
continued across tool turns. It recovered from a real read-before-write race; this
is a smoke-test observation, not a measured repair rate. Browser QA confirmed
35 layout states, inline command/output/time, default-collapsed reasoning, and
image preview/upload/paste without page errors.

## Model and context increment

Provider/model/context settings, version snapshots and real automatic/manual compression are implemented; see MODEL_SETTINGS.md. Older future-scope statements above describe the earlier increment.
