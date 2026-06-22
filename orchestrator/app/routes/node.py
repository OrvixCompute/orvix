"""WebSocket endpoint that nodes connect to: WS /v1/node/connect."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.logger import logger
from app.models.protocol import (
    HeartbeatMessage,
    JobChunkMessage,
    JobResultMessage,
    RegisterAckMessage,
    RegisterMessage,
    parse_message,
    serialize,
)
from app.services.node_manager import node_manager

router = APIRouter(tags=["node"])

REGISTER_TIMEOUT_S = 10.0


@router.websocket("/v1/node/connect")
async def node_connect(websocket: WebSocket) -> None:
    await websocket.accept()
    node_id: str | None = None
    try:
        # 1. First frame must be a RegisterMessage (within the timeout).
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=REGISTER_TIMEOUT_S)
        except asyncio.TimeoutError:
            await websocket.send_text(
                serialize(
                    RegisterAckMessage(node_id="", accepted=False, reason="register timeout")
                )
            )
            await websocket.close()
            return

        msg = parse_message(raw)
        if not isinstance(msg, RegisterMessage):
            await websocket.send_text(
                serialize(
                    RegisterAckMessage(
                        node_id="", accepted=False, reason="expected register message"
                    )
                )
            )
            await websocket.close()
            return

        # 2. Register via the manager.
        try:
            conn = await node_manager.register_node(websocket, msg)
        except ValueError as exc:
            await websocket.send_text(
                serialize(RegisterAckMessage(node_id="", accepted=False, reason=str(exc)))
            )
            await websocket.close()
            return

        node_id = conn.node_id
        await websocket.send_text(
            serialize(RegisterAckMessage(node_id=node_id, accepted=True))
        )

        # 3. Main receive loop.
        while True:
            raw = await websocket.receive_text()
            try:
                incoming = parse_message(raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bad frame from node {}: {}", node_id, exc)
                continue

            if isinstance(incoming, HeartbeatMessage):
                node_manager.update_heartbeat(
                    node_id, incoming.status, incoming.current_jobs
                )
            elif isinstance(incoming, JobResultMessage):
                node_manager.handle_job_result(node_id, incoming)
            elif isinstance(incoming, JobChunkMessage):
                node_manager.handle_job_chunk(node_id, incoming)
            else:
                logger.debug("Ignoring {} from node {}", incoming.type, node_id)

    except WebSocketDisconnect:
        logger.info("Node {} disconnected", node_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Node connection error ({}): {}", node_id, exc)
    finally:
        if node_id:
            await node_manager.unregister_node(node_id)
