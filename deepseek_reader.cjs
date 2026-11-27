// Read only lifecycle metadata from the DeepSeek v4 Zstandard session journal.
const fs = require("node:fs");
const zlib = require("node:zlib");

const MAX_BATCH = 32 * 1024 * 1024;
const MAX_FRAME = 128 * 1024 * 1024;

function frameEnd(bytes, start) {
  if (start + 4 > bytes.length) return null;
  let cursor = start + 4;
  const magic = bytes.readUInt32LE(start);
  if (magic >= 0x184d2a50 && magic <= 0x184d2a5f) {
    if (cursor + 4 > bytes.length) return null;
    const end = cursor + 4 + bytes.readUInt32LE(cursor);
    return end <= bytes.length ? [end, true] : null;
  }
  if (magic !== 0xfd2fb528) throw new Error("Unsupported DeepSeek session frame");
  if (cursor === bytes.length) return null;
  const flags = bytes[cursor++];
  if (flags & 8) throw new Error("Invalid Zstandard frame header");
  const singleSegment = Boolean(flags & 32);
  cursor += (singleSegment ? 0 : 1) + [0, 1, 2, 4][flags & 3]
            + [singleSegment ? 1 : 0, 2, 4, 8][flags >> 6];
  let block;
  do {
    if (cursor + 3 > bytes.length) return null;
    block = bytes.readUIntLE(cursor, 3);
    const type = (block >> 1) & 3;
    if (type === 3) throw new Error("Invalid Zstandard block");
    cursor += 3 + (type === 1 ? 1 : block >> 3);
  } while (!(block & 1));
  if (flags & 4) cursor += 4;
  return cursor <= bytes.length ? [cursor, false] : null;
}

function consume(text, state) {
  for (const line of text.split("\n")) {
    if (!line.includes('"turn/') && !line.includes('"agent/inbox/spliced"')) continue;
    const row = JSON.parse(line);
    const data = row.data || {};
    if (row.type === "turn/start" || row.type === "turn/end") {
      if (typeof row.time !== "number") throw new Error("DeepSeek event has no timestamp");
      state.fact = {
        at: row.time / 1000,
        status: row.type === "turn/start" ? "running"
          : data.reason?.kind === "completed" ? "completed" : "attention",
        id: String(row.seq),
      };
    } else if (row.type === "agent/inbox/spliced"
               && ["next-turn", "next-step"].includes(data.target)) {
      const removed = Number.isInteger(data.removedCount) ? data.removedCount : 0;
      const added = Array.isArray(data.inserted) ? data.inserted.length : 0;
      state.inbox[data.target] = Math.max(0, (state.inbox[data.target] || 0) - removed + added);
    }
  }
}

function scan(item) {
  const state = { offset: item.offset, fact: item.fact, inbox: { ...item.inbox } };
  const fd = fs.openSync(item.path, "r");
  try {
    const stop = Math.min(item.size, fs.fstatSync(fd).size);
    while (state.offset < stop) {
      const bytes = Buffer.alloc(Math.min(stop - state.offset, MAX_BATCH));
      const length = fs.readSync(fd, bytes, 0, bytes.length, state.offset);
      const readable = bytes.subarray(0, length);
      let cursor = 0;
      while (cursor < length) {
        const frame = frameEnd(readable, cursor);
        if (!frame) break;
        const [end, skip] = frame;
        if (!skip) {
          const output = zlib.zstdDecompressSync(readable.subarray(cursor, end),
                                                { maxOutputLength: MAX_FRAME });
          const text = output.toString("utf8");
          if (!text.endsWith("\n")) throw new Error("DeepSeek frame is missing its JSONL terminator");
          consume(text, state);
        }
        cursor = end;
      }
      state.offset += cursor;
      if (!cursor) break;
    }
    return { ...state, complete: state.offset === item.size };
  } finally {
    fs.closeSync(fd);
  }
}

if (typeof zlib.zstdDecompressSync !== "function") {
  throw new Error("Node.js with Zstandard support is required.");
}
const requests = JSON.parse(fs.readFileSync(0, "utf8"));
const results = requests.map(item => {
  try {
    return scan(item);
  } catch (error) {
    // A repaired/replaced file may no longer have an append boundary at the cached offset.
    if (item.offset) {
      try {
        return scan({ ...item, offset: 0, fact: null, inbox: {} });
      } catch (retryError) {
        return { error: retryError.code || retryError.message };
      }
    }
    return { error: error.code || error.message };
  }
});
process.stdout.write(JSON.stringify(results));
