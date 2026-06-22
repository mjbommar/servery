#!/usr/bin/env python3
"""Start a servery subprocess, wait for it, run loadgen against it, tear it down.

Keeps the server as a managed child (killed in a finally), so benchmarks never
orphan a process. One foreground invocation does the whole cycle.

    python scripts/abdriver.py 8800 SERVER_ARGS... -- LOADGEN_ARGS...
    # e.g.
    python scripts/abdriver.py 8800 /tmp/sd -p 8800 -q -- http://127.0.0.1:8800/s.txt -c 100 --close
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
sep = argv.index("--")
port = int(argv[0])
server_args = argv[1:sep]
load_args = argv[sep + 1 :]
here = Path(__file__).resolve().parent.parent

env = {**os.environ, "PYTHONPATH": str(here)}
server = subprocess.Popen([sys.executable, "-m", "servery", *server_args], env=env)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        print("server did not bind", file=sys.stderr)
        raise SystemExit(1)
    subprocess.run([sys.executable, str(here / "scripts" / "loadgen.py"), *load_args], check=False)
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
