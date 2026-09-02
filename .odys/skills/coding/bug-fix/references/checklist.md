# Repair checklist

1. Preserve an observable failing case.
2. Identify the narrowest causal boundary.
3. Apply one version-checked change.
4. Run focused validation, then the full requested validator.
5. Report evidence without exposing raw tool arguments or secrets.
