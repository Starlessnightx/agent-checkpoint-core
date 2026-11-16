from __future__ import annotations
from dataclasses import dataclass
import json
from typing import Any, Callable
from uuid import uuid4
from harness.api_audit import ApiAuditStore, bind_api_audit
from harness.context.builder import ModelContext
from harness.context.manager import RequestCapacityError, estimate_tokens
from harness.media.images import ModelImageInputError
from harness.model_limits import ModelContextLengthError, ModelPricing
from harness.spec import HarnessSpec
from harness.types import ChatModel
from harness.provider_recovery import ProviderBillingError, ProviderTransientError, wait_for_billing_resume
from .session import Session
from .budgets import _session_budget_reason
from .sampling_support import _inject_hook_context, _last_usage, _usage_snapshot, _policy_get, _text

@dataclass(frozen=True)
class SamplingResult:
    response: dict[str, Any] | None = None
    retry: bool = False
    termination: str | None = None
    reasons: tuple[str, ...] = ()
    text: str = ""

    def __post_init__(self):
        if sum((self.response is not None, self.retry, self.termination is not None)) != 1:
            raise ValueError("sampling result must be response, retry or stop")

    @classmethod
    def stop(cls, termination: str, reasons: tuple[str, ...], text: str = "") -> SamplingResult:
        return cls(termination=termination, reasons=reasons, text=text)

