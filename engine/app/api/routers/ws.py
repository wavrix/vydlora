"""WebSocket endpoint for real-time updates."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...core.events import manager

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_json({"type": "connected", "data": {"clients": manager.count}})
        while True:
            # Keep the connection open; ignore inbound payloads (heartbeat/ping).
            await ws.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception:
        await manager.disconnect(ws)
