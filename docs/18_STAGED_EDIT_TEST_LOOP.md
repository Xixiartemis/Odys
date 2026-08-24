# E4 Staged Edit/Test Loop

E4 creates a temporary `StagedWorkspace` copy from a source repository. Reads, searches, tests, exact text edits, diff, and restore operate on the staging root; the source repository is never a mutation target. Symlinks are skipped while copying, and staging file/count/byte limits prevent unbounded copies.

`workspace.edit` only performs one exact replacement in an existing UTF-8 text file. It requires a non-empty `old_text`, rejects zero or multiple matches, and can enforce an optimistic `expected_sha256` guard. Writes use a flushed temporary sibling followed by atomic replace. `workspace.restore` restores the captured baseline, while `workspace.diff` emits bounded unified diff and change counters.

Mutation exposure is explicit: a capability with `side_effect=True` is hidden unless it appears in both `allowed_capabilities` and `allowed_side_effect_capabilities`. Human-approval capabilities remain hidden even when explicitly listed. E2/E3 defaults therefore remain deny-by-default.

Safe CLI continues to run only policy-approved commands in the staged root. It is not a filesystem sandbox: an explicitly allowed subprocess still has host OS permissions and may have side effects. E4 provides no source apply, file create/delete/rename, git mutation, or human-gate integration. The deliverable is a validated candidate ChangeSet artifact for a later approval phase.
