# Odys CLI Alpha — HV-1.4

Product promise: point Odys at a local software repository, state a meaningful
engineering goal, observe durable work, validate the staged result, inspect the
run, and resume after process interruption.

Tagline: **Plan. Act. Recover. Finish.**

Base main SHA: `b9d289e7e7699ad65e71b671f1a01fe5900d97c1`

## User commands

The primary executable is `odys`; the historical `lhas` executable remains an
alias to the same Typer application.

- `odys run "<goal>" --repo <path> --verify "<command>"`
- `odys resume <run_id>`
- `odys inspect <run_id> [--events] [--json]`
- `odys runs [--limit N] [--status STATUS]`
- `odys version`

`odys run` validates the source path, verification argv, and provider
configuration before creating a Project or Task. It creates or reuses a
repository-identity Project, creates the Task, binds a durable workspace,
constructs the real recovery orchestrator, and keeps the source tree immutable.

## Runtime assembly

The CLI is an application adapter. It reuses:

- `RecoveringOrchestrator` for Attempt, validation, classification, recovery,
  checkpoint, arbitration, and terminal semantics;
- `RunWorkspaceManager` for run-bound durable baseline/work sessions;
- `InnerAgentExecutor` and `OpenAIAgentsBackend` for configured live providers;
- `ToolRegistry` and staged workspace tools;
- `ContextBuilder` and checkpoint reconstruction with CP-3;
- `EventStore` and persistence repositories;
- existing `FailureClassifier` and `RecoveryPolicy`.

`odys resume` reconstructs this assembly from the persisted Task configuration
and Run metadata. It resolves the existing workspace binding and invokes
`RecoveringOrchestrator.resume_run`; it does not implement a second recovery
engine or create a replacement workspace.

## Explicit-command validation

The Alpha verifier parses the user command into argv and executes it with
`asyncio.create_subprocess_exec`. It never uses `shell=True`. The exact argv is
also the Inner Agent's only allowed command rule; extra arguments are not
silently granted.

Validation runs in the durable staged workspace and passes only when the
configured process exits zero. Persisted evidence is bounded:

- exit code;
- timeout flag;
- duration;
- stdout/stderr truncation flags;
- bounded stdout/stderr.

SafeCli removes secret environment variables and explicitly prefers the active
virtual environment's executable directory. Command-not-allowed failures show
a bounded list of configured prefixes.

## Terminal UI and interruption

Rich Live renders at no more than four updates per second from repositories and
safe EventStore projections. It shows product/harness/provider identity, goal,
Run/Task state, Attempt counters, tool counters, workspace integrity,
validation, recovery, checkpoint/CP-3, and the eight most recent safe events.
Raw tool arguments, model transcript, hidden reasoning, credentials, and
unrestricted environment values are excluded.

`--no-ui` and non-TTY stdout use the same runtime path with deterministic plain
event output. Rich initialization or rendering failure falls back to plain
output without changing task correctness.

Ctrl+C leaves Run and workspace state durable and prints the known Run ID plus:

```text
odys resume <run_id>
```

The next process uses the existing E6 resume state machine.

## Tool-aware recovery

Compatibility `workspace.edit` remains unchanged. HV-1.4 adds
`workspace.edit_lines`:

- workspace-relative path;
- 1-based inclusive start/end lines;
- required current file SHA-256;
- `STALE_FILE_VERSION` on version drift;
- range, binary, file, path, and symlink protections;
- atomic replacement;
- preservation of LF/CRLF and final-newline semantics;
- bounded mutation metadata only.

Safe recovery metadata gives the agent an actionable next step:

- exact target missing → `REREAD_THEN_LINE_EDIT`;
- ambiguous target → `NARROW_TARGET_OR_LINE_EDIT`;
- stale SHA → `REFRESH_FILE_VERSION`;
- disallowed command → configured allowed prefixes;
- escaped cwd/path → `USE_WORKSPACE_RELATIVE_CWD`.

Within one Inner Agent invocation, a repeated failure is keyed by capability,
SHA-256 of normalized arguments, and error type. The second identical failure
returns its repeat count and `strategy_change_required=true`. Raw arguments are
not added to the trace or checkpoint.

## CP-3 failure memory

Bounded WorkingState now carries:

- `tool_failure_count`;
- counts by capability and error type;
- last failure capability and type;
- `strategy_change_required`.

Dictionaries are capped at 32 keys and string keys are bounded. The event
allowlist remains the only history projection; unknown payload fields do not
enter CP-3. Tests verify that previous edit-failure counts reach the
executor-facing context while raw arguments do not.

## Offline deterministic product evidence

The `offline` provider is a network-free product demo/test adapter. It still
uses the real CLI application, durable workspace, ToolRegistry,
InnerAgentExecutor, RecoveringOrchestrator, Outer Validator, EventStore, and
persistence path. It is not a canonical benchmark and makes no real-model
performance claim.

Measured local acceptance:

| Metric | Value |
|---|---:|
| Repository tests | 321 passed |
| New tests from 287-test baseline | 34 |
| Primary CLI commands covered | 5 / 5 |
| `workspace.edit_lines` safety cases | 9 |
| CP-3 failure-memory focused cases | 2 |
| Deterministic end-to-end CLI scenarios | 2 |
| Live providers executed | 0 |

The end-to-end scenarios are:

1. fresh offline `run` → two staged file edits → pytest PASS → inspect/runs and
   terminal resume;
2. simulated process interruption with a durable RUNNING Attempt → fresh CLI
   resume → same session/root → Attempt 1 becomes
   `CRASHED/PROCESS_INTERRUPTED` → CP-3 Attempt 2 → pytest PASS.

The demo source tree remained byte-identical in both scenarios.

## Demo

PowerShell:

```powershell
uv run odys run `
  "Repair tenant-scoped session isolation, prevent delayed messages from recreating deleted sessions, and preserve active/new session behavior." `
  --repo evals/fixtures/hv12_session_lifecycle `
  --verify "pytest -q" `
  --provider offline `
  --no-ui `
  --yes `
  --db .odys-demo/odys.db
```

Then use the printed Run ID:

```powershell
uv run odys inspect <run_id> --events --db .odys-demo/odys.db
uv run odys runs --limit 10 --db .odys-demo/odys.db
uv run odys resume <run_id> --no-ui --db .odys-demo/odys.db
```

For a configured real provider, omit `--provider offline` and set
`ODYS_AGENT_MODEL` plus `ODYS_AGENT_API_KEY` (and provider-specific base URL or
API mode when required). No live provider was executed during this phase.

## Boundaries

Ordinary CLI runs are rerunnable product demos. They do not create `HV14-LIVE`
artifacts or one-shot claim markers. Dynamic Macro Replan, TaskGraph mutation,
parallel agents, browser/server UI, remote execution, automatic commits/PRs,
arbitrary shell, long-term/vector memory, and expanded external side effects
remain out of scope.
