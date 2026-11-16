# Trajectory index schema

An index has `schema_version`, workspace, source list, parse errors, normalized
events, and aggregate metrics. Event IDs are stable only within that index.

Normalized events retain: source, inherited task, type, tool/name, ok, exit_code,
failure_kind, retryable, arguments, output_preview, artifacts, StateDelta,
VerificationEvidence, and a call fingerprint derived from tool plus arguments.

The fingerprint detects identical calls; it does not prove equivalent workspace
state. Output preview is capped and must not replace referenced artifacts or raw
sources when exact evidence matters.
