---
name: shell
description: Run bounded foreground commands and manage background process lifecycles with structured failure evidence. Use whenever a task requires shell execution, builds, local diagnostics, servers, daemons, watchers, readiness checks, logs, or process cleanup.
---

# Shell

Choose foreground or managed background execution deliberately and retain the
evidence required to explain success or failure.

## Workflow

1. State which fact the command should establish.
2. Use `run_command` for work expected to terminate: builds, compilers, linters,
   diagnostics, and one-shot generators.
   Inspect its `StateDelta`; use temporary output paths or clean check-only files.
3. Use `start_process` for servers, daemons, workers, and watchers.
4. For finite background work, retain the handle and do independent useful work.
   When its result is needed, call `wait_process` with that handle. It waits
   until an adaptive review checkpoint, completion or control interruption.
   At each checkpoint, inspect progress and reconsider independent useful work.
   Supply `estimated_duration_seconds` when known; long stable computations get
   less frequent reviews. `timeout_seconds` may shorten a checkpoint, but never
   terminates the process. Do not use shell `sleep` loops or rapid status-only
   turns in place of adaptive supervision.
   Use nonblocking `process_status` for service readiness, not completion waiting.
   Inspect logs on failure, and call `stop_process` when ownership ends.
5. Classify failures from exit code, stderr, timeout state, signal, and
   `failure_kind` before choosing a materially different recovery action.

Do not use detached shell patterns such as `&` or `nohup`; they bypass process
ownership, log capture, and cleanup.

## Examples

### Run a bounded build

1. Discover the project-native build command with `project-testing`.
2. Run it in the correct workspace-relative `cwd` with an appropriate timeout.
3. Inspect its exit code and archived output.
4. Validate the produced artifact rather than inferring success from exit code
   alone.

### Start and probe a service

1. Start the service with `start_process`.
2. Poll the handle until it exits or reaches the readiness deadline.
3. Run the TCP probe when a socket contract is sufficient:

```json
{"name":"shell","script":"scripts/probe_tcp.py","arguments":["127.0.0.1","8080","2.0"]}
```

4. Use a real protocol client when liveness does not prove readiness.
5. Stop the managed process after testing.

## Guidelines

- Keep stdout and stderr distinct while diagnosing.
- Do not increase a timeout until evidence shows slow progress rather than a hang.
- Do not retry an identical failed command without a new observation or changed
  hypothesis.
- Long computations should log progress and checkpoint reusable outputs. Do not
  kill and restart solely because logs are quiet; obtain concrete failure or
  throughput evidence first. Validate the exact model, input size, batch size,
  worker count and preprocessing on a representative small sample before a full run.
- Do not pipe through `head` merely to hide output; preserve the real exit status
  and use bounded artifacts.
- Use `project-testing.run_check` when the result must count as verification.
- Shell commands are not assumed read-only. Review created, modified, and deleted
  paths even when the exit code is zero.

## Resources

- Read [command contract](references/command-contract.md) for foreground execution.
- Read [process lifecycle](references/process-lifecycle.md) for managed services.
- Read [failure taxonomy](references/failure-taxonomy.md) before recovery.
