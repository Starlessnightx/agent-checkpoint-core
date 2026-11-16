from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from .ledger import TaskLedger
from .builder import ContextBuilder, ModelContext
from .policy import ContextPolicy
from .store import ContextStore, ObservationStore
from .prefix import PrefixObserver
from harness.media.images import has_images, image_tokens, is_tool_attachment, text_projection


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Return a conservative dependency-free estimate for chat messages."""
    serialized = json.dumps(text_projection(messages), ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(serialized) + 2) // 3) + image_tokens(messages)


class RequestCapacityError(ValueError):
    """The required request prefix alone exceeds the configured input capacity."""


def _request_tokens(
    messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]] | None
) -> int:
    if not tool_schemas:
        return estimate_tokens(messages)
    return estimate_tokens(messages + [{"role": "tool_schemas", "content": tool_schemas}])


class ContextManager:
    def __init__(
        self,
        policy: Any,
        workspace: Path,
        task: str,
        *,
        max_context_tokens: int | None,
        archive_directory: Path | None = None,
    ):
        self.policy = policy
        self.workspace = workspace.resolve()
        self.max_context_tokens = max_context_tokens
        self.archive_directory = archive_directory.resolve() if archive_directory else None
        self.ledger = TaskLedger(task)
        self.store = ContextStore(task_contract=task)
        self.observations = ObservationStore(
            self.archive_directory or self.workspace / ".harness" / "observations"
        )
        action_limit = _get(policy, "max_recent_actions", None)
        if action_limit is None:
            action_limit = _get(policy, "keep_latest", None)
        self.builder = ContextBuilder(ContextPolicy(
            max_recent_actions=(
                None if action_limit is None else max(0, int(action_limit))
            ),
            max_facts=24,
        ))
        self.last_model_view: dict[str, Any] = {}
        self.last_metrics: dict[str, int] = {}
        self._observed_event_ids: set[str] = set()
        self._ingested_event_ids: set[str] = set()
        self.compaction_count = 0
        self.summarizer: Callable[[list[dict[str, Any]], str], str] | None = None
        self._checkpoint: dict[str, Any] | None = None
        self._prefix_count = 0
        self._prefix_hash = ""
        self._last_raw_messages: list[dict[str, Any]] = []
        self._request_growth: list[int] = []
        self._last_request_size = 0
        self._summary_failure: str | None = None
        self._summary_failures = 0
        self._output_sizes: list[int] = []
        self._estimation_error = 0
        self._microcompacted_contents: set[str] = set()
        self._prefix_observer = PrefixObserver()
        self.last_prefix_metrics: dict[str, Any] = {}

    def checkpoint_state(self) -> dict[str, Any]:
        """Versioned plain data; raw observations are captured separately."""
        from dataclasses import asdict
        from copy import deepcopy
        return deepcopy({
            "schema_version": 1, "workspace": str(self.workspace),
            "archive_directory": str(self.archive_directory) if self.archive_directory else None,
            "max_context_tokens": self.max_context_tokens,
            "policy": self.policy,
            "ledger_task": self.ledger.task, "store": self.store.snapshot(),
            "builder_policy": asdict(self.builder.policy),
            "observations": {"root": str(self.observations.root),
                             "fallbacks": [str(path) for path in self.observations.fallbacks]},
            "last_model_view": self.last_model_view, "last_metrics": self.last_metrics,
            "observed_event_ids": sorted(self._observed_event_ids),
            "ingested_event_ids": sorted(self._ingested_event_ids),
            "compaction_count": self.compaction_count, "checkpoint": self._checkpoint,
            "prefix_count": self._prefix_count, "prefix_hash": self._prefix_hash,
            "last_raw_messages": self._last_raw_messages, "request_growth": self._request_growth,
            "last_request_size": self._last_request_size, "summary_failure": self._summary_failure,
            "summary_failures": self._summary_failures, "output_sizes": self._output_sizes,
            "estimation_error": self._estimation_error,
            "microcompacted_contents": sorted(self._microcompacted_contents),
            "prefix_observer": self._prefix_observer.checkpoint_state(),
            "last_prefix_metrics": self.last_prefix_metrics,
            "summarizer_enabled": self.summarizer is not None,
            "summarizer_implementation": (type(self.summarizer).__module__ + ":" + type(self.summarizer).__qualname__
                                          if self.summarizer is not None else None),
        })

    def record_model_usage(self, usage: dict[str, Any]) -> None:
        output = max(0, int(usage.get("output_tokens", 0) or 0))
        self._output_sizes = [*self._output_sizes[-31:], output]
        measured = max(0, int(usage.get("input_tokens", 0) or 0))
        if measured:
            estimate = self.last_metrics.get("request_tokens", measured)
            self._estimation_error = max(0, measured - estimate)

    def restore_checkpoint_state(self, value: dict[str, Any], *, observations: ObservationStore) -> None:
        """Restore data into a freshly assembled compatible context component.

        Observation archives must already be restored by the caller. This API
        neither accesses archived host paths nor imports executable policies.
        """
        from .state import restore_context
        restore_context(self, value, observations=observations)

    def _add_supplement(
        self, checkpoint: dict[str, Any], messages: list[dict[str, Any]],
    ) -> None:
        reference = self.observations.put("context_supplement", messages)
        self.store.add_observation_ref(reference)
        original = str(checkpoint["content"])
        evidence, separator, summary = original.partition("\n\nConversation continuation:\n")
        previous = self.observations.put("context_summary", {"content": summary})
        self.store.add_observation_ref(previous)
        # Preserve the deterministic task and verification ledger in full.
        # Both shortened continuation parts point to their complete evidence.
        checkpoint["content"] = (
            evidence + separator + summary[:len(summary) // 3] + "\n"
            + _continuation_fallback(messages, len(summary) // 3)
            + f"\n[prior summary: {previous}; additional history: {reference}]"
        )

    def _active_history(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if (
            self._checkpoint is not None
            and len(messages) >= self._prefix_count
            and _history_hash(messages[:self._prefix_count]) == self._prefix_hash
        ):
            first = int(_get(self.policy, "keep_first", 2))
            return messages[:first] + [self._checkpoint] + messages[self._prefix_count:]
        return messages

    def _commit_checkpoint(
        self, messages: list[dict[str, Any]], recent: list[dict[str, Any]],
        checkpoint: dict[str, Any],
    ) -> None:
        self._prefix_count = len(messages) - len(recent)
        self._prefix_hash = _history_hash(messages[:self._prefix_count])
        self._checkpoint = checkpoint

    def _checkpoint_message(
        self, omitted: list[dict[str, Any]], events: list[dict[str, Any]], archive: str,
        *, summary: str | None = None,
    ) -> dict[str, Any]:
        evidence = self.ledger.checkpoint(events).prompt(archive)
        if self._checkpoint is not None and self._checkpoint not in omitted:
            omitted = [self._checkpoint, *omitted]
        configured_summary_limit = _get(self.policy, "max_summary_chars", None)
        max_chars = max(256, estimate_tokens(omitted))
        if self.max_context_tokens is not None:
            max_chars = min(max_chars, max(256, self.max_context_tokens * 3 // 8))
        if configured_summary_limit is not None:
            max_chars = min(max_chars, int(configured_summary_limit))
        supplied_summary = summary is not None
        summary = summary or _continuation_fallback(omitted, max_chars)
        if not supplied_summary and self.summarizer is not None and self._summary_failures < 3:
            try:
                summary = self.summarizer(text_projection(omitted), evidence)
                # Retain the entire model-produced summary in observations.
                reference = self.observations.put("context_summary", {"content": summary})
                self.store.add_observation_ref(reference)
                summary = summary[:max_chars] + f"\n[full summary observation: {reference}]"
                self._summary_failures = 0
            except Exception as exc:
                self._summary_failure = f"{type(exc).__name__}: {exc}"
                self._summary_failures += 1
        return {"role": "system", "content": evidence + "\n\nConversation continuation:\n" + summary}

    def build_model_context(
        self,
        messages: list[dict[str, Any]],
        events: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> tuple[ModelContext, dict[str, Any] | None]:
        """Project raw session state into the bounded messages sent to the model."""
        self._last_raw_messages = list(messages)
        prepared, event = self.prepare(messages, events, tool_schemas)
        first_count = min(len(prepared), int(_get(self.policy, "keep_first", 2)))
        if self._checkpoint is not None and len(prepared) > first_count and prepared[first_count] is self._checkpoint:
            first_count += 1
        fixed = prepared[:first_count]
        groups = _atomic_groups(prepared[first_count:])
        group_limit = self.builder.policy.max_recent_actions
        recent_groups = (
            groups
            if group_limit is None
            else ([] if group_limit == 0 else groups[-group_limit:])
        )
        required_image = _required_image_start(groups)
        if required_image is not None and len(recent_groups) < len(groups) - required_image:
            recent_groups = groups[required_image:]
        recent = [message for group in recent_groups for message in group]
        self.last_model_view = self.builder.build(self.store)
        # Keep structured state available to observers, without injecting a
        # changing system message into every model request.
        model_messages = fixed + recent
        request_before = _request_tokens(model_messages, tool_schemas)
        omitted_groups: list[list[dict[str, Any]]] = []
        if self.max_context_tokens is not None:
            while recent_groups and _request_tokens(model_messages, tool_schemas) > self.max_context_tokens:
                _require_omittable(recent_groups)
                omitted_groups.append(recent_groups.pop(0))
                recent = [message for group in recent_groups for message in group]
                model_messages = fixed + recent
            if _request_tokens(model_messages, tool_schemas) > self.max_context_tokens:
                raise RequestCapacityError(
                    "system instructions, task contract, structured state, and active tools "
                    "exceed the configured model input capacity"
                )
        if omitted_groups:
            assert self.max_context_tokens is not None
            self.compaction_count += 1
            archive_path = self._archive_path(self.compaction_count)
            archived = self._archive(
                archive_path,
                [message for group in omitted_groups for message in group],
                events,
                estimated_tokens=estimate_tokens(
                    [message for group in omitted_groups for message in group]
                ),
            )
            first_omitted_count = len(omitted_groups)
            checkpoint = self._checkpoint_message(
                [message for group in omitted_groups for message in group],
                events, self._archive_reference(archive_path),
            )
            fixed = prepared[:int(_get(self.policy, "keep_first", 2))] + [checkpoint]
            model_messages = fixed + recent
            while recent_groups and _request_tokens(model_messages, tool_schemas) > self.max_context_tokens:
                _require_omittable(recent_groups)
                omitted_groups.append(recent_groups.pop(0))
                recent = [message for group in recent_groups for message in group]
                model_messages = fixed + recent
            if len(omitted_groups) > first_omitted_count:
                supplementary = [message for group in omitted_groups[first_omitted_count:] for message in group]
                self._add_supplement(checkpoint, supplementary)
            if _request_tokens(model_messages, tool_schemas) > self.max_context_tokens:
                raise RequestCapacityError("checkpoint and required request state exceed input capacity")
            # Archive the complete omitted prefix, including groups removed to
            # make room for the continuation summary.
            archived = self._archive(
                archive_path, [message for group in omitted_groups for message in group],
                events, estimated_tokens=estimate_tokens([message for group in omitted_groups for message in group]),
            )
            self._commit_checkpoint(messages, recent, checkpoint)
            projection = {
                "reason": "request_capacity",
                "estimated_request_tokens_before": request_before,
                "estimated_request_tokens_after": _request_tokens(model_messages, tool_schemas),
                "input_capacity": self.max_context_tokens,
                "omitted_atomic_groups": len(omitted_groups),
                "archive": self._archive_reference(archive_path),
                "archive_sha256": archived["messages_sha256"],
                "summary_failure": self._summary_failure,
            }
            if event is None:
                event = {"type": "context_compaction", **projection}
            else:
                event["request_projection"] = projection
        context = ModelContext(
            messages=model_messages,
            estimated_tokens=estimate_tokens(model_messages),
            observation_refs=list(self.store.observation_refs),
        )
        self.last_metrics = {
            "context_tokens": context.estimated_tokens,
            "observation_count": len(context.observation_refs),
            "raw_message_count": len(messages),
            "model_message_count": len(model_messages),
            "request_tokens": _request_tokens(model_messages, tool_schemas),
        }
        request_size = self.last_metrics["request_tokens"]
        if self._last_request_size and event is None:
            self._request_growth.append(max(0, request_size - self._last_request_size))
            self._request_growth = self._request_growth[-32:]
        self._last_request_size = request_size
        self.last_prefix_metrics = self._prefix_observer.observe(model_messages, tool_schemas)
        return context, event

    def reduce_after_provider_rejection(
        self,
        context: ModelContext,
        tool_schemas: list[dict[str, Any]],
        events: list[dict[str, Any]],
        *,
        token_gap: int | None = None,
    ) -> tuple[ModelContext, dict[str, Any]]:
        """Drop older atomic groups after a provider context-length rejection."""
        before = _request_tokens(context.messages, tool_schemas)
        trimmed, references = self._trim_runtime_messages(context.messages, pressure=True)
        micro_size = _request_tokens(trimmed, tool_schemas)
        if references and micro_size < before:
            reduced = ModelContext(trimmed, estimate_tokens(trimmed), list(self.store.observation_refs))
            self.last_metrics.update({"context_tokens": reduced.estimated_tokens, "request_tokens": micro_size})
            return reduced, {
                "type": "context_compaction", "reason": ["provider_context_limit", "tool_microcompaction"],
                "estimated_request_tokens_before": before,
                "estimated_request_tokens_after": micro_size,
                "tool_output_archives": references,
            }
        # The synthetic suffix is not a raw action group or an archived turn.
        # Excluding it also keeps checkpoint cursors aligned with raw messages.
        suffix = context.messages[-1:]
        if not (suffix and suffix[0].get("role") == "system" and
                str(suffix[0].get("content", "")).startswith("Structured model view")):
            suffix = []
        conversation = context.messages[:-1] if suffix else context.messages
        fixed_count = min(len(conversation), int(_get(self.policy, "keep_first", 2)))
        while fixed_count < len(conversation) and conversation[fixed_count].get("role") == "system":
            fixed_count += 1
        fixed = conversation[:fixed_count]
        groups = _atomic_groups(conversation[fixed_count:])
        if not groups:
            raise RequestCapacityError(
                "provider rejected the context and no older action groups remain to compact"
            )
        target_reduction = token_gap or max(1, before // 4)
        omit_count = 0
        removed_tokens = 0
        required_image = _required_image_start(groups)
        for index, group in enumerate(groups):
            if index == required_image:
                if omit_count == 0:
                    raise RequestCapacityError("Provider rejected the required image input; choose smaller samples or a model with sufficient image/context capacity. Images were not silently omitted.")
                break
            omit_count += 1
            removed_tokens += estimate_tokens(group)
            if removed_tokens >= target_reduction:
                break
        omitted = [message for group in groups[:omit_count] for message in group]
        retained = [message for group in groups[omit_count:] for message in group]
        self.compaction_count += 1
        archive_path = self._archive_path(self.compaction_count)
        archived = self._archive(
            archive_path, omitted, events,
            estimated_tokens=estimate_tokens(omitted),
        )
        continuation = [*([self._checkpoint] if self._checkpoint else []), *omitted]
        checkpoint = {"role": "system", "content": self.ledger.checkpoint(events).prompt(self._archive_reference(archive_path)) + "\n" + _continuation_fallback(continuation, max(128, before // 4))}
        # The retry carries a compact evidence reference as well as the suffix.
        first = int(_get(self.policy, "keep_first", 2))
        structured = suffix or [message for message in fixed[first:] if str(message.get("content", "")).startswith("Structured model view")]
        reduced_messages = fixed[:first] + [checkpoint] + retained + structured
        # Unknown providers may reject the summary itself. Keep its evidence in
        # the Observation Store and use the archive pointer on the retry.
        if _request_tokens(reduced_messages, tool_schemas) >= before:
            checkpoint = {"role": "system", "content": f"Earlier history: {self._archive_reference(archive_path)}"}
            reduced_messages = fixed[:first] + [checkpoint] + retained + structured
        self._commit_checkpoint(self._last_raw_messages, retained, checkpoint)
        reduced = ModelContext(
            messages=reduced_messages,
            estimated_tokens=estimate_tokens(reduced_messages),
            observation_refs=list(context.observation_refs),
        )
        after = _request_tokens(reduced_messages, tool_schemas)
        # Learn a conservative effective input capacity for later turns. This
        # is an observed planning bound, not a claimed provider model limit.
        self.max_context_tokens = min(self.max_context_tokens or after, after)
        self.last_metrics.update({
            "context_tokens": reduced.estimated_tokens,
            "request_tokens": after,
            "model_message_count": len(reduced_messages),
        })
        return reduced, {
            "type": "context_compaction",
            "reason": ["provider_context_limit"],
            "estimated_request_tokens_before": before,
            "estimated_request_tokens_after": after,
            "omitted_atomic_groups": omit_count,
            "archive": self._archive_reference(archive_path),
            "archive_sha256": archived["messages_sha256"],
            "token_gap": token_gap,
            "observed_input_capacity": self.max_context_tokens,
        }

    def prepare(
        self,
        messages: list[dict[str, Any]],
        events: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        fresh_events = []
        self._summary_failure = None
        for event in events:
            event_id = str(event.get("event_id", ""))
            if event_id and event_id not in self._ingested_event_ids:
                fresh_events.append(event)
                self._ingested_event_ids.add(event_id)
        self.store.ingest(fresh_events)
        for event in events:
            event_id = str(event.get("event_id", ""))
            if event_id and event_id not in self._observed_event_ids:
                self.observations.put(str(event.get("type", "event")), event,
                                      summary=str(event.get("output", ""))[:240])
                self._observed_event_ids.add(event_id)
        self.last_model_view = self.builder.build(self.store)
        active = self._active_history(messages)
        structured_tokens = estimate_tokens([{"role": "system", "content": self.builder.prompt(self.store)}])
        untrimmed_tokens = _request_tokens(active, tool_schemas) + structured_tokens
        pressure = (
            self.max_context_tokens is not None
            and untrimmed_tokens + max(self._request_growth, default=untrimmed_tokens // 20)
            >= self.max_context_tokens
        )
        previous_microcompact_count = len(self._microcompacted_contents)
        trimmed, tool_output_archives = self._trim_runtime_messages(active, pressure=pressure)
        estimated = _request_tokens(trimmed, tool_schemas) + structured_tokens
        self.last_metrics = {
            "context_tokens": estimated,
            "observation_count": len(self._observed_event_ids),
        }
        ratio = (
            estimated / self.max_context_tokens
            if self.max_context_tokens is not None else None
        )
        raw_max_messages = _get(self.policy, "max_messages", None)
        max_messages = (
            int(raw_max_messages) if raw_max_messages is not None else None
        )
        reasons = []
        if max_messages is not None and len(trimmed) > max_messages:
            reasons.append("message_limit")
        raw_compact_ratio = _get(self.policy, "compact_at_ratio", None)
        # Reserve a predicted next turn and estimation overhead. The amount
        # adapts to observed growth; legacy ratio overrides remain supported.
        growth = max(self._request_growth, default=0)
        predicted_output = max(self._output_sizes, default=0)
        headroom = max(growth, predicted_output, estimated // 20) + self._estimation_error
        compact_ratio = float(raw_compact_ratio) if raw_compact_ratio is not None else (
            max(0.1, 1 - headroom / self.max_context_tokens)
            if self.max_context_tokens else 1.0
        )
        if ratio is not None and ratio >= compact_ratio:
            reasons.append("token_ratio")
        if not reasons:
            if (
                pressure and tool_output_archives and estimated < untrimmed_tokens
                and len(self._microcompacted_contents) > previous_microcompact_count
            ):
                return trimmed, {
                    "type": "context_compaction", "reason": ["tool_microcompaction"],
                    "estimated_tokens_before": untrimmed_tokens,
                    "estimated_tokens_after": estimated,
                    "tool_output_archives": tool_output_archives,
                }
            return trimmed, None

        raw_emergency_ratio = _get(
            self.policy, "emergency_compact_at_ratio", None
        )
        emergency = (
            ratio is not None
            and raw_emergency_ratio is not None
            and ratio >= float(raw_emergency_ratio)
        )
        first_count = min(len(trimmed), int(_get(self.policy, "keep_first", 2)))
        raw_latest_limit = _get(self.policy, "keep_latest", None)
        latest_limit = (
            None if raw_latest_limit is None else max(1, int(raw_latest_limit))
        )
        if emergency and latest_limit is not None:
            latest_limit = max(8, latest_limit // 2)
        raw_target_ratio = _get(self.policy, "compact_to_ratio", None)
        target_ratio = (
            float(raw_target_ratio) if raw_target_ratio is not None else compact_ratio
        )
        if emergency:
            target_ratio = min(target_ratio, 0.45)
        target_tokens = (
            max(1, int(self.max_context_tokens * target_ratio) - structured_tokens - (_request_tokens([], tool_schemas) if tool_schemas else 0))
            if self.max_context_tokens is not None else None
        )

        sequence = self.compaction_count + 1
        archive_path = self._archive_path(sequence)
        archive_ref = self._archive_reference(archive_path)
        checkpoint = self.ledger.checkpoint(events)
        checkpoint_message = {
            "role": "system",
            "content": checkpoint.prompt(archive_ref),
        }
        fixed = trimmed[:first_count] + [checkpoint_message]
        groups = _atomic_groups(trimmed[first_count:])
        recent_message_limit = latest_limit
        if max_messages is not None:
            available = max(1, max_messages - first_count - 1)
            recent_message_limit = (
                available
                if recent_message_limit is None
                else min(recent_message_limit, available)
            )
        recent_groups = _select_recent_groups(
            fixed,
            groups,
            max_messages=recent_message_limit,
            target_tokens=target_tokens,
        )
        retained_group_count = len(recent_groups)
        omitted_group_count = len(groups) - retained_group_count
        if omitted_group_count <= 0:
            return trimmed, None

        omitted_groups = groups[:omitted_group_count]
        omitted = [message for group in omitted_groups for message in group]
        recent = [message for group in recent_groups for message in group]
        checkpoint_message = self._checkpoint_message(omitted, events, archive_ref)
        fixed = trimmed[:first_count] + [checkpoint_message]
        initial_omitted_count = len(omitted)
        # Summary size is measured before committing the new view.
        while len(recent_groups) > 1 and target_tokens is not None and estimate_tokens(fixed + recent) > target_tokens:
            if _required_image_start(recent_groups) == 0:
                break
            omitted.extend(recent_groups.pop(0))
            recent = [message for group in recent_groups for message in group]
        if len(omitted) > initial_omitted_count:
            additional = omitted[initial_omitted_count:]
            self._add_supplement(checkpoint_message, additional)
        omitted_group_count = len(groups) - len(recent_groups)
        retained_group_count = len(recent_groups)
        compacted = fixed + recent
        self._commit_checkpoint(messages, recent, checkpoint_message)
        self.compaction_count = sequence
        archive_metadata = self._archive(
            archive_path,
            omitted,
            events,
            estimated_tokens=estimate_tokens(omitted),
        )
        after = estimate_tokens(compacted)
        event = {
            "type": "context_compaction",
            "sequence": sequence,
            "reason": reasons,
            "trigger_policy": {
                "input_capacity": self.max_context_tokens,
                "compact_ratio": compact_ratio,
                "message_limit": max_messages,
            },
            "estimated_tokens_before": estimated,
            "estimated_tokens_after": after,
            "target_tokens": target_tokens,
            "target_met": target_tokens is None or after <= target_tokens,
            "emergency": emergency,
            "omitted_messages": len(omitted),
            "omitted_atomic_groups": omitted_group_count,
            "retained_atomic_groups": retained_group_count,
            "archive": archive_ref,
            "archive_sha256": archive_metadata["messages_sha256"],
            "tool_output_archives": tool_output_archives,
            "checkpoint": checkpoint.dictionary(),
            "summary_failure": self._summary_failure,
            "model_view": self.last_model_view,
            "observation_refs": list(self.store.observation_refs),
        }
        return compacted, event

    def _archive_path(self, sequence: int) -> Path:
        directory = self.archive_directory or self.workspace / ".harness" / "context"
        return directory / f"checkpoint-{sequence:04d}.json"

    def _archive_reference(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)

    def _archive(
        self,
        path: Path,
        messages: list[dict[str, Any]],
        events: list[dict[str, Any]],
        *,
        estimated_tokens: int,
    ) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        canonical = json.dumps(
            messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
        metadata = {
            "schema_version": 1,
            "sequence": self.compaction_count,
            "message_count": len(messages),
            "estimated_tokens": estimated_tokens,
            "messages_sha256": sha256(canonical.encode("utf-8")).hexdigest(),
            "event_count_at_archive": len(events),
        }
        path.write_text(
            json.dumps({"metadata": metadata, "messages": messages}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return metadata

    def _trim_runtime_messages(
        self, messages: list[dict[str, Any]], *, pressure: bool = False,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        limit = int(_get(self.policy, "max_tool_output_chars", 16_000))
        result: list[dict[str, Any]] = []
        archives: list[str] = []
        for message in messages:
            content = str(message.get("content", ""))
            content_hash = sha256(content.encode()).hexdigest()
            should_trim = (
                not _get(self.policy, "microcompact_only_under_pressure", False)
                or pressure or content_hash in self._microcompacted_contents
            )
            if message.get("role") != "tool" or len(content) <= limit or not should_trim:
                result.append(message)
                continue
            reference = self._archive_tool_output(content)
            self._microcompacted_contents.add(content_hash)
            archives.append(reference)
            result.append(_trim_message(message, limit, reference))
        return result, list(dict.fromkeys(archives))

    def _archive_tool_output(self, content: str) -> str:
        lines = content.splitlines()
        summary = (lines[0] if lines else content)[:240]
        observation_id = self.observations.put(
            "tool_output", {"content": content}, summary=summary
        )
        self.store.add_observation_ref(observation_id)
        return observation_id




def _atomic_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group an assistant Tool call with every consecutive Tool result."""
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if is_tool_attachment(message) and groups:
            # Middleware context may separate the last tool batch and its images.
            batch = next((i for i in range(len(groups) - 1, -1, -1) if groups[i][0].get("role") == "assistant" and groups[i][0].get("tool_calls")), None)
            if batch is not None:
                groups[batch].extend([item for group in groups[batch + 1:] for item in group])
                groups[batch].append(message)
                del groups[batch + 1:]
                index += 1
                continue
        group = [message]
        index += 1
        if message.get("role") == "assistant" and message.get("tool_calls"):
            while index < len(messages) and messages[index].get("role") == "tool":
                group.append(messages[index])
                index += 1
        groups.append(group)
    return groups


