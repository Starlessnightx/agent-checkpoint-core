"""A pure request tool: only the boundary policy may perform the checkpoint."""
from harness.tools.base import ToolPermission, ToolResult, ToolSpec


def request_checkpoint(context, arguments):
    reason = arguments.get('reason')
    if type(reason) is not str or not reason.strip() or len(reason) > 512:
        return ToolResult(False, 'Provide a milestone reason of 1–512 characters.')
    return ToolResult(True,
        'Checkpoint requested, not saved yet; this response does not confirm a successful save. '
        'The harness evaluates the request at a safe boundary and may defer or reject it. '
        'The harness manages saving and any required waiting. Continue when control returns; '
        'do not poll checkpoint files or repeat this request without new work or an explicit '
        'failure whose cause you have addressed. Do not claim a successful save without confirmation.',
        structured={'checkpoint_request': {'schema_version': 1, 'reason': reason.strip()}})


def checkpoint_tool():
    return ToolSpec('request_checkpoint',
        'Request a checkpoint to preserve meaningful completed work or a useful branching point '
        'before the next costly operation. Use it sparingly: at most one request per phase, and '
        'not merely because time or tool calls passed. A completed experiment with useful '
        'failure results also qualifies; passing tests are not required. Avoid routine reads, '
        'small edits, intermediate artifacts, and duplicate requests. First collect tool results, write important outputs to disk, and '
        'wait for task processes and delegated work to finish; do not kill useful processes '
        'just to save. Running-program memory and live connections are not captured. '
        'Make this the only tool call in the response, after preceding work has returned. '
        'The harness checks the safe boundary and manages any required waiting. This tool '
        'acknowledges a request, not a successful save; saving may be deferred or rejected. '
        'Initial and final boundaries are automatic. Give a factual milestone, result, and '
        'optional next decision in reason.',
        {'type': 'object', 'properties': {'reason': {'type': 'string', 'minLength': 1, 'maxLength': 512,
         'description': 'Brief milestone, observed result, and optional next decision. Example: '
                        'Features saved to features.csv; validation passed; next: compare two models. '
                        'State failures honestly; exclude credentials and secrets.'}},
         'required': ['reason'], 'additionalProperties': False},
        request_checkpoint, 'skill:checkpoint', required_permission=ToolPermission.READ_ONLY)


def boundary_factory(freeze, control_root, *, wait_seconds=0, require_joint=False, config=None):
    from .boundary import BoundaryClient
    from .policy import CheckpointPolicy
    def assemble(session):
        tool = checkpoint_tool()
        existing = session.tools.specs.get(tool.name)
        if existing is not None and existing != tool:
            raise ValueError('checkpoint request tool name is already in use')
        allowed = session.tools.allowed_tools
        if (allowed is None or tool.name in allowed) and tool.name not in session.tools.specs:
            session.tools.register(tool)
            try:
                session.tools.router.activate([tool.name])
            except Exception:
                pass
        return BoundaryClient(freeze, control_root, wait_seconds=wait_seconds,
                              require_joint=require_joint, policy=CheckpointPolicy(config))
    return assemble
