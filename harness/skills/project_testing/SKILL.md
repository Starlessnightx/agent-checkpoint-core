---
name: project-testing
description: Inspect repository test machinery, discover project-native checks, and execute structured verification. Use whenever a task requires testing, linting, type checking, building, validating an artifact, selecting a verification command, or proving a change after the final write.
---

# Project Testing

Select the lowest-cost project-native check that exercises the changed contract,
then broaden verification according to regression risk.

## Workflow

1. Call `inspect_repository` at the smallest relevant project root.
2. Call `discover_checks` and prefer commands justified by repository metadata,
   scripts, build files, or CI configuration.
3. Name the exact target being verified.
4. Run the focused command with `run_check` after the final workspace mutation.
5. Inspect the returned `StateDelta`. Put disposable compiler/build output in a
   temporary directory when possible; otherwise remove artifacts created only by
   verification and restore unintended modifications before completion.
6. Broaden from syntax and focused tests to package, integration, build, or full
   suite checks when the changed surface requires it.
7. Preserve VerificationEvidence and report untested constraints explicitly.

Keep product failures, malformed tests, infrastructure failures,
timeouts/resource failures, and unrelated regressions separate.

## Examples

### Verify a Python library change

1. Inspect `pyproject.toml` and the test layout.
2. Discover declared test and build commands.
3. Run the focused module after implementation.
4. Run the package build or broader suite when public API or packaging changed.

### Build a deterministic check matrix

```json
{"name":"project-testing","script":"scripts/check_matrix.py","arguments":["."]}
```

Use the result to find repository evidence for candidate commands. Do not assume
every Python repository uses pytest or every JavaScript repository uses npm.

## Guidelines

- Name targets such as `config parser invalid-input tests`, not `test` or `all`.
- Do not accept a check that passed before the final write as current evidence.
- Do not turn unavailable infrastructure into a product pass.
- Do not discard broad-suite failures merely because they are outside the edited
  file.
- Record command, target, cwd, exit code, duration, and output artifacts.
- Treat a successful check with workspace side effects as unfinished cleanup,
  especially when the deliverable constrains filenames or directory contents.

## Resources

- Read [check discovery](references/discovery.md) for ecosystem-specific routing.
- Read [evidence and failures](references/evidence-and-failures.md) for result
  interpretation and completion rules.
