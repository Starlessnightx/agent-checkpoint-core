from __future__ import annotations

import base64
import io
import json
import struct
import tempfile
import unittest
import urllib.error
import zlib
from pathlib import Path
from unittest.mock import patch

from tests.runtime_support import sample_tools, install_planning
from harness.core import AgentRuntime
from harness.context.manager import ContextManager, RequestCapacityError, _atomic_groups, estimate_tokens
from harness.context.policy import ContextPolicy
from harness.context.summarizer import ModelSummarizer
from harness.executor import LocalExecutor
from harness.media.images import ATTACHMENT_PREFIX, ModelImageInputError, has_images, image_info, text_projection
from harness.model import OpenAICompatibleModel, _image_setting
from harness.model_capabilities import capabilities_from_payload
from harness.permissions import PermissionLevel, PermissionPolicy
from harness.spec import build_spec
from harness.subagents.manager import AgentSpec
from harness.subagents.registry import default_subagents
from harness.tools.registry import ToolRegistry, default_tool_specs

ROOT = Path(__file__).resolve().parents[1]


def png(width=2, height=2):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * width) * height)) + chunk(b"IEND", b"")


def call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def attachment_message(url=None):
    return {"role": "user", "content": [
        {"type": "text", "text": ATTACHMENT_PREFIX + " snapshot=.harness/artifacts/images/hash"},
        {"type": "image_url", "image_url": {"url": url or "data:image/png;base64," + base64.b64encode(png()).decode(), "detail": "auto"}},
    ]}


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"role": "assistant", "content": "red"}}], "usage": {"prompt_tokens": 20, "completion_tokens": 1}}).encode()


class VisionModel:
    supports_image_input = True

    def __init__(self, reject=False):
        self.requests = []
        self.reject = reject

    def complete(self, messages, tools, temperature=0):
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            return {"role": "assistant", "content": "", "tool_calls": [call("i1", "view_image", {"path": "sample.png"}), call("t1", "list_directory", {"path": "."})]}
        if self.reject and any(has_images(message) for message in messages):
            raise ModelImageInputError("image input unsupported")
        return {"role": "assistant", "content": "Continue inspecting evidence."}


