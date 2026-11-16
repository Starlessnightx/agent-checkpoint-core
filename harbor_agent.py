from __future__ import annotations

import os
import shlex
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harbor_capture import capture_harness_run


SOURCE_ROOT = Path(
    os.getenv("HARNESS_SOURCE_ROOT", Path(__file__).resolve().parent)
).resolve()
INSTALL_ROOT = "/installed-harness"


class TerminalBenchHarnessAgent(BaseInstalledAgent):
    def __init__(self, *args, extra_env=None, engine=None, permission_profile="restricted", **kwargs):
        selected_engine = os.getenv("HARNESS_ENGINE", "turn") if engine is None else engine
        if selected_engine not in {"legacy", "turn"}:
            raise ValueError("engine/HARNESS_ENGINE must be legacy or turn")
        if permission_profile not in {"restricted", "container-benchmark", "container-full-access"}:
            raise ValueError("unknown permission_profile")
        if permission_profile != "restricted" and selected_engine != "turn":
            raise ValueError("container permission profiles require engine=turn")
        env = dict(extra_env or {})
        key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("MODEL_API_KEY")
        if key:
            env["MODEL_API_KEY"] = key
        env["MODEL_BASE_URL"] = os.getenv(
            "MODEL_BASE_URL", "https://api.deepseek.com"
        )
        harbor_model = str(kwargs.get("model_name") or "")
        if "/" in harbor_model:
            harbor_model = harbor_model.split("/", 1)[1]
        env["MODEL_NAME"] = os.getenv(
            "MODEL_NAME", harbor_model or "deepseek-v4-flash"
        )
        for name in (
            "MODEL_THINKING",
            "MODEL_API_MODE",
            "MODEL_RESPONSES_STREAM",
            "MODEL_RESPONSES_PREVIOUS_ID",
            "MODEL_RESPONSES_STORE",
            "MODEL_PROMPT_CACHE_RETENTION",
            "MODEL_REASONING_EFFORT",
            "MODEL_PARALLEL_TOOL_CALLS",
            "HARNESS_ENABLE_TURN_PLAN",
            "MODEL_CONTEXT_WINDOW_TOKENS",
            "MODEL_MAX_OUTPUT_TOKENS",
            "MODEL_OUTPUT_RESERVE_TOKENS",
            "MODEL_CAPABILITY_DISCOVERY",
            "MODEL_INPUT_USD_PER_MILLION",
            "MODEL_CACHED_INPUT_USD_PER_MILLION",
            "MODEL_OUTPUT_USD_PER_MILLION",
            "HARNESS_MAX_CONTEXT_TOKENS",
            "HARNESS_MAX_CUMULATIVE_INPUT_TOKENS",
            "HARNESS_MAX_CUMULATIVE_OUTPUT_TOKENS",
            "HARNESS_MAX_COST_USD",
            "HARNESS_MAX_TURNS",
            "HARNESS_MAX_TOOL_CALLS",
            "SCIENCE_CHECKPOINT_CONTROL_DIR",
            "SCIENCE_CHECKPOINT_ACK_TIMEOUT",
        ):
            value = os.getenv(name)
            if value is not None:
                env[name] = value
        super().__init__(*args, extra_env=env, **kwargs)
        self.engine = selected_engine
        self.permission_profile = permission_profile
        self.task_workdir = "/app"

    @staticmethod
    def name() -> str:
        return "self-improving-harness"

    def version(self) -> str | None:
        return "0.2.0"

    async def install(self, environment: BaseEnvironment) -> None:
        result = await environment.exec(command="pwd")
        if result.return_code == 0 and result.stdout:
            self.task_workdir = result.stdout.strip()
        await self.exec_as_root(
            environment,
            command=(
                f"mkdir -p {INSTALL_ROOT} && "
                "if command -v python3 >/dev/null 2>&1; then :; "
                "elif command -v apt-get >/dev/null 2>&1; then "
                "for attempt in 1 2 3; do "
                "DEBIAN_FRONTEND=noninteractive apt-get update && break; "
                "sleep 2; "
                "done; "
                "installed=0; "
                "for attempt in 1 2 3; do "
                "if DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "--fix-missing --no-install-recommends "
                "python3 ca-certificates; then installed=1; break; fi; "
                "sleep 2; "
                "done; "
                "[ \"$installed\" -eq 1 ]; "
                "elif command -v apk >/dev/null 2>&1; then "
                "apk add --no-cache python3 ca-certificates; "
                "elif command -v dnf >/dev/null 2>&1; then "
                "dnf install -y python3 ca-certificates; "
                "elif command -v yum >/dev/null 2>&1; then "
                "yum install -y python3 ca-certificates; "
                "else echo 'No supported Python installer found' >&2; exit 1; fi"
            ),
            timeout_sec=300,
        )
        await environment.upload_dir(
            SOURCE_ROOT / "harness", f"{INSTALL_ROOT}/harness"
        )
        await environment.upload_dir(
            SOURCE_ROOT / "safety", f"{INSTALL_ROOT}/safety"
        )
        await environment.upload_file(
            SOURCE_ROOT / "main.py", f"{INSTALL_ROOT}/main.py"
        )
        # S1a boundary export is opt-in via SCIENCE_CHECKPOINT_CONTROL_DIR.
        # Keep the default installed-agent behavior unchanged.
        if os.getenv("SCIENCE_CHECKPOINT_CONTROL_DIR"):
            await environment.upload_dir(
                SOURCE_ROOT / "checkpoint_plugin", f"{INSTALL_ROOT}/checkpoint_plugin"
            )
            await environment.upload_dir(
                SOURCE_ROOT / "science_checkpoint", f"{INSTALL_ROOT}/science_checkpoint"
            )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        try:
            await self.exec_as_agent(
                environment,
                command=(f"python3 {shlex.quote(INSTALL_ROOT + '/main.py')} --engine "
                         f"{shlex.quote(getattr(self, 'engine', 'turn'))} --headless -- "
                         f"{shlex.quote(instruction)} </dev/null"),
                cwd=self.task_workdir,
                env={
                    "HARNESS_WORKSPACE": self.task_workdir,
                    # Harbor already provides the container boundary. This selects
                    # the executor adapter that treats that boundary as canonical.
                    "HARNESS_EXECUTOR": "contained",
                    "HARNESS_PERMISSION_PROFILE": getattr(self, "permission_profile", "restricted"),
                    **({"SCIENCE_CHECKPOINT_CONTROL_DIR": os.environ["SCIENCE_CHECKPOINT_CONTROL_DIR"]}
                       if os.getenv("SCIENCE_CHECKPOINT_CONTROL_DIR") else {}),
                    **({"SCIENCE_CHECKPOINT_ACK_TIMEOUT": os.environ["SCIENCE_CHECKPOINT_ACK_TIMEOUT"]}
                       if os.getenv("SCIENCE_CHECKPOINT_ACK_TIMEOUT") else {}),
                },
            )
        finally:
            try:
                await capture_harness_run(
                    environment,
                    context,
                    workspace=self.task_workdir,
                    logs_dir=self.logs_dir,
                )
            except Exception:
                # Evidence collection is best-effort and must never replace the
                # original Agent failure that Harbor needs to classify.
                pass
