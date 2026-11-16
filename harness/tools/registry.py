from __future__ import annotations

from pathlib import Path
from typing import Any
from dataclasses import replace

from harness.executor.base import Executor
from harness.skills.registry import SkillRegistry
from harness.skills.tools import skill_tool_specs
from harness.executor.factory import create_executor
from harness.permissions import PermissionDecision, PermissionPolicy, Sandbox

from .base import ToolContext, ToolFailure, ToolRequest, ToolResult, ToolSpec, object_schema
from .router import ToolRouter
from harness.capabilities import default_capability_graph


class ToolRegistry:
    """Mutable per-agent registry supporting progressive capability loading."""

    def __init__(
        self,
        workspace: Path,
        specs: tuple[ToolSpec, ...],
        *,
        output_soft_limit: int = 16_000,
        allowed_skills: tuple[str, ...] | None = None,
        allowed_tools: tuple[str, ...] | None = None,
        executor: Executor | None = None,
        permission_policy: PermissionPolicy | None = None,
        sandbox: Sandbox | None = None,
        independent_tools: bool = False,
    ):
        self.workspace = workspace.resolve()
        self.independent_tools = independent_tools
        self.specs: dict[str, ToolSpec] = {}
        self.skills = SkillRegistry()
        self.allowed_skills = set(allowed_skills) if allowed_skills is not None else None
        self.allowed_tools = set(allowed_tools) if allowed_tools is not None else None
        self.permission_policy = permission_policy or PermissionPolicy()
        self.sandbox = sandbox or Sandbox()
        self.active_skills: list[str] = []
        self.active_tool_names: tuple[str, ...] = ()
        executor = executor or create_executor(self.workspace)
        self.context = ToolContext(
            workspace=self.workspace,
            artifact_root=self.workspace / ".harness" / "artifacts",
            process_registry=executor,
            output_soft_limit=output_soft_limit,
            executor=executor,
            filesystem_root=getattr(self.sandbox, "filesystem_root", None),
        )
        for spec in specs:
            self.register(spec)
        if independent_tools:
            from .catalog import builtin_tool_specs

            legacy_guides = {spec.name: spec for spec in skill_tool_specs(self.skills)}
            for spec in skill_tool_specs(self.skills, instructions_only=True, allowed_skills=self.allowed_skills):
                if spec.name in self.specs:
                    configured = self.specs[spec.name]
                    original = legacy_guides[spec.name]
                    if (configured.handler.__module__, configured.handler.__qualname__) == (
                            original.handler.__module__, original.handler.__qualname__):
                        self.specs[spec.name] = replace(configured, handler=spec.handler,
                            description=spec.description if configured.description == original.description else configured.description)
            # Conservatively map older skill-only capability restrictions. New
            # configurations should use an explicit tool allowlist instead.
            groups = self.allowed_skills if self.allowed_tools is None else None
            for spec in builtin_tool_specs(groups):
                if spec.name not in self.specs and (self.allowed_tools is None or spec.name in self.allowed_tools):
                    self.register(spec)
        if "tool_search" not in self.specs:
            self.register(ToolSpec(
                "tool_search",
                "Search the registered capability catalog and activate matching tools.",
                object_schema({
                    "query": {"type": "string"},
                    "capability": {"type": "string"},
                }),
                lambda context, arguments: ToolResult(False, "tool_search is handled by the registry"),
                "skill:search",
            ))
        self.capability_graph = default_capability_graph()
        self.router = ToolRouter(self, self.capability_graph)
        self.active_tool_names = tuple(spec.name for spec in self.router.select("", {}, {}))
        self.context.tool_registry = self
        self.context.lsp_manager = None
        self.context.environment_state = None

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("tool name must be non-empty")
        if spec.name in self.specs:
            raise ValueError(f"tool already registered: {spec.name}")
        self.specs[spec.name] = spec

    def checkpoint_state(self) -> dict[str, Any]:
        """Catalog/progressive-loading data, never executable handlers or grants."""
        import time
        now = time.monotonic()
        return {
            "schema_version": 1, "independent_tools": self.independent_tools,
            "active_skills": list(self.active_skills), "active_tool_names": list(self.active_tool_names),
            "allowed_skills": sorted(self.allowed_skills) if self.allowed_skills is not None else None,
            "allowed_tools": sorted(self.allowed_tools) if self.allowed_tools is not None else None,
            "registered": [{"name": item.name, "schema": item.schema(), "capability": item.capability,
                            "implementation": item.handler.__module__ + ":" + item.handler.__qualname__,
                            "metadata": item.metadata, "timeout_seconds": item.timeout_seconds}
                           for item in self.specs.values()],
            "router_decision": self.router.last_decision.dictionary(),
            "output_soft_limit": self.context.output_soft_limit,
            "exec_output_offsets": dict(self.context.exec_output_offsets),
            "process_elapsed_seconds": {key: max(0, now - start)
                                        for key, start in self.context.process_started_times.items()},
            "authority_restored": False,
        }

    def restore_checkpoint_state(self, value: dict[str, Any]) -> None:
        """Restore progressive visibility, never handlers, grants or live handles."""
        import json
        import math
        import time
        from harness.core.portable_state import copy_plain, require_fields, strings
        from .router import RouteDecision
        value = copy_plain(value)
        current = json.loads(json.dumps(self.checkpoint_state()))
        require_fields(value, current)
        for key in ('schema_version', 'independent_tools', 'allowed_skills', 'allowed_tools',
                    'registered', 'output_soft_limit', 'authority_restored'):
            if type(value[key]) is not type(current[key]) or value[key] != current[key]:
                raise ValueError('tool deployment configuration differs: ' + key)
        for key in ('active_skills', 'active_tool_names'):
            strings(value[key])
            if len(value[key]) != len(set(value[key])):
                raise ValueError('duplicate tool visibility entry')
        if (any(name not in self.specs for name in value['active_tool_names'])
                or any(name not in self.skills.names() for name in value['active_skills'])
                or self.allowed_tools is not None and not set(value['active_tool_names']) <= self.allowed_tools
                or self.allowed_skills is not None and not set(value['active_skills']) <= self.allowed_skills):
            raise ValueError('restored tool visibility exceeds the target catalog')
        if type(value['exec_output_offsets']) is not dict or type(value['process_elapsed_seconds']) is not dict:
            raise ValueError('invalid process history')
        completed = {p.handle for p in self.context.executor.checkpoint_processes() if p.running is False}
        for handle, offset in value['exec_output_offsets'].items():
            if handle not in completed or type(offset) is not int or offset < 0:
                raise ValueError('completed process state needs a capable Executor importer')
        for handle, elapsed in value['process_elapsed_seconds'].items():
            if (handle not in completed or type(elapsed) not in (int, float)
                    or not math.isfinite(elapsed) or elapsed < 0):
                raise ValueError('completed process state needs a capable Executor importer')
        decision = value['router_decision']
        require_fields(decision, {'selected', 'reason'})
        strings(decision['selected'])
        if (type(decision['reason']) is not str or len(set(decision['selected'])) != len(decision['selected'])
                or any(name not in self.specs for name in decision['selected'])):
            raise ValueError('invalid restored route decision')
        self.active_skills = value['active_skills']
        self.active_tool_names = tuple(value['active_tool_names'])
        self.router.last_decision = RouteDecision(tuple(decision['selected']), decision['reason'])
        self.context.exec_output_offsets = value['exec_output_offsets']
        now = time.monotonic()
        self.context.process_started_times = {key: now-elapsed for key, elapsed in value['process_elapsed_seconds'].items()}

    def get(self, name: str) -> ToolSpec:
        try:
            return self.specs[name]
        except KeyError as exc:
            raise KeyError(f"unknown or unloaded tool: {name}") from exc

    def search(self, capability: str) -> tuple[ToolSpec, ...]:
        query = capability.strip().casefold()
        if not query:
            return ()
        return tuple(
            spec for spec in self.specs.values()
            if spec.capability.casefold() == query
            or spec.capability.casefold().startswith(query + ":")
        )

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self.specs.values()]

    def active_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self.specs[name] for name in self.active_tool_names if name in self.specs)

    def active_schemas(self) -> list[dict[str, Any]]:
        # Routing/permissions still select the active set. Enumeration order is
        # not a capability change and must not churn the provider prompt prefix.
        return [spec.schema() for spec in sorted(self.active_specs(), key=lambda spec: spec.name)]

    def set_routing_context(self, task: str, state: Any = None, environment: Any = None) -> None:
        selected = self.router.select(task, state, environment)
        self.active_tool_names = tuple(spec.name for spec in selected)

    def attach_lsp_manager(self, manager: Any) -> None:
        """Attach an already configured manager; no server is started implicitly."""
        self.context.lsp_manager = manager

    def resolve_capability(self, capability: str, environment: Any = None,
                           permission: str = "workspace_write") -> Any:
        return self.router.resolve_capability(capability, environment, permission)

    def enable_skill(self, name: str) -> tuple[str, ...]:
        if self.allowed_skills is not None and name not in self.allowed_skills:
            raise PermissionError(f"skill is not permitted for this agent: {name}")
        if name in self.active_skills:
            return ()
        if self.independent_tools:
            self.skills.get(name)  # Validate knowledge selection, not tool installation.
            self.active_skills.append(name)
            return ()
        provided = self.skills.tool_specs(name)
        if self.allowed_tools is not None:
            provided = tuple(
                spec for spec in provided if spec.name in self.allowed_tools
            )
        if not provided:
            raise PermissionError(
                f"skill {name} provides no tools permitted for this agent"
            )
        collisions = sorted(spec.name for spec in provided if spec.name in self.specs)
        if collisions:
            raise ValueError(
                f"skill {name} collides with installed tools: {', '.join(collisions)}"
            )
        for spec in provided:
            self.register(spec)
        self.active_skills.append(name)
        # Loading a skill is an explicit activation boundary; expose its tools
        # immediately for compatibility, while the initial surface stays small.
        self.router.activate(spec.name for spec in provided)
        return tuple(spec.name for spec in provided)

    def dispatch(self, request: ToolRequest) -> ToolResult:
        if (self.independent_tools and request.name in {"load_skill", "read_skill_reference", "run_skill_script"}
                and self.allowed_skills is not None and request.arguments.get("name") not in self.allowed_skills):
            return ToolResult(False, "skill is not permitted for this agent", failure_kind="permission_denied")
        if request.name == "tool_search":
            query = str(request.arguments.get("query", request.arguments.get("capability", "")))
            matches = self.router.search(query)
            total_matches = len(matches)
            if self.independent_tools:
                matches = matches[:8]
            decisions = {spec.name: self.permission_policy.evaluate(spec, ToolRequest(spec.name, {}), self.context)
                         for spec in matches}
            available = [spec for spec in matches if decisions[spec.name].decision is not PermissionDecision.DENY]
            activated = self.router.activate(spec.name for spec in available)
            results = [{"name": spec.name, "description": spec.description, "capability": spec.capability,
                        "status": ("permission_denied" if decisions[spec.name].decision is PermissionDecision.DENY
                                   else "approval_required" if decisions[spec.name].decision is PermissionDecision.ASK
                                   else "available"),
                        "schema_loaded": spec.name in activated} for spec in matches]
            import json
            return ToolResult(True, f"found {len(matches)} matching tools",
                              stdout=json.dumps({"tools": [spec.schema() for spec in available],
                                                 "activated": list(activated), "results": results,
                                                 "total_matches": total_matches,
                                                 "has_more": total_matches > len(matches),
                                                 "catalog_status": "matches" if matches else "no_match_in_configured_catalog",
                                                 "hint": ("Refine the query or check configured tools/services; reading a skill does not grant permission."
                                                          if not matches else "Activation is not authorization; each call is checked again.")},
                                                ensure_ascii=False, indent=2),
                              structured={"activated": list(activated),
                                          "results": results,
                                          "registered_tools": len(self.specs),
                                          "active_tools": len(self.active_tool_names)})
        try:
            spec = self.get(request.name)
        except KeyError:
            return ToolResult(
                False,
                (f"unknown tool: {request.name}; search the configured tool catalog or configure the required service"
                 if self.independent_tools else
                 f"unknown or unloaded tool: {request.name}; load the matching capability skill first"),
                failure=ToolFailure(
                    "unknown_tool",
                    f"unknown or unloaded tool: {request.name}",
                ),
            )
        permission_arguments = dict(request.arguments)
        for source, target in spec.permission_argument_aliases:
            if source in request.arguments:
                permission_arguments[target] = request.arguments[source]
        permission_request = replace(request, arguments=permission_arguments)
        permission = self.permission_policy.evaluate(spec, permission_request, self.context)
        if permission.decision is PermissionDecision.DENY:
            return ToolResult(False, f"permission denied: {permission.reason}",
                              failure_kind="permission_denied",
                              structured={"permission": permission.dictionary()})
        if permission.decision is PermissionDecision.ASK and not self.permission_policy.approve(
            permission, spec, permission_request
        ):
            return ToolResult(False, f"permission approval required: {permission.reason}",
                              failure_kind="permission_approval_required",
                              structured={"permission": permission.dictionary()})
        sandbox = self.sandbox.check(request, self.context)
        if not sandbox.allowed:
            return ToolResult(False, f"sandbox denied: {sandbox.reason}",
                              failure_kind="permission",
                              structured={"permission": permission.dictionary(),
                                          "sandbox": sandbox.dictionary()})
        try:
            result = spec.execute(request, self.context)
            if request.name == "load_skill" and result.ok:
                installed = self.enable_skill(str(request.arguments.get("name", "")))
                if installed:
                    result = ToolResult(
                        True,
                        result.summary + "\nRuntime tools now available: " + ", ".join(installed),
                        stdout=result.stdout,
                        artifacts=result.artifacts,
                    )
            return replace(result, structured={
                **(result.structured or {}),
                "permission": permission.dictionary(),
            })
        except PermissionError as exc:
            return ToolResult(
                False,
                str(exc),
                failure=ToolFailure("permission", str(exc)),
            )
        except Exception as exc:
            return ToolResult(
                False,
                f"{type(exc).__name__}: {exc}",
                failure=ToolFailure(
                    "tool_runtime_error", f"{type(exc).__name__}: {exc}"
                ),
            )

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Compatibility adapter; Runtime code must use dispatch(ToolRequest)."""
        return self.dispatch(ToolRequest(name=name, arguments=arguments))


def default_tool_specs() -> tuple[ToolSpec, ...]:
    """Bootstrap only skill discovery; capabilities arrive through load_skill."""
    return skill_tool_specs()
