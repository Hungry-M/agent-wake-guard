"""Protocol example that reports an idle source with no task observed."""

import json
import sys


def serve():
    if sys.argv[1:] != ["--source-plugin-v1"]:
        return 2
    for line in sys.stdin.buffer:
        command = json.loads(line)["command"]
        if command == "hello":
            fields = {"protocol": 1}
        elif command == "sample":
            fields = {"available": True, "observed": False, "busy": 0, "attention": 0, "revision": "idle"}
        else:
            print(json.dumps({"command": command, "ok": False, "error": "Unsupported command"}), flush=True)
            continue
        print(json.dumps({"command": command, "ok": True, **fields}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(serve())
