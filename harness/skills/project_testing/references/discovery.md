# Check discovery reference

## Python

Inspect `pyproject.toml`, `tox.ini`, `noxfile.py`, `pytest.ini`, `setup.cfg`,
`Makefile`, and CI workflows. Respect configured test paths, markers, extras, and
the repository's environment manager.

## JavaScript and TypeScript

Inspect `package.json` scripts and the matching lockfile before selecting npm,
pnpm, yarn, or bun. Note monorepo workspace filters and separate test/build/type
check scripts.

## Rust

Inspect `Cargo.toml` workspace membership, features, examples, and package names.
Choose focused package tests during iteration and workspace checks for cross-crate
changes.

## Go

Inspect `go.mod`, workspace files, Make targets, generated code, build tags, and
package layout. Prefer repository-declared wrappers when they set required flags.

## Make and CI

Make targets and CI jobs are strong evidence but may include release credentials
or external services. Read the target/job before executing. Reproduce the smallest
safe local portion of a CI contract when full infrastructure is unavailable.
