from __future__ import annotations

import base64
from dataclasses import dataclass
from functools import lru_cache
from math import ceil
from typing import Any

ATTACHMENT_PREFIX = "Tool image attachments (audit snapshots; not verification evidence):"


@dataclass(frozen=True)
class ImageAttachment:
    source: str
    snapshot: str
    sha256: str
    mime_type: str
    width: int
    height: int
    detail: str
    data_url: str

    def dictionary(self) -> dict[str, Any]:
        return {key: value for key, value in vars(self).items() if key != "data_url"}


class ModelImageInputError(ValueError):
    """A provider explicitly rejected its image input capability."""


def image_info(data: bytes) -> tuple[str, int, int]:
    """Read raster headers, not a replacement for a provider's image decoder."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 33:
        if data[12:16] == b"IHDR" and data[-12:-8] == b"\x00\x00\x00\x00" and data[-8:-4] == b"IEND":
            width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
            return _dimensions("image/png", width, height)
    if data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 255:
                break
            while offset < len(data) and data[offset] == 255:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 8:
                return _dimensions("image/jpeg", int.from_bytes(data[offset + 5:offset + 7], "big"), int.from_bytes(data[offset + 3:offset + 5], "big"))
            offset += length
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        kind, payload = data[12:16], data[20:]
        if kind == b"VP8X":
            if payload[0] & 2:
                raise ValueError("Animated WebP is not supported; select a still frame.")
            return _dimensions("image/webp", 1 + int.from_bytes(payload[4:7], "little"), 1 + int.from_bytes(payload[7:10], "little"))
        if kind == b"VP8 " and payload[3:6] == b"\x9d\x01\x2a":
            return _dimensions("image/webp", int.from_bytes(payload[6:8], "little") & 0x3FFF, int.from_bytes(payload[8:10], "little") & 0x3FFF)
        if kind == b"VP8L" and payload[0] == 0x2F:
            bits = int.from_bytes(payload[1:5], "little")
            return _dimensions("image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 14:
        # Parse block boundaries rather than counting separator bytes in compressed data.
        offset = 13 + (3 * (2 ** ((data[10] & 7) + 1)) if data[10] & 128 else 0)
        frames = 0
        while offset < len(data):
            marker = data[offset]
            offset += 1
            if marker == 0x3B and frames == 1:
                return _dimensions("image/gif", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"))
            if marker == 0x2C:
                frames += 1
                if frames > 1:
                    raise ValueError("Animated GIF is not supported; select a still frame.")
                if offset + 9 >= len(data):
                    break
                packed = data[offset + 8]
                offset += 9 + (3 * (2 ** ((packed & 7) + 1)) if packed & 128 else 0) + 1
            elif marker == 0x21:
                offset += 1
            else:
                break
            while offset < len(data):
                size = data[offset]
                offset += 1 + size
                if not size:
                    break
    raise ValueError("Unsupported or malformed image. Use PNG, JPEG, WebP or a non-animated GIF (not SVG/PDF/text).")


def _dimensions(mime: str, width: int, height: int) -> tuple[str, int, int]:
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions must be positive.")
    return mime, width, height


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def has_images(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "image_url" for part in content)


@lru_cache(maxsize=8)
def _image_cost(url: str, detail: str) -> int:
    # Planning heuristic only. Providers use different patch/tile accounting;
    # measured usage and context-rejection recovery remain authoritative.
    if detail == "low":
        return 4096
    try:
        _, width, height = image_info(base64.b64decode(url.split(",", 1)[1], validate=True))
        return max(4096, ceil(width / 32) * ceil(height / 32) * 4)
    except (ValueError, IndexError):
        return 4096


def image_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        if has_images(message):
            for part in message["content"]:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image = part.get("image_url") or {}
                    total += _image_cost(str(image.get("url", "")), str(image.get("detail", "auto")))
    return total


def is_tool_attachment(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return message.get("role") == "user" and has_images(message) and isinstance(content, list) and bool(content) and isinstance(content[0], dict) and str(content[0].get("text", "")).startswith(ATTACHMENT_PREFIX)


def text_projection(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Never tokenize/summarize base64 as ordinary text; retain snapshot labels."""
    return [
        {**message, "content": [
            {"type": "text", "text": "[native image input; inspect audit snapshot or view_image to reload]"}
            if isinstance(part, dict) and part.get("type") == "image_url" else part
            for part in message["content"]
        ]} if has_images(message) else message
        for message in messages
    ]