def _require_omittable(groups: list[list[dict[str, Any]]]) -> None:
    if _required_image_start(groups) == 0:
        raise RequestCapacityError("Required image input exceeds the estimated request capacity; choose smaller samples or a vision model with sufficient capacity. Images were not silently omitted.")


def _required_image_start(groups: list[list[dict[str, Any]]]) -> int | None:
    """Images after the last model response still require first delivery."""
    required = None
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if any(has_images(message) for message in group):
            required = index
        if any(message.get("role") == "assistant" for message in group):
            break
    return required


def _select_recent_groups(
    fixed: list[dict[str, Any]],
    groups: list[list[dict[str, Any]]],
    *,
    max_messages: int | None,
    target_tokens: int | None,
) -> list[list[dict[str, Any]]]:
    selected: list[list[dict[str, Any]]] = []
    selected_count = 0
    required_image = _required_image_start(groups)
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        candidate_count = selected_count + len(group)
        candidate_groups = [group, *selected]
        candidate = fixed + [message for value in candidate_groups for message in value]
        exceeds_messages = (
            max_messages is not None and candidate_count > max_messages
        )
        exceeds_tokens = target_tokens is not None and estimate_tokens(candidate) > target_tokens
        if selected and (exceeds_messages or exceeds_tokens) and (required_image is None or index < required_image):
            break
        selected = candidate_groups
        selected_count = candidate_count
    return selected


