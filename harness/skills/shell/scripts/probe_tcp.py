from __future__ import annotations

import socket
import sys


_workspace = sys.argv[1]
host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
port = int(sys.argv[3])
timeout = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0
with socket.create_connection((host, port), timeout=timeout):
    print(f"ready: tcp://{host}:{port}")
