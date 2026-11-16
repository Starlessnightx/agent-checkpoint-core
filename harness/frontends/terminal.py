from __future__ import annotations

import asyncio
import codecs
import json
import os
import sys
from typing import Awaitable, Callable, TextIO
from uuid import uuid4

from harness.protocol import ApprovalResponse, UserInputAnswer


class TerminalInput:
    """Cancellable POSIX terminal input; no blocked input worker to abandon."""
    def __init__(self, stream: TextIO = sys.stdin, output: TextIO = sys.stdout):
        self.stream, self.output = stream, output
        self.buffer = ""
        self.eof = False
        self.decoder = codecs.getincrementaldecoder(stream.encoding or "utf-8")(errors="replace")

    def _line(self) -> str | None:
        if "\n" in self.buffer:
            value, self.buffer = self.buffer.split("\n", 1)
            return value.rstrip("\r")
        value, self.buffer = self.buffer, ""
        return value if value else None

    async def readline(self, prompt: str) -> str | None:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.output.write(prompt)
        self.output.flush()
        if "\n" in self.buffer or self.eof:
            return self._line()
        descriptor = self.stream.fileno()

        def ready():
            if future.done():
                return
            try:
                chunk = os.read(descriptor, 4096)
                self.eof = not bool(chunk)
                self.buffer += self.decoder.decode(chunk, final=self.eof)
                if "\n" in self.buffer or self.eof:
                    future.set_result(self._line())
            except (BlockingIOError, InterruptedError):
                return
            except Exception as exc:
                future.set_exception(exc)

        loop.add_reader(descriptor, ready)
        try:
            return await future
        finally:
            loop.remove_reader(descriptor)


class TerminalFrontend:
    def __init__(self, read: Callable[[str], Awaitable[str | None]], write: Callable[[str], None]):
        self.read, self.write = read, write

    async def __call__(self, event: dict) -> ApprovalResponse | UserInputAnswer | None:
        scope = dict(session_id=event["session_id"], turn_id=event["turn_id"],
                     request_id=event["request_id"], operation_id=str(uuid4()))
        details = event["details"]
        if event["type"] == "approval_requested":
            if details.get("request_kind") == "model":
                self.write(f"Approval: model {details['model_name']}\n{event['text']}")
            else:
                self.write(f"Approval: {details['tool_name']}\n{json.dumps(details['arguments'], ensure_ascii=False)}\n{event['text']}")
            while True:
                answer = await self.read("Allow this invocation? [y/N] ")
                if answer is None:
                    return None
                normalized = answer.strip().lower()
                if normalized in {"y", "yes", "", "n", "no"}:
                    return ApprovalResponse(**scope, approved=normalized in {"y", "yes"})
                self.write("Please enter yes or no.")
        answers = []
        for question in details["questions"]:
            answer = await self.read(question["question"] + " ")
            if answer is None:
                return None
            answers.append((question["id"], answer))
        return UserInputAnswer(**scope, answers=tuple(answers))
