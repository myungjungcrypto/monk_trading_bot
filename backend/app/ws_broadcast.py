"""
대시보드 WebSocket 브로드캐스트.

실시간 PNL, 포지션, 시그널 데이터를 연결된 대시보드에 push합니다.
"""

import asyncio
import json
import logging
from typing import List, Optional

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class DashboardBroadcaster:
    """
    WebSocket 브로드캐스터.

    연결된 대시보드 클라이언트에 실시간 데이터를 전송합니다.
    """

    def __init__(self):
        self._connections: List[WebSocket] = []
        self._bot_engine = None

    def set_bot_engine(self, engine) -> None:
        """봇 엔진 참조를 설정합니다."""
        self._bot_engine = engine

    async def connect(self, websocket: WebSocket) -> None:
        """새 클라이언트 연결을 수락합니다."""
        await websocket.accept()
        self._connections.append(websocket)
        logger.info("Dashboard client connected (total: %d)", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        """클라이언트 연결을 제거합니다."""
        if websocket in self._connections:
            self._connections.remove(websocket)
        logger.info("Dashboard client disconnected (total: %d)", len(self._connections))

    async def broadcast(self, data: dict) -> None:
        """모든 연결된 클라이언트에 데이터를 전송합니다."""
        if not self._connections:
            return

        message = json.dumps(data)
        disconnected = []

        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)

        for ws in disconnected:
            self.disconnect(ws)

    async def start_broadcast_loop(self, interval: float = 1.0) -> None:
        """
        주기적으로 봇 상태를 브로드캐스트합니다.

        Args:
            interval: 브로드캐스트 간격 (초)
        """
        logger.info("Dashboard broadcaster started (interval=%.1fs)", interval)
        while True:
            try:
                if self._connections and self._bot_engine:
                    status = self._bot_engine.get_status()
                    await self.broadcast({
                        "type": "status",
                        "data": status,
                    })
            except Exception as e:
                logger.error("Broadcast error: %s", e)

            await asyncio.sleep(interval)

    @property
    def client_count(self) -> int:
        return len(self._connections)


# 싱글톤 인스턴스
broadcaster = DashboardBroadcaster()
