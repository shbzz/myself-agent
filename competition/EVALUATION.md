# Frozen, bounded comparison protocol

This CLI creates **new private synthetic data only**. It is not a production endpoint or
an alternative way to submit single-Agent entries to the contest. No production sessions,
files, policies or credentials are imported. No provider requests happen during freeze.

## Conditions and cases

Two versioned cases in `evaluation_cases.py`: CSV order parsing + currency-aware integer-cent
summary, and timezone-aware event normalization + grouped reporting. Each has two independently
specified module contracts, starter files, module checks and a composition check. Freeze records
the complete case/evaluator contents and hashes before any benchmark request.

- `single`: exactly one bounded worker role, with delegation tools disabled. It implements both
  modules and returns a strict JSON verdict. A private fixture snapshot deliberately does not
  satisfy the public multi-Agent team minimum; it is a comparison baseline, not a product claim.
- `fixed_team`: two implementers, one module verifier and a tool-free final evidence verifier;
  one fixed pass. All produced outputs face frozen command verification.
- `custom`: identical roles, prompts, tools and task/check definitions, wrapped in a two-iteration
  feedback loop with explicit dependency-scoped repair and reuse. The same workspace write
  exclusion applies, so declared parallelism does not imply parallel writer speedup.

Same configured model (`deepseek-flash`), reasoning (`off`), context/output policy, builder tools,
initial files, local Python image digest and final evaluator for all conditions. Cloud sampling
and cache warmth are not controlled; returned usage/cache is retained, not converted into money.
There is one repeat per case/condition: six observations, **at most 12 model calls each / 72 total**,
90 seconds per worker process, no automatic retries. Verification/compaction calls consume the
same global model-call budget. The supervisor terminates a timed-out process group, cleans only
its labelled containers and runs the same final host evaluator against retained artifacts.

Generated planning is excluded from these fixed-template comparisons to avoid conflating
planner variability with scheduler/repair behavior. Its successful and failed integration
observations remain separate evidence.

## Acceptance, accounting and retained failures

Every condition receives the same fresh read-only Docker final evaluator. Its source is not an
editable workspace test file. Every command must exit zero **and reach its completion marker**;
artifact fingerprints must remain stable and no unexpected deliverable files may appear (derived
`__pycache__` files are ignored in the deliverable-name check). These finite behavioral checks are
not a formal proof against malicious, test-aware Python code.

Primary `accepted` means: no worker error, a strict true completion verdict, and all final artifact
checks pass. `artifact_checks_passed` is reported separately, including code that passes despite a
failed run/verdict. `wrong_acceptances` means pre-evaluator success claims rejected by the final
checker; it is not a claim about statistically established population error rates.

Per-call starts and provider usage are persisted, with duplicate callback IDs deduplicated. A
missing usage response is **unknown, not zero**. A failed or timed-out run stays in the denominator;
no case replacement or silent rerun. Token totals are reported only when every charged call has
observed usage. Partial provider observations and cache details remain in private raw evidence.
Actual tools, repairs, reused tasks, elapsed time, frozen hashes and command evidence are retained.

Existing unfinished slots stop execution for inspection. Observation timeout does not imply that
a worker stopped; inspect its exact process handle before taking action. Restart never replays an
unknown run. Existing terminal observations are integrity-checked and skipped, not executed again.

## Running on the designated Linux home host

Use the existing application environment and locally available pinned Python Docker image.
Credentials stay outside Git in an owner-only mode-600 config file, using the same schema as the
application. Never paste keys into commands, task prompts, reports or screenshots.

```bash
# Commit and review code first; freeze refuses a dirty checkout or an existing target directory.
PYTHONPATH=src python -m myself_agent.evaluation_cli freeze \
  --root /path/to/new-private-experiment --config /path/to/private/deepseek.env
PYTHONPATH=src python -m myself_agent.evaluation_cli run --root /path/to/new-private-experiment
PYTHONPATH=src python -m myself_agent.evaluation_cli report --root /path/to/new-private-experiment
```

The frozen protocol records source commit/file hashes, case/check hashes, model/policy identity,
image digest and actual local image ID, run order and budgets. Source/protocol/image changes refuse
execution. `report.json` explicitly says whether all six observations exist. Raw data, provider
configuration, private paths, credentials and development-agent records must not be published.
Publish only a reviewed summary with reproducible case/evaluator references.

## Evidence standards

Tests validate known-good and known-bad implementations, zero-exit-without-completion rejection,
real Docker across all conditions, true single-role execution, shared budgets, unknown usage,
source/record tamper refusal and timeout/no-replay behavior. A separately labelled deterministic
repair fixture proves that an unaffected module is reused; it is not a paid-model success result.

Do not claim statistical superiority, broad speedup or cost savings from this small exploratory
matrix. If custom orchestration is slower or fails, report it. Performance trade-offs and failures
are part of the contest evidence, not something to remove after seeing outcomes.
