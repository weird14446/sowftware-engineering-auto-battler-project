import asyncio
import json
from typing import Any, Dict, Optional


async def read_message(reader: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
    try:
        line = await reader.readline()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError:
        return None


async def send_message(writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
    try:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        writer.write(data)
        await writer.drain()
    except (ConnectionResetError, ConnectionError, BrokenPipeError):
        pass
