"""
playback_state.py
Tracks unified playback state across SageTV and ChannelsDVR.
Provides polling, state access, and change subscriptions.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from models.playback import PlaybackState

logger = logging.getLogger(__name__)


class PlaybackStateTracker:
    """
    Polls SageTV and ChannelsDVR for playback state and notifies subscribers
    when state changes.
    """

    def __init__(
        self,
        orchestrator: Any,
        poll_interval: float = 2.0,
    ) -> None:
        self.orchestrator = orchestrator
        self.poll_interval = poll_interval
        self._state: Dict[str, PlaybackState] = {}
        self._subscribers: List[Callable[[str, PlaybackState], Awaitable[None]]] = []
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start the polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Playback state polling started (interval=%.1fs)", self.poll_interval)

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Playback state polling stopped")

    def get_state(self, target: str | None = None) -> Dict[str, PlaybackState]:
        """
        Return current playback state.
        If target is specified, returns a single-key dict for that target.
        """
        if target:
            state = self._state.get(target)
            if state:
                return {target: state}
            return {}
        return dict(self._state)

    async def update_state(self, target: str, state: PlaybackState) -> None:
        """Manually update the state for a target and notify subscribers."""
        self._state[target] = state
        logger.debug("State updated for %s: playing=%s pos=%d", target, state.playing, state.position)
        await self._notify(target, state)

    def subscribe(self, callback: Callable[[str, PlaybackState], Awaitable[None]]) -> None:
        """Register a callback for state changes. Signature: async fn(target, state)."""
        self._subscribers.append(callback)
        logger.debug("Subscriber added (total=%d)", len(self._subscribers))

    def unsubscribe(self, callback: Callable[[str, PlaybackState], Awaitable[None]]) -> None:
        """Remove a previously registered callback."""
        self._subscribers = [s for s in self._subscribers if s is not callback]

    async def _poll_loop(self) -> None:
        """Continuously poll backends for playback state."""
        while self._running:
            for target in ("sagetv", "channels"):
                try:
                    result = await self.orchestrator.execute(f"{target}.status", {})
                    if "error" not in result:
                        new_state = PlaybackState(
                            playing=result.get("playing", False),
                            position=result.get("position", 0),
                            duration=result.get("duration"),
                            title=result.get("title"),
                            channel=result.get("channel"),
                            program_id=result.get("program_id"),
                        )
                        old_state = self._state.get(target)
                        if old_state != new_state:
                            self._state[target] = new_state
                            await self._notify(target, new_state)
                except Exception:
                    logger.debug("Poll failed for %s", target, exc_info=True)
            await asyncio.sleep(self.poll_interval)

    async def _notify(self, target: str, state: PlaybackState) -> None:
        """Invoke all subscriber callbacks."""
        for callback in self._subscribers:
            try:
                await callback(target, state)
            except Exception:
                logger.debug("Subscriber callback error", exc_info=True)
