# Verification evidence and failure handling

VerificationEvidence must identify the command, cwd, named target, exit code,
duration, and output/artifact reference. It supports only the target actually
exercised by that command.

Verification commands are not assumed to be read-only. Review their StateDelta;
build executables, caches, snapshots, coverage files, and generated fixtures may
violate the final artifact contract even when the check passes. Prefer temporary
output paths and remove check-only artifacts before completion.

After a failure, preserve the first relevant error and the final summary. Avoid
changing multiple components before testing a root-cause hypothesis. For flaky or
performance-sensitive checks, repeat under controlled conditions and retain the
distribution rather than selecting the best run.

A passing build can prove compilation/package formation but not runtime semantics.
A passing unit test can prove one behavior but not packaging. A live process can
prove neither readiness nor protocol correctness. Compose evidence according to
the task contract.
