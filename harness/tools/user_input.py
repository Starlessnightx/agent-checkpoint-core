"""Question tool: validation and a structured result, no Session mutation."""
from .base import ToolContext, ToolResult, ToolSpec, object_schema


def request_user_input(context: ToolContext, arguments: dict) -> ToolResult:
    questions = arguments.get("questions")
    valid = isinstance(questions, list) and bool(questions)
    if valid:
        valid = all(isinstance(q, dict) and set(q) == {"id", "question"}
                    and all(isinstance(q[key], str) and q[key].strip() for key in q)
                    for q in questions)
    if not valid or len({q["id"] for q in questions}) != len(questions):
        return ToolResult(False, "Provide nonempty questions with unique id and question fields.",
                          failure_kind="invalid_input")
    if context.interaction is None:
        return ToolResult(False, "User input is unavailable in this execution mode.",
                          failure_kind="user_input_unavailable")
    return context.interaction.ask(questions)


def user_input_spec() -> ToolSpec:
    return ToolSpec(
        "request_user_input", "Ask the user questions when an interactive frontend is available.",
        object_schema({"questions": {"type": "array", "minItems": 1, "items": object_schema({
            "id": {"type": "string"}, "question": {"type": "string"},
        }, ("id", "question"))}}, ("questions",)),
        request_user_input, "interaction:question",
    )
