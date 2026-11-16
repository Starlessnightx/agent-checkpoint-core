# Failure analysis protocol

Start with deterministic questions: what failed, what changed, what was retried,
what succeeded later, and what verification followed the last mutation. Separate
runtime/infrastructure exceptions before mining behavioral weakness.

For every candidate root cause record: observed symptom, mechanism, event IDs,
counterevidence, alternative explanations, target Harness component, and the
cheapest action that would falsify it. Prefer one causal edit over bundles of
unrelated prompt and runtime changes.

Generic verifier rejection without supporting trajectory facts is insufficient
to select a component. Missing evidence should lead to instrumentation, not a
confident diagnosis.
