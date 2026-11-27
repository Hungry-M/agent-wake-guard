"""Read lifecycle metadata from concatenated Zstandard session frames."""

import json
from pathlib import Path

import zstandard as zstd

MAX_BATCH = 32 * 1024 * 1024
MAX_FRAME = 128 * 1024 * 1024


def frame_end(data, start):
    cursor = start + 4
    if cursor > len(data):
        return None
    magic = int.from_bytes(data[start:cursor], "little")
    if 0x184D2A50 <= magic <= 0x184D2A5F:
        if cursor + 4 > len(data):
            return None
        end = cursor + 4 + int.from_bytes(data[cursor:cursor + 4], "little")
        return (end, True) if end <= len(data) else None
    if magic != 0xFD2FB528:
        raise ValueError("Unsupported DeepSeek session frame")
    if cursor == len(data):
        return None
    flags = data[cursor]
    if flags & 8:
        raise ValueError("Invalid Zstandard frame header")
    single_segment = bool(flags & 32)
    cursor += 1 + (not single_segment) + (0, 1, 2, 4)[flags & 3]
    cursor += (int(single_segment), 2, 4, 8)[flags >> 6]
    while cursor + 3 <= len(data):
        block = int.from_bytes(data[cursor:cursor + 3], "little")
        block_type = (block >> 1) & 3
        if block_type == 3:
            raise ValueError("Invalid Zstandard block")
        cursor += 3 + (1 if block_type == 1 else block >> 3)
        if block & 1:
            end = cursor + (4 if flags & 4 else 0)
            return (end, False) if end <= len(data) else None
    return None


def consume(output, state):
    if not output.endswith(b"\n"):
        raise ValueError("DeepSeek frame is missing its JSONL terminator")
    for line in output.split(b"\n"):
        if b'"turn/' not in line and b'"agent/inbox/spliced"' not in line:
            continue
        row = json.loads(line)
        kind, event = row["type"], row.get("data") or {}
        if kind in {"turn/start", "turn/end"}:
            if kind == "turn/start":
                status = "running"
            else:
                reason = (event.get("reason") or {}).get("kind")
                status = "completed" if reason == "completed" else "attention"
            state["fact"] = {"at": row["time"] / 1000, "status": status, "id": str(row["seq"])}
        elif kind == "agent/inbox/spliced" and event.get("target") in {"next-turn", "next-step"}:
            target, inbox = event["target"], state["inbox"]
            removed, inserted = event.get("removedCount"), event.get("inserted")
            removed = removed if type(removed) is int else 0
            added = len(inserted) if isinstance(inserted, list) else 0
            inbox[target] = max(0, inbox.get(target, 0) - removed + added)


def scan(item, decoder):
    state = {"offset": item["offset"], "fact": item["fact"], "inbox": dict(item["inbox"])}
    with Path(item["path"]).open("rb") as stream:
        stream.seek(state["offset"])
        while state["offset"] < item["size"]:
            data = memoryview(stream.read(min(item["size"] - state["offset"], MAX_BATCH)))
            cursor = 0
            while cursor < len(data):
                frame = frame_end(data, cursor)
                if frame is None:
                    break
                end, skip = frame
                if not skip:
                    compressed = data[cursor:end]
                    if zstd.frame_content_size(compressed) > MAX_FRAME:
                        raise ValueError("DeepSeek frame exceeds its decompression limit")
                    consume(decoder.decompress(compressed, max_output_size=MAX_FRAME,
                                               allow_extra_data=False), state)
                cursor = end
            if not cursor:
                break
            state["offset"] += cursor
            stream.seek(state["offset"])
    state["complete"] = state["offset"] == item["size"]
    return state


def read_journals(requests):
    errors = (OSError, ValueError, TypeError, KeyError, zstd.ZstdError)
    decoder = zstd.ZstdDecompressor(max_window_size=MAX_FRAME)
    for item in requests:
        try:
            try:
                record = scan(item, decoder)
            except errors:
                if not item["offset"]:
                    raise
                record = scan({**item, "offset": 0, "fact": None, "inbox": {}}, decoder)
        except errors as exc:
            record = {"error": str(exc)}
        yield record