class NativeImagesTest(unittest.TestCase):
    def registry(self, root, permission=None):
        registry = ToolRegistry(root, default_tool_specs(), permission_policy=permission)
        registry.context.executor.write_file(root / "sample.png", png())
        registry.enable_skill("filesystem")
        return registry

    def test_read_only_image_result_contains_snapshot_not_deliverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            registry = self.registry(root, PermissionPolicy(PermissionLevel.READ_ONLY))
            result = registry.execute("view_image", {"path": "sample.png"})
            self.assertTrue(result.ok, result.output)
            self.assertIsNone(result.state_delta)
            self.assertEqual(result.artifacts, ())
            image = result.images[0]
            self.assertEqual((image.width, image.height, image.mime_type), (2, 2, "image/png"))
            self.assertEqual(registry.context.executor.read_file(root / image.snapshot).content, png())
            self.assertNotIn("base64", json.dumps(result.structured))
            self.assertNotIn("base64", result.output)
            registry.context.executor.write_file(root / "sample.png", png(3, 3))
            self.assertEqual(registry.context.executor.read_file(root / image.snapshot).content, png())
            replay = registry.execute("view_image", {"path": image.snapshot})
            self.assertTrue(replay.ok)
            self.assertEqual(replay.images[0].data_url, image.data_url)

    def test_paths_and_symlinks_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp).resolve()
            registry = self.registry(root)
            LocalExecutor(Path(outside)).write_file(Path(outside) / "secret.png", png())
            (root / "escape.png").symlink_to(Path(outside) / "secret.png")
            for path in ("../outside.png", "escape.png", str(Path(outside) / "secret.png")):
                result = registry.execute("view_image", {"path": path})
                self.assertFalse(result.ok)
                self.assertEqual(result.failure_kind, "permission")

    def test_known_unsupported_model_fails_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.registry(Path(tmp).resolve())
            registry.context.image_input_support = lambda: False
            with patch.object(registry.context.executor, "read_file", side_effect=AssertionError("must not read")):
                result = registry.execute("view_image", {"path": "sample.png"})
            self.assertEqual(result.failure_kind, "unsupported_image_input")

    def test_missing_invalid_and_invalid_detail_are_explicit_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            registry = self.registry(root)
            registry.context.executor.write_file(root / "not.png", b"<svg>text</svg>")
            for arguments, kind in (({"path": "missing.png"}, "not_found"), ({"path": "not.png"}, "invalid_image"), ({"path": "sample.png", "detail": "original"}, "invalid_input")):
                result = registry.execute("view_image", arguments)
                self.assertFalse(result.ok)
                self.assertEqual(result.failure_kind, kind)

    def test_supported_raster_headers_and_animated_rejection(self):
        self.assertEqual(image_info(png()), ("image/png", 2, 2))
        jpeg = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x03\x00\x04\x01\x01\x11\x00\xff\xd9"
        self.assertEqual(image_info(jpeg), ("image/jpeg", 4, 3))
        webp = b"RIFF" + struct.pack("<I", 22) + b"WEBPVP8X" + struct.pack("<I", 10) + b"\0\0\0\0\x03\0\0\x02\0\0"
        self.assertEqual(image_info(webp), ("image/webp", 4, 3))
        still = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")
        self.assertEqual(image_info(still), ("image/gif", 1, 1))
        animated = still[:-1] + still[still.index(b"\x2c"):]
        with self.assertRaisesRegex(ValueError, "Animated"):
            image_info(animated)
        with self.assertRaises(ValueError):
            image_info(b"\x89PNG\r\n\x1a\n")

    def test_api_payload_contains_native_image_parts_and_original_pixels(self):
        model = OpenAICompatibleModel("https://gateway.example/v1", "test-key", "vision", supports_image_input=True)
        with patch("urllib.request.urlopen", return_value=Response()) as send:
            model.complete([attachment_message()], [])
        payload = json.loads(send.call_args.args[0].data)
        image = payload["messages"][0]["content"][1]
        self.assertEqual(image["type"], "image_url")
        self.assertEqual(base64.b64decode(image["image_url"]["url"].split(",")[1]), png())
        self.assertEqual(image["image_url"]["detail"], "auto")

    def test_provider_unsupported_image_is_typed_not_transient_retry(self):
        model = OpenAICompatibleModel("https://gateway.example/v1", "test-key", "text")
        error = urllib.error.HTTPError("https://gateway.example", 400, "Bad Request", {}, io.BytesIO(b'{"error":"image_url not supported by this model"}'))
        with patch("urllib.request.urlopen", side_effect=error) as send:
            with self.assertRaises(ModelImageInputError):
                model.complete([attachment_message()], [])
        self.assertEqual(send.call_count, 1)
        self.assertFalse(model.supports_image_input)

    def test_vision_capability_metadata_and_setting_do_not_guess_model_name(self):
        result = capabilities_from_payload({"id": "m", "architecture": {"input_modalities": ["text", "image"]}}, "m")
        self.assertTrue(result.supports_image_input)
        result = capabilities_from_payload({"id": "m", "input_modalities": ["text"]}, "m")
        self.assertFalse(result.supports_image_input)
        self.assertIsNone(capabilities_from_payload({"id": "vision-name-only"}, "vision-name-only"))
        self.assertIsNone(_image_setting("auto"))
        self.assertFalse(_image_setting("false"))
        with self.assertRaises(ValueError):
            _image_setting("maybe")

    def test_bad_image_format_does_not_disable_model_vision(self):
        model = OpenAICompatibleModel("https://gateway.example/v1", "test-key", "vision", supports_image_input=True)
        error = urllib.error.HTTPError("https://gateway.example", 400, "Bad Request", {}, io.BytesIO(b'{"error":"unsupported image format"}'))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError):
                model.complete([attachment_message()], [])
        self.assertTrue(model.supports_image_input)

    def test_known_unsupported_direct_api_input_does_not_make_request(self):
        model = OpenAICompatibleModel("https://gateway.example/v1", "test-key", "text", supports_image_input=False)
        with patch("urllib.request.urlopen") as send:
            with self.assertRaises(ModelImageInputError):
                model.complete([attachment_message()], [])
            send.assert_not_called()

    def test_compaction_archives_pixels_and_retains_reload_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = ContextManager({}, root, "task", max_context_tokens=None)
            manager.builder.policy = ContextPolicy(max_recent_actions=1)
            messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}, attachment_message(), {"role": "assistant", "content": "observed red; evidence still needs validation"}]
            context, _ = manager.build_model_context(messages, [])
            self.assertFalse(any(has_images(message) for message in context.messages))
            self.assertTrue(has_images(messages[2]))
            # Native snapshots remain reloadable; full raw messages are archived
            # by the normal pressure-triggered compaction, not the view limiter.
            manager = ContextManager({"max_messages": 3, "keep_latest": 1}, root, "task", max_context_tokens=None)
            compacted, event = manager.prepare(messages, [])
            self.assertIsNotNone(event)
            self.assertNotIn("base64", str(compacted))
            archives = list((root / ".harness" / "context").glob("*.json"))
            self.assertTrue(archives)
            self.assertTrue(any("base64" in path.read_text() for path in archives))

    def test_base64_not_tokenized_or_summarized_as_text(self):
        small = attachment_message()
        huge = attachment_message(small["content"][1]["image_url"]["url"] + "A" * 30_000)
        self.assertEqual(estimate_tokens([small]), estimate_tokens([huge]))
        projected = text_projection([small])
        self.assertNotIn("base64", json.dumps(projected))
        self.assertIn("snapshot=", json.dumps(projected))
        class Model:
            def complete(self, messages, tools):
                self.request = messages
                return {"content": "summary"}
        model = Model()
        ModelSummarizer(model, lambda *args, **kwargs: None, lambda usage: None)([small], "evidence")
        self.assertNotIn("base64", json.dumps(model.request))

    def test_atomic_groups_include_image_and_intervening_middleware(self):
        messages = [{"role": "assistant", "tool_calls": [call("1", "view_image", {})]}, {"role": "tool", "tool_call_id": "1", "content": "queued"}, {"role": "system", "content": "plan update"}, attachment_message(), {"role": "assistant", "content": "observed"}]
        groups = _atomic_groups(messages)
        self.assertEqual([len(group) for group in groups], [4, 1])

    def test_new_image_not_silently_dropped_by_context_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
            messages = prefix + [attachment_message(), {"role": "system", "content": "deferred plan update"}]
            manager = ContextManager({}, Path(tmp), "task", max_context_tokens=None)
            manager.builder.policy = ContextPolicy(max_recent_actions=0)
            context, _ = manager.build_model_context(messages, [])
            self.assertTrue(any(has_images(message) for message in context.messages))
            manager.max_context_tokens = 1000
            with self.assertRaisesRegex(RequestCapacityError, "image input"):
                manager.build_model_context(messages, [])

    def test_provider_context_reduction_keeps_required_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ContextManager({}, Path(tmp), "task", max_context_tokens=None)
            context, _ = manager.build_model_context([{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}, attachment_message()], [])
            with self.assertRaisesRegex(RequestCapacityError, "image input"):
                manager.reduce_after_provider_rejection(context, [], [])

    def test_runtime_tool_batch_order_and_structured_events(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"HARNESS_WORKSPACE": tmp}):
            model = VisionModel()
            runtime = AgentRuntime(build_spec(ROOT), model)
            session = runtime.create_session("Inspect sample.png visually, then test the task.")
            try:
                session.tools.context.executor.write_file(session.tools.workspace / "sample.png", png())
                session.tools.enable_skill("filesystem")
                session.state.turn = 1
                sample_tools(runtime, session)
                self.assertFalse(any(has_images(message) for message in session.messages))
                session.state.turn = 2
                sample_tools(runtime, session)
                request = model.requests[1]
                batch = next(i for i, message in enumerate(request) if message.get("tool_calls"))
                self.assertEqual([message["role"] for message in request[batch:batch + 3]], ["assistant", "tool", "tool"])
                image_index = next(i for i, message in enumerate(request) if has_images(message))
                self.assertGreater(image_index, batch + 2)
                self.assertTrue(any(event["type"] == "image_input_attached" for event in session.events))
                self.assertTrue(any("Image audit snapshot" in fact for fact in session.context_manager.store.facts))
                event = next(event for event in session.events if event["type"] == "tool" and event["name"] == "view_image")
                self.assertFalse(event["material_write"])
                self.assertNotIn("base64", json.dumps(event))
            finally:
                session.close()

    def test_runtime_provider_rejection_continues_without_fabricated_sight(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"HARNESS_WORKSPACE": tmp}):
            model = VisionModel(reject=True)
            runtime = AgentRuntime(build_spec(ROOT), model)
            session = runtime.create_session("Inspect and verify sample.png")
            try:
                session.tools.context.executor.write_file(session.tools.workspace / "sample.png", png())
                session.tools.enable_skill("filesystem")
                for turn in (1, 2):
                    session.state.turn = turn
                    sample_tools(runtime, session)
                self.assertEqual(len(model.requests), 3)
                self.assertFalse(any(has_images(message) for message in model.requests[-1]))
                self.assertIn("NOT seen", str(model.requests[-1]))
                self.assertEqual(session.tools.execute("view_image", {"path": "sample.png"}).failure_kind, "unsupported_image_input")
                self.assertTrue(any(event["type"] == "image_input_rejected" for event in session.events))
                self.assertIsNone(session.state.termination)
            finally:
                session.close()

    def test_roles_and_child_runtime_have_native_view(self):
        for role in default_subagents():
            self.assertIn("view_image", role.tools)
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"HARNESS_WORKSPACE": tmp}):
            root = Path(tmp).resolve()
            runtime = AgentRuntime(build_spec(ROOT), VisionModel())
            session = runtime.create_child_session(None, AgentSpec("visual-inspector", capabilities=("view_image", "load_skill", "list_directory"), skills=("filesystem",)), "Inspect sample.png", "", agent_id="image-child", workspace=root)
            try:
                session.tools.context.executor.write_file(root / "sample.png", png())
                session.tools.enable_skill("filesystem")
                result = session.tools.execute("view_image", {"path": "sample.png"})
                self.assertTrue(result.ok, result.output)
                session.hook("AfterTool", result=result)
                session.hook("BeforeModel")
                self.assertTrue(has_images(session.messages[-1]))
            finally:
                session.close()

    def test_unrecoverable_input_rejection_closes_session_with_reason(self):
        class RejectingModel(VisionModel):
            def complete(self, messages, tools, temperature=0):
                if not self.requests:
                    return super().complete(messages, tools, temperature)
                self.requests.append(list(messages))
                raise ModelImageInputError("provider rejects input")

        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"HARNESS_WORKSPACE": tmp}):
            model = RejectingModel()
            runtime = AgentRuntime(build_spec(ROOT), model)
            session = runtime.create_session("Inspect sample.png")
            session.tools.context.executor.write_file(session.tools.workspace / "sample.png", png())
            session.tools.enable_skill("filesystem")
            import asyncio
            from harness.tasks.oneshot import _run_session_once
            result = asyncio.run(_run_session_once(runtime, session, session.task.text))
            self.assertFalse(result.verified)
            self.assertIn("provider rejects input", str(result.reasons))
            self.assertTrue(session.closed)
            self.assertEqual(result.events[-1]["termination"], "error")
            self.assertEqual(len(model.requests), 3)
            self.assertTrue(any(event["type"] == "model_input_rejected" for event in session.events))
