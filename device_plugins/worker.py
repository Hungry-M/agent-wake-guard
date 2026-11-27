"""Serve an adapter over Guard's v1 UTF-8 JSON-lines protocol."""

import json
import sys

from windows_power import GuardError


def serve(session, active_status, incoming=None, outgoing=None):
    incoming = sys.stdin.buffer if incoming is None else incoming
    outgoing = sys.stdout.buffer if outgoing is None else outgoing

    def receive():
        line = incoming.readline(65537)
        if not line:
            return None
        if len(line) > 65536 or not line.endswith(b"\n"):
            raise ValueError("Invalid protocol frame length.")
        value = json.loads(line.decode("utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("command"), str):
            raise ValueError("Expected a JSON object with a command.")
        return value

    def emit(**message):
        outgoing.write(json.dumps(message).encode("utf-8") + b"\n")
        outgoing.flush()

    def update_status(title, detail):
        emit(event="status", title=title, detail=detail)

    command = "hello"
    errors = (GuardError, OSError, ValueError, RecursionError)
    try:
        packet = receive()
        if packet is None:
            return 0
        if packet.get("command") != command:
            raise ValueError("Expected hello.")
        emit(command=command, ok=True, protocol=1)
        command = "start"
        packet = receive()
        if packet is None:
            return 0
        if packet.get("command") != command or type(packet.get("enabled")) is not bool:
            raise ValueError("Expected start with a Boolean enabled field.")
        with session(packet["enabled"], update_status) as protection:
            active = protection is not None
            emit(command=command, ok=True, active=active, status=active_status if active else "")
            methods = {"check": protection.require_alive, "restore": protection.restore,
                       "suspend": protection.suspend} if active else {}
            command = "stop"
            while (packet := receive()) is not None:
                command = packet["command"]
                if command == "stop":
                    break
                try:
                    if command not in methods:
                        raise ValueError(f"Unsupported command in this session: {command}")
                    methods[command]()
                except errors as exc:
                    emit(command=command, ok=False, error=str(exc))
                else:
                    emit(command=command, ok=True)
                command = "stop"
        # Acknowledge stop only after the adapter's context has restored its settings.
        if packet is not None:
            emit(command="stop", ok=True)
        return 0
    except BrokenPipeError:
        return 1
    except errors as exc:
        try:
            emit(command=command, ok=False, error=str(exc))
        except OSError:
            pass
        return 1
