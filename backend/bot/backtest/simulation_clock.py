"""
Simulation Clock — 백테스트용 시간 관리.

time.time() 대신 시뮬레이션 시간을 제공합니다.
"""


class SimulationClock:
    """백테스트 시뮬레이션 시간."""

    def __init__(self):
        self._now: float = 0.0  # Unix timestamp (seconds)

    @property
    def now(self) -> float:
        return self._now

    def set(self, timestamp_s: float) -> None:
        self._now = timestamp_s

    def set_ms(self, timestamp_ms: int) -> None:
        self._now = timestamp_ms / 1000.0

    def strftime(self, fmt: str = "%Y-%m-%d") -> str:
        import time
        return time.strftime(fmt, time.gmtime(self._now))
