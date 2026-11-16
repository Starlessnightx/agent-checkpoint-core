# Managed process lifecycle

`start_process` creates a process group, persistent log, and opaque handle in the
per-Agent ProcessRegistry. Only that handle may be passed to `process_status` and
`stop_process`; arbitrary host PIDs are not accepted.

Readiness has layers:

1. process exists;
2. process remains alive beyond immediate startup;
3. expected transport endpoint accepts connections;
4. application protocol returns the required response;
5. requested end-to-end behavior succeeds.

Record which layer has been demonstrated. On early exit, inspect the log. On a
readiness deadline, preserve the last status and log, then stop the process before
another startup attempt. A handle is scoped to its ToolRegistry and cannot control
unrelated host processes.

For a finite computation, continue independent useful work first. `wait_process`
returns at an adaptive model review checkpoint, process exit or control interruption.
Supply an estimated duration when known. Each checkpoint returns current logs/status
to the ordinary model loop, even if the process is still running. Longer stable work
gets less frequent review; no checkpoint kills the process. An explicit timeout may
shorten the next checkpoint. Quiet logs alone do not prove a hang. Preserve original
logs, distinguish service readiness from finite computation, and assess whether the
next interval or plan should change. A completed nonzero exit is a structured failure.
