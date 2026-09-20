# Trace-derived reliability demos

`generate_gifs.py` renders the recorded event order from one benchmark
execution trace. It does not create a scenario timeline or fabricate events.
Each frame is the cumulative view after one JSONL event, including the event
label, task/step/attempt identity, and counts derived from events seen so far.

Run it from the repository root:

```text
python evals/reliability/demo_gif/generate_gifs.py \
  --trace results/traces/example.jsonl
```

The trace records must contain:

```text
timestamp, event_type, task_id, step_id, attempt_id, status, metadata
```

The three generated files are written to
`evals/reliability/demo_gif/` by default. Unknown event types are rendered as
forward-compatible labels; missing events required by a story fail closed
with `MISSING_REQUIRED_EVENT`.
