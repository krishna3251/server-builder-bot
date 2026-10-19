"""Write complete JSON snapshots without truncating the previous valid file."""

import asyncio
import json
import os
import tempfile
from pathlib import Path


async def write_json_atomic(filename: str, data) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False)

    def write():
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    # Let the write finish even if the caller is cancelled, so a subsequent
    # operation cannot race an older replacement after releasing its lock.
    task = asyncio.create_task(asyncio.to_thread(write))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
