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
