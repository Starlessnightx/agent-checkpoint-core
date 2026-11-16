"""Read the optional host acknowledgement control record."""
from pathlib import Path
from checkpoint_plugin.contracts import ContractError
ACK = "accepted"

def read_ack(control_root: Path, request_id: str) -> dict:
    if type(request_id) is not str or len(request_id) != 32 or any(c not in '0123456789abcdef' for c in request_id):
        raise ContractError('invalid request identity')
    base = Path(control_root)
    from harness.executor import LocalExecutor
    from harness.permissions import PermissionPolicy
    from .control_io import ControlIO
    io = ControlIO(LocalExecutor(base.parent), PermissionPolicy())
    failure = base / 'failures' / (request_id + '.json')
    if failure.is_file():
        raise ContractError('boundary capture failed: ' + str(io.read(failure, maximum=8192).get('error_type', 'unknown')))
    ack = base / 'acks' / (request_id + '.json')
    if not ack.is_file():
        raise FileNotFoundError('boundary acknowledgement is not available')
    value = io.read(ack, maximum=8192)
    if (type(value) is not dict or value.get('schema_version') != 1
            or value.get('kind') != 'science-boundary-ack-v1'
            or value.get('request_id') != request_id or value.get('status') != ACK
            or type(value.get('root_index')) is not str or len(value['root_index']) != 64
            or any(c not in '0123456789abcdef' for c in value['root_index'])):
        raise ContractError('invalid boundary acknowledgement')
    return value
