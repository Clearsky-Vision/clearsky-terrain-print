"""Start the packaged TopoSaic studio on this computer."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser


def main():
    root = Path(__file__).resolve().parent.parent
    address = ("127.0.0.1", 8787)
    url = "http://127.0.0.1:8787"
    try:
        with socket.create_connection(address, timeout=.3):
            print("Port 8787 is already in use. Close the other local studio and try again.")
            return 1
    except OSError:
        pass
    env = os.environ | {
        "TOPOSAIC_BIND": "127.0.0.1:8787",
        "TOPOSAIC_DATA_DIR": str(root / ".studio-data"),
        "TOPOSAIC_CACHE_DIR": str(root / ".studio-cache"),
        "TOPOSAIC_STUDIO_DIR": str(root / "studio"),
        "TOPOSAIC_CLEARSKY_PYTHON": sys.executable,
        "TOPOSAIC_CLEARSKY_BRIDGE": str(root / "clearsky_print" / "studio_bridge.py"),
        "TOPOSAIC_CLEARSKY_ENGINE": str(root / "bin" / "clearsky_terrain.exe"),
        "TOPOSAIC_CLEARSKY_EXAMPLE": str(root / "examples" / "mount-fuji"),
        "RUST_LOG": "warn",
        # The Windows Python installer supplies the usual MSVC runtime
        # beside its executable for machines without a system copy.
        "PATH": sys.base_exec_prefix + os.pathsep + os.environ.get("PATH", ""),
    }
    try:
        process = subprocess.Popen([str(root / "bin" / "toposaic-api.exe")], cwd=root, env=env)
    except OSError as error:
        print(f"Could not start TopoSaic: {error}")
        return 1
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with urlopen(url + "/api/clearsky/status", timeout=1) as response:
                    if json.load(response)["available"]:
                        break
            except (URLError, OSError, ValueError, KeyError):
                time.sleep(.25)
        else:
            print("The local studio did not start. Check the messages above.")
            return 1
        print(f"TopoSaic is open at {url}. Keep this window open while you work.", flush=True)
        webbrowser.open(url)
        process.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
