# Filesystem tool contracts

## `list_directory`

Arguments: `path` defaults to `.`, `offset` defaults to `0`, and `limit` is
bounded to 500. The result is one directory level with entry type and size plus
pagination metadata. A missing directory and a path that names a file are errors.

## `glob_files`

Arguments: required `pattern`, optional base `path`, optional bounded `limit`.
Patterns are evaluated inside the workspace. Results are relative paths. Common
cache, VCS, dependency, and virtual-environment trees are excluded.

## `search_text`

Arguments: required `query`; optional `path`, `glob`, and `fixed_strings`.
The implementation uses ripgrep and returns path, one-based line number, and
matching text. Exit code 1 means no matches and is not a process crash. Missing
ripgrep is a classified dependency failure.

## `read_file`

Arguments: required `path`; optional one-based `start_line` and `line_count`.
Output includes numbered lines, total line count, and `next_start_line` when more
content exists. Files are decoded as UTF-8 with replacement for invalid bytes;
the tool is intended for text, not opaque binary data.

## `view_image`

Arguments: required workspace `path`; optional `detail` (`auto`, `low`, `high`).
PNG, JPEG, WebP and non-animated GIF are sent unmodified as native image inputs.
Headers are inspected locally; the provider remains responsible for full decoding.
SVG/PDF and animated files require a separately produced raster/still frame.

The result queues typed images, not a textual rendering or verification pass.
An Executor-backed content-addressed audit snapshot records source, MIME,
dimensions and SHA-256; it is not a deliverable or workspace StateDelta.
Delivery middleware appends image parts after all results in the tool batch.
Unknown provider capability is attempted normally; explicit unsupported-image
responses produce model-visible unavailability and prevent repeated bad requests.
Metadata or `MODEL_SUPPORTS_IMAGES=auto|true|false` may declare capability; no
automatic model substitution occurs. Images exceeding request capacity are not
silently dropped. After compaction, snapshots can be viewed again with this tool.

## `read_many_files`

Arguments: one or more `patterns`, optional file `limit` up to 200, and optional
`max_file_chars`. Each section is prefixed by its relative path. Unreadable or
binary files are counted as skipped. Per-file truncation instructs the caller to
continue with `read_file`. Aggregate output beyond the ToolContext soft limit is
stored as an artifact.

## `write_file`

Arguments: required `path` and UTF-8 `content`. Parent directories are created.
The implementation writes a temporary sibling and atomically replaces the target.
Result StateDelta reports `created` or `modified`; callers must not infer this
from prose.

## `replace_text`

Arguments: required `path`, `old`, and `new`. Exactly one occurrence must exist.
Zero occurrences return a not-found failure; multiple occurrences return an
ambiguity failure. No fuzzy, regex, or partial fallback is performed.

## `apply_patch`

Argument: required unified `patch`. Every `---`/`+++` header is validated against
the workspace; `/dev/null` is allowed for creation/deletion. The patch executable
must exist. The implementation performs a dry run before applying and returns a
StateDelta for affected paths.

## Shared result fields

All operations return `ToolResult`: `ok`, summary, stdout/stderr, optional
exit_code, artifact references, StateDelta, failure_kind, retryable, and optional
VerificationEvidence. Consumers should branch on structured fields, not parse a
success claim from free-form output.
