from __future__ import annotations

from pathlib import Path

from harness.executor import Executor, create_executor

from .doctor import Doctor, DoctorReport
from .probe import EnvProbe
from .state import EnvironmentState


class EnvironmentRuntime:
    def __init__(self, workspace: Path, executor: Executor | None = None):
        self.workspace = workspace.resolve()
        self.executor = executor or create_executor(self.workspace)
        self.state: EnvironmentState | None = None
        self.doctor_report: DoctorReport | None = None

    def bootstrap(self) -> EnvironmentState:
        probes = EnvProbe(self.executor, self.workspace).run()
        values = {probe.name: probe for probe in probes}
        state = EnvironmentState(
            os=str(values["os"].value), arch=str(values["arch"].value), cwd=str(self.workspace),
            runtime={"python": values["python"].value, "node": values["node"].value},
            executables={name: values[name].value for name in ("python", "node", "git")},
            package_managers={"pip": values["package_manager.pip"].value,
                              "npm": values["package_manager.npm"].value},
            environment=dict(values["environment"].value),
            permission={"workspace_writable": bool(values["workspace"].value.get("writable"))},
            workspace=dict(values["workspace"].value), network=dict(values["network"].value),
            probes=tuple(probe.dictionary() for probe in probes),
        )
        self.state = state
        self.doctor_report = Doctor().check(state)
        return state
