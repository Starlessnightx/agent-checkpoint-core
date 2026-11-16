"""Unit-test seams for one sample/tool batch and explicitly installed policies.

These helpers have no execution loop, completion gate or cancellation policy.
Production turn lifecycle coverage uses TurnLoopDriver / RegularTask directly.
"""
from harness.core.sampling import SamplingStep
from harness.planning.middleware import PlanningMiddleware
from harness.planning.store import PlanStore
from harness.planning.tools import planning_tools
from harness.core.hooks import HookPipeline


def sample_tools(runtime, session):
    sampled = SamplingStep(runtime.spec, session.model or runtime.model).sample(session, lambda: False)
    if sampled.response is not None:
        response = sampled.response
        session.messages.append({key: value for key, value in response.items() if not key.startswith("_")})
        calls = response.get("tool_calls") or []
        if calls:
            runtime.execute_tool_calls(session, calls, session.state.turn)
    return sampled


def install_planning(session):
    """Exercise optional planning policy without changing the default assembly."""
    store = PlanStore(session.tools.context.executor,
                      session.tools.workspace / ".harness" / "plans" / session.session_id,
                      session.task.text)
    store.seed_conditions(list(session.task.success_conditions))
    session.tools.context.plan_store = store
    session.subagents.plan_store = store
    for tool in planning_tools(store, session.subagents):
        session.tools.register(tool)
    session.hooks = HookPipeline(session.hooks.middleware + (
        PlanningMiddleware(store, session.tools.context.background_tasks),))
    return store
