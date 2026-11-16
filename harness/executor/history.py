"""Completed logical process records, never reconstructed process authority."""
from dataclasses import replace
from pathlib import Path
import re
from typing import Callable

from .base import ProcessStatus


class CompletedProcessHistory:
    def __init__(self):
        self._records: dict[str, ProcessStatus] = {}
        self._installed = False
        self._workspace: Path | None = None

    def __contains__(self, handle: str) -> bool:
        return handle in self._records

    def install(self, records: tuple[ProcessStatus, ...], workspace: Path) -> None:
        if self._installed:
            raise RuntimeError('completed process history is already installed')
        if type(records) is not tuple or len(records)>65536:
            raise ValueError('completed process history exceeds bound')
        imported = {}
        root = workspace.resolve()
        for record in records:
            if (type(record) is not ProcessStatus or type(record.handle) is not str
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', record.handle)
                    or record.handle in imported or record.running is not False
                    or type(record.pid) is not int or record.pid < 0
                    or type(record.return_code) is not int or type(record.command) is not str
                    or len(record.command)>1024*1024 or not isinstance(record.log_path, Path)
                    or not record.log_path.is_absolute()
                    or not record.log_path.resolve().is_relative_to(root)):
                raise ValueError('invalid completed process record')
            imported[record.handle] = replace(record, pid=0, output='')
        self._records, self._installed, self._workspace = imported, True, root

    def records(self) -> tuple[ProcessStatus, ...]:
        return tuple(self._records.values())

    def status(self, handle: str, read_log: Callable[[Path], bytes]) -> ProcessStatus | None:
        record = self._records.get(handle)
        if record is None:
            return None
        if not record.log_path.resolve().is_relative_to(self._workspace):
            raise ValueError('completed process log escapes the target workspace')
        # The deployment has verified and restored the immutable log archive.
        # Missing/corrupt logs are errors, not invented empty process output.
        return replace(record, output=read_log(record.log_path).decode('utf-8', errors='replace'))


def fresh_handle(factory, unavailable):
    for _ in range(16):
        handle = factory()
        if not unavailable(handle):
            return handle
    raise RuntimeError('cannot allocate a unique process handle')
