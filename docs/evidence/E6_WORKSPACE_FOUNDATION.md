# E6-A Durable Workspace Foundation

## Scope

E6-A makes the E4 staged workspace truthful across object destruction and a
later process reopen. It does not implement `resume_run()`, automatic run
discovery, provider conversation continuation, dynamic replan, or any live
model behavior. The later E6-B process-resume integration is recorded separately
under `E6_PROCESS_RESUME.md` and uses Harness `HV-1.1`; this historical E6-A
artifact remains labeled with the version under which it was produced.

## Design decisions

- A durable session owns `manifest.json`, `baseline/`, and `work/` outside the
  agent-visible workspace root.
- `baseline/` is the immutable reference snapshot for diff semantics;
  reconstructing a baseline from mutable `work/` after restart would erase
  pre-crash edits.
- The manifest is strict, versioned, atomically published, and contains only
  canonical paths, tree hashes, limits, identity, and lifecycle metadata.
- `StagedWorkspace` keeps its existing ephemeral mode and accepts an explicit
  `baseline_root` for durable reopen mode. Workspace tools continue to receive
  only `work/`.
- Reopen validates baseline integrity and source identity before exposing the
  mutable work tree. It rejects baseline corruption and source drift instead
  of silently rebasing.
- Tree hashes are deterministic over sorted relative paths and file-content
  hashes; mtime, absolute temporary paths, and enumeration order do not affect
  them.

## Trade-offs

- Baseline and work are duplicated on disk to make recovery evidence explicit;
  this costs storage but avoids ambiguous copy-on-write semantics.
- Baseline immutability is enforced by ownership and reopen hash validation,
  rather than relying on platform-specific read-only filesystem flags.
- Source verification hashes the policy-visible tree and therefore follows the
  existing excluded-directory policy (including `.pytest_cache`).
- Session creation uses the existing bounded, symlink-safe staging copy logic,
  then atomically publishes the completed session directory.

## Failed approaches avoided

- Reopening `StagedWorkspace` directly from `work/` would treat edited files as
  the new baseline and make `diff()` falsely empty.
- Silently copying a changed source into an existing session would rebase a
  candidate patch and destroy deterministic resume semantics.
- Persisting file contents, tool output, stdout/stderr, credentials, or model
  reasoning inside JSON would make the manifest an unsafe data channel.

## Known limitations

- E6-A alone does not resume an outer run; E6-B adds that path without claiming
  provider-internal conversation restoration.
- No process restart discovery, side-effect replay, or provider-internal
  conversation restoration is implemented.
- A caller that manually mutates `baseline/` is detected on reopen; the
  baseline is not repaired automatically.

## Next hypothesis

E6-B can resume an outer run by reopening a validated session, reconstructing
checkpoint state, and independently validating candidate artifacts before any
retry. It must not claim to restore the exact hidden LLM turn.
