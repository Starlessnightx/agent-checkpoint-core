from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .session import Session


def _session_budget_reason(session: Session) -> str | None:
    task_usage = (
        session.task_usage.snapshot()
        if session.task_usage is not None else session.usage.dictionary()
    )
    input_tokens = task_usage["input_tokens"]
    output_tokens = task_usage["output_tokens"]
    budget = session.budget
    if session.token_budget is not None and session.usage.total_tokens >= session.token_budget:
        return (
            f"agent token budget reached: {session.usage.total_tokens} "
            f">= {session.token_budget}"
        )
    if budget is None:
        return None
    max_input = getattr(budget, "max_cumulative_input_tokens", None)
    max_output = getattr(budget, "max_cumulative_output_tokens", None)
    if max_input is not None and input_tokens >= int(max_input):
        return f"cumulative input token budget reached: {input_tokens} >= {max_input}"
    if max_output is not None and output_tokens >= int(max_output):
        return f"cumulative output token budget reached: {output_tokens} >= {max_output}"
    max_cost = getattr(budget, "max_cost_usd", None)
    if max_cost is not None and float(task_usage.get("cost_usd", 0)) >= float(max_cost):
        return (
            f"task cost budget reached: ${task_usage['cost_usd']:.6f} "
            f">= ${float(max_cost):.6f}"
        )
    return None
