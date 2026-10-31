# Development handoff - 2026-10-29

The current baseline is the 0.3 demo: three constrained software tasks, chat
history, task tracking, and Docker acceptance tests. It does not yet implement
the general-purpose Agent requested by the owner.

Read AGENTS.md and CONTRIBUTING.md. Build, install, test, call models and run on
the designated home server. Configure `git config --local core.hooksPath .githooks`
in each checkout. Work on codex/<topic> branches and open PRs against main.
Run `competition/scripts/check-home.sh` before pushing. Review and verify before
merging. Merging does not automatically deploy. Local hooks can be bypassed;
GitHub branch protection requires separate repository administration permission.

The full owner-approved specification is REBUILD_PLAN.md in the local project
folder. Scope: arbitrary tasks and real tools; workspaces and sessions; custom
providers/models and thinking/image capabilities; image input; context budgets
and compaction; resizable/collapsible sidebars; settings and polished interactions;
configurable multi-Agent orchestration and evaluation.

Next: implement persistent generic sessions and real file/search/edit/Shell tools,
stream execution events to chat and tracker, and demonstrate an arbitrary software
task through test execution and repair. Reuse Deep Agents and LangGraph. Keep the
fixed scenarios as evaluation fixtures, not product boundaries. Do not change
upstream libs as part of application work.

This commit does not deploy or migrate data. Deployments must identify a tested
commit and preserve a consistent database backup and rollback procedure. Keep
credentials, host-specific deployment scripts, environment files, databases and
user data outside version control.

Baseline checks: Ruff lint/format, 24 pytest cases and both existing JavaScript
syntax checks. The sidebar CSS fix still needs browser regression evidence.
No real model call is needed for baseline tests.

## General Agent preview increment

See GENERAL_AGENT.md for the implemented generic session/tool loop, current limits, API and verification. The existing constrained app remains separate. Custom model/image/context settings and competition orchestration still require implementation.


## Workspace import and file delivery - 2026-10-30

Model/context settings have landed in main (#3). The general workbench now adds ZIP
project import into a new workspace, directory browsing, UTF-8 file preview,
individual downloads and workspace ZIP export. See WORKSPACE_FILES.md for API,
bounds and import/export behavior. Existing sessions and data remain in place.
No data schema migration is required for this increment.

Validation includes 57 pytest cases, Ruff, TypeScript/Vite and real Docker
execution on an imported project. Browser evidence is kept outside the repository.
This increment does not call a paid model. Continue with external research/browser/
MCP tools, then Agent registration, configurable workflows, dynamic scheduling,
independent verification/repair and competition evidence. The owner's frontend
visual redesign remains deferred until these functions are complete.


## Controlled external tools - 2026-10-30

Workspace import/file delivery has landed in main (#4). The next increment adds
explicit system/workspace/session policies for HTTPS page reads, sandboxed read-only
Chromium and whitelisted MCP Streamable HTTP tools. Browser screenshots appear in
conversation. Profiles, secrets and enabled tools are frozen per run; old snapshots
remain stable. See EXTERNAL_TOOLS.md for API, limits and unsupported modes.

98 pytest cases and required Ruff/TypeScript/Vite checks pass. Real authenticated MCP,
Chromium and Docker are exercised with a deterministic model in regression tests.
A separate bounded DeepSeek Flash task succeeded against an actual public page,
Chromium screenshot, controlled local MCP server and container verification; actual
usage is 20383 tokens (including 15360 cached input). Browser tests verify configuration,
secret non-echo, prior-run snapshots, inline activity and desktop/mobile geometry.
Clearing an enabled MCP allowlist denies all calls without locking configuration;
regression checks cover all three policy scopes, UI deselection and reload recovery.
Host evidence remains outside Git. Do not deploy fixture profiles or test data.

Next: Agent registration/capabilities, configurable workflow/control structures,
dynamic planning/scheduling, independent verification/repair, recovery and competition
baselines. Do not spend the next round on full browser automation or visual redesign.


## Backend orchestration increment - 2026-10-30

External tools have landed in main (#5). The next increment adds versioned Agent/team
and workflow definitions, a bounded declarative scheduler/interpreter for sequential,
parallel, conditional, loop and nested flows, independent read-only command evidence,
artifact fingerprints and dependency-scoped feedback repair. All activated producer
outputs must be covered by successful verification. See ORCHESTRATION.md and the
validated examples/orchestration.json. No frontend source changes are included.

Required home checks pass: 172 pytest cases, Ruff/format, static JS and TypeScript/Vite.
The pre-existing LangGraph durability warning remains. Frontend source and asset hashes
are unchanged. Enabled MCP server IDs/names are handed only to roles with effective
MCP tools, without endpoint or credential metadata; read-only business-call denial
and three authorized combinations are covered by deterministic Docker regressions.

Durable APIs, immutable per-run snapshots, shared model/external-operation budgets,
workspace write leases, cancellation, restart interruption and labelled-container
cleanup are covered by offline tests and real Docker. A bounded real DeepSeek Flash
run succeeded through API and supervised worker, rejected a known-bad baseline,
selected another eligible builder after feedback and independently verified repair.
Actual: 13 model calls, 20.399 seconds, 25648 tokens (15488 cached input). This is one
observed run, not a measured success rate or performance improvement.

A separately bounded WorkBuddy DeepSeek V4.1 Flash/max development worker supplied
25 contract tests, reviewed and verified before integration. Its usage is recorded
separately, not represented as platform runtime evidence. Host data, credentials,
paid-run records and deployment scripts stay outside Git. Do not deploy test profiles.

Next: controlled evaluation baselines, knowledge citations, human intervention and
safe recovery, then frontend orchestration/monitoring. Do not claim distributed
queues, cross-run replay, vector memory or visual workflow editing are implemented.

### Terminal consistency follow-up - 2026-10-31

A resumed full regression exposed a supervisor race: parent failure was visible
before its orchestration result. Supervisor stop transitions now commit parent,
active tasks and result atomically, and public details use one read snapshot.
Regression tests prove rollback on an injected SQLite failure, cancellation fences,
concurrent read consistency and preservation of completed rejection evidence.
The full 172-test home gate passes; no frontend changes or extra paid product run.
