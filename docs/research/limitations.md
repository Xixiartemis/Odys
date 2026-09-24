# Odys Phase 5 — Limitations

## Known Limitations

1. **Simulated execution**: Pilot runs use simulated agent execution, not real
   LLM calls. Results from the pilot validate pipeline integrity only, not
   recovery effectiveness.

2. **Sample task set**: Full ToolMaze has 2000 tasks. Pilot uses 5 tasks.
   Statistical power is insufficient for any conclusion.

3. **Single model**: Pilot uses a dry-run model. Real experiments require
   testing with production LLMs (GPT-4o, Claude, etc.).

4. **ToolMaze only**: Results may not generalize to other benchmarks
   (ToolSandbox, Terminal-Bench, TUA-Bench).

5. **Static perturbation**: ToolMaze uses pre-generated perturbation maps.
   Real-world failures may be more complex and unpredictable.

6. **No cost analysis**: Token cost and latency comparisons are not yet
   implemented.

## Threats to Validity

- **Internal**: Simulated execution may not exercise all recovery paths
- **External**: ToolMaze tasks are synthetic; real tool-use may differ
- **Construct**: TSR/PRR/RC may not capture all aspects of recovery quality
- **Conclusion**: Small sample size prevents statistical significance