class SamplingStep:
    """Shared request/recovery/accounting, without Session finalization."""
    def __init__(self, spec: HarnessSpec, model: ChatModel):
        self.spec, self.model = spec, model

    def _before_model(self, session: Session, model: ChatModel,
                      interrupt: Callable[[], bool]) -> SamplingResult | None:
        if interrupt():
            return SamplingResult.stop('cancellation_requested', ('cancellation requested',))
        context = session.hook('BeforeModel', turn=session.state.turn, phase=session.state.phase)
        while context is not None and context.stopped:
            reason = str(context.data.get('stop_reason') or 'model request blocked by middleware')
            termination = 'model_hook_blocked'
            if context.data.get('approval'):
                interaction = session.tools.context.interaction
                if interaction is not None and interaction.approve_model_hook(
                    str(getattr(model, 'model', type(model).__name__)), uuid4().hex, reason,
                ):
                    context = session.hooks.resume_after_approval(context)
                    continue
                termination = ('model_approval_denied' if interaction is not None and interaction.interactive
                               else 'model_approval_required')
            if interrupt():
                return SamplingResult.stop('cancellation_requested', ('cancellation requested',))
            session.emit('model_request_blocked', reason=reason, termination=termination)
            return SamplingResult.stop(termination, (reason,))
        if interrupt():
            return SamplingResult.stop('cancellation_requested', ('cancellation requested',))
        _inject_hook_context(session, context, session.state.turn, session.state.phase)
        return None

    def sample(self, session: Session, interrupt: Callable[[], bool]) -> SamplingResult:
        turn = session.state.turn
        if turn < 1:
            raise ValueError('session.state.turn must be set before run_turn')
        orchestration = session.orchestrator
        model = session.model or self.model
        session.state.phase = orchestration.phase
        exhausted = _session_budget_reason(session)
        if exhausted:
            session.emit('budget', turn=turn, phase=orchestration.phase, reason=exhausted, usage=session.usage.dictionary(), task_usage=session.task_usage.snapshot() if session.task_usage is not None else session.usage.dictionary())
            result = SamplingResult.stop('budget_exhausted', (exhausted,), session.state.final_text)
            return result
        blocked = self._before_model(session, model, interrupt)
        if blocked is not None:
            return blocked
        schemas = session.tools.active_schemas()
        try:
            model_context, compaction = session.context_manager.build_model_context(session.messages, session.events, schemas)
        except RequestCapacityError as exc:
            session.emit('context_capacity_exceeded', turn=turn, input_capacity=session.context_manager.max_context_tokens, reason=str(exc))
            return SamplingResult.stop('context_capacity_exceeded', (str(exc),), session.state.final_text)
        session.model_context = model_context
        if compaction:
            session.hook('AfterCompact', compaction=compaction)
            event_type = str(compaction.get('type', 'context_compaction'))
            session.emit(event_type, **{key: value for key, value in compaction.items() if key != 'type'}, turn=turn, phase=orchestration.phase)
        session.emit('model_context_built', turn=turn, phase=orchestration.phase, raw_message_count=len(session.messages), model_message_count=len(model_context.messages), estimated_tokens=model_context.estimated_tokens, observation_refs=list(model_context.observation_refs), prefix_reuse=dict(session.context_manager.last_prefix_metrics))
        context_retries = 0
        transient_retries = 0
        while True:
            if interrupt():
                return SamplingResult.stop('cancellation_requested', ('cancellation requested',))
            exhausted = _session_budget_reason(session)
            if exhausted:
                session.emit('budget', turn=turn, reason=exhausted)
                return SamplingResult.stop('budget_exhausted', (exhausted,), session.state.final_text)
            if session.active_turn_id is not None:
                session.sampling_index += 1
            session.emit('model_requested', turn=turn, phase=orchestration.phase, message_count=len(model_context.messages), raw_message_count=len(session.messages), tool_count=len(schemas), registered_tools=len(session.tools.specs), active_tools=len(session.tools.active_specs()), context_tokens=session.context_manager.last_metrics.get('context_tokens', 0), estimated_request_tokens=session.context_manager.last_metrics.get('request_tokens', 0), input_capacity=session.context_manager.max_context_tokens, tool_schema_tokens=max(1, len(json.dumps(schemas, ensure_ascii=False)) // 3), observation_count=session.context_manager.last_metrics.get('observation_count', 0), context_retry=context_retries)
            try:
                audit = ApiAuditStore(session.recorder.path.parent / 'api',
                                      session.tools.context.executor, session.emit)
                with bind_api_audit(audit):
                    if getattr(model, "supports_cache_key", False):
                        response = model.complete(
                            model_context.messages, schemas,
                            cache_key=session.session_id,
                        )
                    else:
                        response = model.complete(model_context.messages, schemas)
                session.emit('sampling_response', turn=turn, response=response)
                break
            except ProviderBillingError:
                if not wait_for_billing_resume(session, interrupt):
                    return SamplingResult.stop('cancellation_requested', ('billing pause interrupted',))
                transient_retries = 0
            except ProviderTransientError as exc:
                if transient_retries >= 3:
                    session.emit('provider_retry_exhausted', retries=transient_retries)
                    raise
                import time
                delay = min(30, 2 ** (transient_retries + 1))
                transient_retries += 1
                session.emit('provider_retry_scheduled', attempt=transient_retries, delay_seconds=delay, error_type=type(exc).__name__)
                for _ in range(delay):
                    if interrupt():
                        return SamplingResult.stop('cancellation_requested', ('provider retry interrupted',))
                    time.sleep(1)
            except ModelImageInputError as exc:
                failure = session.hook('ModelInputFailure', messages=model_context.messages, error=str(exc))
                retry_messages = failure.data.get('retry_messages') if failure is not None else None
                if not isinstance(retry_messages, list):
                    session.emit('model_input_rejected', turn=turn, reason=str(exc))
                    return SamplingResult.stop('model_input_rejected', (str(exc),), session.state.final_text)
                model_context = ModelContext(retry_messages, estimate_tokens(retry_messages), model_context.observation_refs)
                session.model_context = model_context
            except ModelContextLengthError as exc:
                max_retries = int(_policy_get(self.spec.recovery, 'max_context_retries', 3))
                if context_retries >= max_retries:
                    reason = f'provider context rejection after {context_retries} retries: {exc}'
                    session.emit('context_capacity_exceeded', turn=turn, reason=reason)
                    return SamplingResult.stop('context_capacity_exceeded', (reason,), session.state.final_text)
                try:
                    model_context, reduction = session.context_manager.reduce_after_provider_rejection(model_context, schemas, session.events, token_gap=exc.token_gap)
                except RequestCapacityError as capacity_error:
                    session.emit('context_capacity_exceeded', turn=turn, reason=str(capacity_error))
                    return SamplingResult.stop('context_capacity_exceeded', (str(capacity_error),), session.state.final_text)
                context_retries += 1
                session.model_context = model_context
                session.emit('context_compaction', turn=turn, phase=orchestration.phase, **{key: value for key, value in reduction.items() if key != 'type'})
            except Exception as exc:
                session.emit('run_exception', turn=turn, phase=orchestration.phase, exception_type=type(exc).__name__, message=str(exc)[:4000])
                raise
        content = _text(response.get('content'))
        calls = response.get('tool_calls') or []
        finish_reason = str(response.get('_finish_reason') or '')
        reasoning_content = _text(response.get('reasoning_content'))
        response_usage = response.get('_usage')
        usage = dict(response_usage) if isinstance(response_usage, dict) else _last_usage(model)
        session.context_manager.record_model_usage(usage)
        usage_before = session.usage.dictionary()
        if usage:
            session.usage.add(usage)
        else:
            snapshot = _usage_snapshot(model)
            if snapshot:
                session.usage.update(snapshot)
        usage_delta = {name: max(0, value - usage_before.get(name, 0)) for name, value in session.usage.dictionary().items()}
        if session.task_usage is not None and any(usage_delta.values()):
            pricing = getattr(model, 'pricing', None)
            cost_usd = pricing.request_cost(usage_delta) if isinstance(pricing, ModelPricing) else 0.0
            task_usage = session.task_usage.add(usage_delta, cost_usd=cost_usd)
            session.emit('usage_recorded', turn=turn, phase=orchestration.phase, request_usage=usage_delta, request_cost_usd=cost_usd, task_usage=task_usage, agent_role=session.agent_role)
        output_limit = int(getattr(model, 'max_output_tokens_per_call', 0) or 0)
        output_tokens = int(usage.get('output_tokens', 0) or 0)
        hit_output_limit = bool(output_limit and output_tokens >= output_limit)
        truncated = finish_reason.lower() in {'length', 'max_tokens', 'max_output_tokens'}
        session.emit('model', turn=turn, phase=orchestration.phase, content=content[:4000], content_empty=not content.strip(), finish_reason=finish_reason or None, reasoning_content_chars=len(reasoning_content), hit_output_limit=hit_output_limit, tool_call_count=len(calls), tool_names=[str((call.get('function') or {}).get('name', '')) for call in calls], usage=usage)
        session.emit('model_responded', turn=turn, phase=orchestration.phase, finish_reason=finish_reason or None, tool_call_count=len(calls), content_empty=not content.strip())
        session.hook('AfterModel', turn=turn, usage=usage, tool_call_count=len(calls), finish_reason=finish_reason or None)
        if interrupt():
            return SamplingResult.stop('cancellation_requested', ('cancellation requested',), 'Cancellation requested after the active model call returned.')
        incomplete = bool(truncated or hit_output_limit or (not calls and (not content.strip())))
        if incomplete:
            reason = 'output_truncated' if truncated or hit_output_limit else 'empty_response'
            observation_ref = session.context_manager.observations.put('model_incomplete_response', {'content': content, 'tool_calls': calls, 'reasoning_content': reasoning_content}, summary=reason)
            session.emit('model_response_retry', turn=turn, phase=orchestration.phase, reason=reason, finish_reason=finish_reason or None, output_tokens=output_tokens, output_limit=output_limit or None, reasoning_content_chars=len(reasoning_content), partial_content_chars=len(content), discarded_tool_calls=len(calls), observation_ref=observation_ref)
            record_incomplete = getattr(session.recovery, 'record_incomplete_response', None)
            if callable(record_incomplete) and (not record_incomplete()):
                return SamplingResult.stop('model_response_incomplete', ('model repeatedly returned incomplete responses',), session.state.final_text)
            session.messages.append({'role': 'system', 'content': 'The previous model response was cut off. Any tool calls in it were discarded and were not executed. Continue from the current task state; reissue a complete tool call if needed. Do not repeat the full analysis.' if reason == 'output_truncated' else 'The previous response was empty. Continue the task by making one concrete tool call, or provide a substantive final answer only if the work is complete.'})
            return SamplingResult(retry=True)
        record_complete = getattr(session.recovery, 'record_complete_response', None)
        if callable(record_complete):
            record_complete()
        return SamplingResult(response=response)
