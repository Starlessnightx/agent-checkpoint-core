---
name: trajectory-debugger
description: Index, query, compare, and diagnose Harness trajectories using source-linked deterministic evidence. Use whenever an agent rollout fails, stalls, repeats actions, finishes without verification, loses context, misuses tools, or changes behavior between baseline and candidate runs.
---

# Trajectory Debugger

Reduce raw traces to source-linked evidence before proposing a Harness change.
Keep observed facts separate from causal hypotheses.

## Workflow

1. Build a bounded index with `build_trajectory_index` from the relevant JSON or
   JSONL trace sources.
2. Query the index by task, text, event type, Tool, success state, or failure kind.
3. Generate a report with `report_failure_episode` for the selected task.
4. Open raw source records for every important event ID, especially when output is
   truncated or several mechanisms fit the symptom.
5. Use each candidate hypothesis's falsification action to gather discriminating
   evidence.
6. Compare matched baseline and candidate indexes with
   `compare_trajectory_runs`; treat metric deltas as descriptive until a focused
   rerun or ablation supports causality.

Separate Harness behavior from product, dependency, environment, evaluator, and
task failures.

## Examples

### Diagnose repeated failures

1. Index the failed rollout.
2. Query `ok=false` and the failing Tool name.
3. Generate a report and inspect repeated argument fingerprints.
4. Check whether observations, arguments, or workspace state changed between
   attempts.
5. Test a materially different recovery hypothesis.

### Build an offline index

```json
{"name":"trajectory-debugger","script":"scripts/trajectory_debugger.py","arguments":["index","runs/**/*.json","--output","debug-index.json"]}
```

The same script exposes `query`, `report`, and `compare` for repeatable analysis.

## Guidelines

- Cite both `event_id` and `source` for important claims.
- Preserve raw traces; indexes and reports are derived artifacts.
- Do not infer omitted content from a bounded output preview.
- Do not label a plausible explanation as an observed root cause.
- Require the exact Harness diff, a predeclared prediction, matched tasks, and
  validation evidence before causal attribution.

## Resources

- Read [index schema](references/index-schema.md) before consuming index fields.
- Read [failure analysis](references/failure-analysis.md) before accepting a
  diagnosis.
- Read [comparison method](references/comparison-method.md) before interpreting
  run deltas.
