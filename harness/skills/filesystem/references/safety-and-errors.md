# Filesystem safety and error recovery

## Boundary model

Every path is resolved against `ToolContext.workspace`. Absolute paths, `..`
escapes, symlink escapes, and patch headers resolving outside that root are
rejected before mutation. Skill loading does not override the Safety layer.

Subagents can receive a filtered installation. A read-only role may load this
package but only receive list/glob/search/read operations; write handlers remain
absent from its registry.

## Failure classes

- `permission`: path or capability boundary violation. Correct the target or
  request an authorized role; retrying an equivalent path is useless.
- `invalid_input`: empty patterns, malformed patch, or invalid arguments. Repair
  the request before retrying.
- `not_found`: target or exact old text does not exist. Discover/read current
  state rather than guessing.
- `ambiguous`: an exact edit target appears multiple times. Add surrounding
  context until it identifies one occurrence.
- `patch_rejected`: the dry run disagrees with current files. Re-read all failed
  hunks and generate a patch from the current base.
- `missing_dependency`: a required local executable such as `rg` or `patch` is
  unavailable. Inspect project/environment constraints before installing.
- `tool_runtime_error`: unexpected implementation failure. Preserve the event
  and artifact; do not reinterpret it as a file-level failure.

## Output pressure

Broad glob/search/read output can consume the model context. Prefer narrower
queries. When the runtime returns an artifact reference, treat the visible head
and tail as a preview and retrieve only the relevant archived section. Context
compaction preserves artifact paths in the deterministic ledger.
