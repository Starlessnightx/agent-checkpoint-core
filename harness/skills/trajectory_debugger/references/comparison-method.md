# Comparing trajectory runs

Normalize both sides with the same index schema and comparable task population.
Inspect task coverage before interpreting aggregate deltas. Fewer failures may
reflect fewer tool calls, early termination, or missing traces rather than better
behavior.

Use task-level pass/fail flips, token/time budgets, and exact component diffs for
evaluation. A trajectory metric is an intermediate behavioral measure, never a
replacement for the external verifier or held-out validation split.
