# Future Hero GIF Trace Contract

P4.1 does not build the renderer. A future demo must consume one real,
persisted EventStore/result trace showing:

```text
A VERIFIED → B fails → failure provenance → LOCAL repair → A1 → A2
→ re-verification → B VERIFIED → C unblocked
```

The renderer must display step statuses, failure type, repair scope, Attempt
IDs, repair number, replan count, duplicate side effects, and preservation of
the verified ancestor. It must read persisted EventStore and result data; it
must not use hand-authored fake state.
