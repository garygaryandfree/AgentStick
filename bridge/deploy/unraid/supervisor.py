from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def main() -> int:
    environment = os.environ.copy()
    processes = [
        subprocess.Popen([sys.executable, "/config/hotword-api.py"], env=environment),
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "vibe_stick",
                "--host",
                "0.0.0.0",
                "--port",
                "8765",
            ],
            env=environment,
        ),
    ]

    stopping = False

    def stop_all(_signum: int | None = None, _frame: object | None = None) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    try:
        while True:
            for process in processes:
                return_code = process.poll()
                if return_code is not None:
                    stop_all()
                    return return_code
            time.sleep(0.25)
    finally:
        stop_all()
        deadline = time.monotonic() + 5
        for process in processes:
            if process.poll() is None:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