def _trim_message(
    message: dict[str, Any], max_chars: int, archive_ref: str = ""
) -> dict[str, Any]:
    if message.get("role") != "tool":
        return message
    result = dict(message)
    content = str(result.get("content", ""))
    if len(content) > max_chars:
        head = max_chars // 2
        tail = max_chars - head
        result["content"] = (
            content[:head]
            + f"\n[... {len(content) - max_chars} tool-output characters omitted from active context ...]\n"
            + content[-tail:]
            + (f"\n[full tool output observation: {archive_ref}]" if archive_ref else "")
        )
    return result


def _action_summary(messages: list[dict[str, Any]], max_chars: int) -> str:
    lines = ["Earlier workspace actions omitted from context:"]
    calls: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            calls[str(call.get("id", ""))] = str(function.get("name", "tool"))
        if message.get("role") == "tool":
            name = calls.get(str(message.get("tool_call_id", "")), "tool")
            output = str(message.get("content", "")).splitlines()
            lines.append(f"- {name}: {(output[0] if output else '')[:240]}")
    return "\n".join(lines)[:max_chars]


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _history_hash(messages: list[dict[str, Any]]) -> str:
    return sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _continuation_fallback(messages: list[dict[str, Any]], max_chars: int) -> str:
    lines = []
    for message in text_projection(messages):
        content = str(message.get("content") or "")
        if content:
            lines.append(f"{message.get('role', 'unknown')}: {content}")
        if message.get("tool_calls"):
            lines.append(json.dumps(message["tool_calls"], ensure_ascii=False))
    return "\n".join(lines)[-max_chars:]
