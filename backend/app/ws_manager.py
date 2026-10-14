"""
Simple WebSocket connection manager for broadcasting reconstruction progress.
"""
import json
from collections import defaultdict
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = defaultdict(list)

    async def connect(self, key: str, ws: WebSocket):
        await ws.accept()
        self._connections[key].append(ws)

    def disconnect(self, key: str, ws: WebSocket):
        self._connections[key] = [c for c in self._connections[key] if c is not ws]

    async def broadcast(self, key: str, data: dict):
        dead = []
        for ws in self._connections.get(key, []):
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(key, ws)


ws_manager = ConnectionManager()
