"""Small checked, atomic JSON control records; no task code is executed."""
from pathlib import Path
import sys
from uuid import uuid4

from checkpoint_plugin.contracts import canonical, decode, ContractError
from harness.executor import Command
from harness.storage import ExperimentIO

_COMMIT = '''import os,sys
source,target=sys.argv[1:]
with open(source,'rb') as stream: os.fsync(stream.fileno())
os.replace(source,target)
fd=os.open(os.path.dirname(target),os.O_RDONLY)
try: os.fsync(fd)
finally: os.close(fd)
'''


class ControlIO:
    def __init__(self, executor, policy, emit=None):
        self.io = ExperimentIO(executor, policy, emit)
        self.executor = executor

    def read(self, path, *, maximum=8 * 1024**2):
        path = self.io.check(Path(path))
        if self.executor.size(path) > maximum:
            raise ContractError('control record exceeds bound')
        return decode(self.io.read(path))

    def exists(self, path):
        return self.executor.exists(self.io.check(Path(path)))

    def write(self, path, value):
        self.write_bytes(path, canonical(value))

    def write_bytes(self, path, raw, *, maximum=8 * 1024**2):
        path = self.io.check(Path(path), True)
        if type(raw) is not bytes or len(raw) > maximum:
            raise ContractError('control record exceeds bound')
        temporary = self.io.check(path.with_name('.pending-' + uuid4().hex), True)
        self.io.write(temporary, raw)
        try:
            result = self.executor.exec(Command.argv(
                (sys.executable, '-I', '-c', _COMMIT, str(temporary), str(path)),
                self.executor.workspace))
            if result.return_code or result.timed_out:
                raise ContractError('atomic control record commit failed')
        finally:
            if self.executor.exists(temporary):
                self.executor.remove_file(self.io.check(temporary, True))

    def remove(self, path):
        path = self.io.check(Path(path), True)
        if self.executor.exists(path):
            self.executor.remove_file(path)

    def directory(self, path):
        path = self.io.check(Path(path), True)
        result = self.executor.exec(Command.argv((sys.executable, '-I', '-c',
            "import os,sys; os.makedirs(sys.argv[1],mode=0o700,exist_ok=True)", str(path)),
            self.executor.workspace))
        if result.return_code:
            raise ContractError('control directory creation failed')

    def publish_directory(self, source, target):
        source = self.io.check(Path(source), True)
        target = self.io.check(Path(target), True)
        result = self.executor.exec(Command.argv((sys.executable, '-I', '-c',
            "import os,sys; os.rename(sys.argv[1],sys.argv[2]); fd=os.open(os.path.dirname(sys.argv[2]),os.O_RDONLY); os.fsync(fd); os.close(fd)",
            str(source), str(target)), self.executor.workspace))
        if result.return_code:
            raise ContractError('control directory publication failed')

    def remove_owned_tree(self, path):
        path = self.io.check(Path(path), True)
        # Only private staging names may be recursively removed by this helper.
        if not (path.name.startswith('.pending-') or path.name.startswith('.source-stage-')):
            raise ContractError('refusing non-staging tree removal')
        result = self.executor.exec(Command.argv((sys.executable, '-I', '-c',
            "import os,shutil,sys; p=sys.argv[1]; shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else None", str(path)),
            self.executor.workspace))
        if result.return_code:
            raise ContractError('staging tree removal failed')
