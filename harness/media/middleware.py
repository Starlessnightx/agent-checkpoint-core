from __future__ import annotations

import json
import os
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from harness.core.hooks import HookContext
from harness.tools.base import ToolContext, ToolResult
from .images import ATTACHMENT_PREFIX, ImageAttachment, has_images, text_projection


class ImageMiddleware:
    parallel_prefetch_safe = True
    name = "native_image_delivery"
    order = 60

    def __init__(self, tools: ToolContext, model: Any):
        self.model = model
        self.pending: list[ImageAttachment] = []
        self.rejected = False
        tools.image_input_support = self.support

    def checkpoint_state(self) -> dict[str, Any]:
        from dataclasses import asdict
        return {"schema_version": 1, "pending": [asdict(image) for image in self.pending],
                "rejected": self.rejected,
                "image_limits": {"maximum_bytes": int(os.getenv("HARNESS_MAX_IMAGE_BYTES", "4000000")),
                                 "maximum_images": int(os.getenv("HARNESS_MAX_IMAGES", "6"))}}

    def support(self) -> bool | None:
        return False if self.rejected else getattr(self.model, "supports_image_input", None)

    def restore_checkpoint_state(self, value: dict[str, Any]) -> None:
        import base64
        import hashlib
        from harness.core.portable_state import copy_plain, require_fields
        from .images import image_info
        value = copy_plain(value)
        require_fields(value, self.checkpoint_state())
        limits = self.checkpoint_state()['image_limits']
        if (type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['image_limits'] != limits or type(value['rejected']) is not bool
                or type(value['pending']) is not list or len(value['pending']) > limits['maximum_images']):
            raise ValueError('unsupported image middleware state')
        pending = []
        fields = set(ImageAttachment.__dataclass_fields__)
        for item in value['pending']:
            require_fields(item, fields)
            if (any(type(item[k]) is not str for k in fields - {'width', 'height'})
                    or any(type(item[k]) is not int or item[k] <= 0 for k in ('width', 'height'))
                    or item['detail'] not in {'auto', 'low', 'high'}):
                raise ValueError('invalid image attachment state')
            prefix = 'data:' + item['mime_type'] + ';base64,'
            encoded = item['data_url']
            if not encoded.startswith(prefix) or len(encoded) > limits['maximum_bytes']*4//3+256:
                raise ValueError('invalid or oversized image payload')
            try:
                raw = base64.b64decode(encoded[len(prefix):], validate=True)
            except ValueError as exc:
                raise ValueError('invalid image encoding') from exc
            if (len(raw) > limits['maximum_bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']
                    or image_info(raw) != (item['mime_type'], item['width'], item['height'])):
                raise ValueError('image attachment content differs')
            pending.append(ImageAttachment(**item))
        self.pending, self.rejected = pending, value['rejected']

    def handle(self, context: HookContext) -> None:
        session = context.session
        if context.event == "AfterTool":
            result = context.payload.get("result")
            if isinstance(result, ToolResult) and result.ok:
                self.pending.extend(result.images)
                for image in result.images:
                    session.emit("context_fact", value=f"Image audit snapshot: source={image.source}; snapshot={image.snapshot}; sha256={image.sha256}. Reload with view_image if pixels leave context. Queueing is not verification evidence.")
        elif context.event == "BeforeModel" and self.pending:
            images, self.pending = self.pending, []
            # Cap the base64 payload: providers reject oversized request bodies,
            # and a handful of full frames is enough to exceed that limit.
            budget = int(os.getenv("HARNESS_MAX_IMAGE_BYTES", "4000000"))
            max_images = int(os.getenv("HARNESS_MAX_IMAGES", "6"))
            kept, dropped, total = [], [], 0
            for image in reversed(images):
                size = len(image.data_url)
                if len(kept) < max_images and total + size <= budget:
                    kept.append(image)
                    total += size
                else:
                    dropped.append(image)
            if not kept and images:
                kept.append(images[-1])
                total = len(images[-1].data_url)
                dropped = images[:-1]
            kept.reverse()
            dropped.reverse()
            note = ""
            if dropped:
                note = ("\n[image payload limit] not attached: "
                        + ", ".join(image.source for image in dropped)
                        + ". Reload a specific image with view_image if its pixels matter.")
                session.emit("image_input_dropped", dropped=[image.dictionary() for image in dropped],
                             retained_bytes=total)
            # Image messages must not interrupt outstanding tool result batches.
            session.messages.append({"role": "user", "content": [
                {"type": "text", "text": ATTACHMENT_PREFIX + "\n" + json.dumps([image.dictionary() for image in kept], ensure_ascii=False) + note},
                *[{"type": "image_url", "image_url": {"url": image.data_url, "detail": image.detail}} for image in kept],
            ]})
            session.emit("image_input_attached", images=[image.dictionary() for image in images])
        elif context.event == "ModelInputFailure":
            messages = context.payload.get("messages", [])
            if self.rejected or not any(has_images(message) for message in messages):
                return
            self.rejected = True
            # Snapshots/events remain auditable; do not imply fictitious sight.
            projected = text_projection(messages)
            projected.append({"role": "user", "content": "Native image input was rejected by this model/provider. You have NOT seen these images. Continue only with available evidence or request a vision-capable model. Error: " + str(context.payload.get("error", ""))[:1000]})
            session.messages[:] = text_projection(session.messages)
            session.messages.append(projected[-1])
            context.data["retry_messages"] = projected
            session.emit("image_input_rejected", reason=str(context.payload.get("error", ""))[:1000], snapshots_retained=True)
