# Foreground command contract

`run_command` accepts required `command`, optional workspace-relative `cwd`, and
`timeout_seconds` between 0.1 and 600. It executes through the platform shell in a
new process group so a timeout can terminate descendants rather than leaving
orphan processes.

The result preserves summary, stdout, stderr, exit_code, artifacts, failure_kind,
and retryable. `ok` is true only for exit code zero. Empty stdout is a valid result.

Shell composition changes semantics. When using pipes, redirects, or logical
operators, make the expected exit-status behavior explicit. Do not interpolate
untrusted discovered text into a command. Prefer structured filesystem tools for
reading, writing, search, and patch operations.

Timeout kills the process group and returns classified evidence. It does not
automatically retry or increase the budget.
