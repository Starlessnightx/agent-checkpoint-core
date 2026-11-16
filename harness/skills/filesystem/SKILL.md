---
name: filesystem
description: Discover, read, search, view images, create, and edit workspace files with bounded, workspace-confined tools. Use whenever a task requires inspecting directories, source code or pictures, locating text, reading files, writing artifacts, replacing text, or applying patches.
---

# Filesystem

Use the installed file tools to inspect and modify the smallest workspace surface
that can satisfy the task.

## Workflow

1. Discover relevant paths with `list_directory`, `glob_files`, or `search_text`.
2. Read every edit target with `read_file`; use `read_many_files` only for a small,
   already identified set.
3. Choose one mutation primitive:
   - use `replace_text` for one exact, unique substitution;
   - use `apply_patch` for reviewable multi-hunk or multi-file edits;
   - use `write_file` for a new file or deliberate full replacement.
4. Inspect the returned `StateDelta` and re-read important changed regions.
5. Load `project-testing` and verify the deliverable after the final write.

Keep every path workspace-relative. Treat truncation and pagination markers as an
instruction to narrow or continue reading, not as permission to infer omitted
content.

## Examples

### Modify one function

1. Search for the function definition.
2. Read the implementation and its relevant test.
3. Apply an exact replacement or patch.
4. Re-read the changed region.
5. Run the focused test after the mutation.

### Inventory an unfamiliar tree

Run the deterministic inventory script through `run_skill_script`:

```json
{"name":"filesystem","script":"scripts/inventory.py","arguments":["src","tests"]}
```

Use its relative paths, sizes, and hashes as orientation evidence, not as a
substitute for semantic source inspection.

## Guidelines

- Use `view_image` for actual visual inspection of workspace PNG/JPEG/WebP/still
  GIF files; `read_file` is text-only. Example: `{"path":"samples/turtle.png",
  "detail":"auto"}`. Images arrive natively on the next model call, after all tool
  results. Do not infer image contents from filenames or a successful queue result.
- An unsupported model gets explicit feedback, not fabricated visual evidence.
  Choose small representative samples; viewing an image does not validate the
  dataset or prove the algorithm works. Audit snapshots allow later re-viewing
  even if the original file changes. After compaction, use `view_image` to reload
  a needed image instead of assuming its pixels remain in context.
- Do not begin with an unbounded recursive read.
- Do not guess after an ambiguous replacement; re-read and narrow the old text.
- Do not treat a successful write as proof that the task contract passes.
- Preserve user-owned changes and limit `StateDelta` to intended paths.
- Archive or paginate large reads instead of discarding their omitted portions.

## Resources

- Read [tool contracts](references/tool-contracts.md) for exact inputs and result
  fields.
- Read [safety and errors](references/safety-and-errors.md) for path boundaries,
  truncation, ambiguity, and recovery.
