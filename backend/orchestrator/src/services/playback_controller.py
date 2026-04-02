"""
playback_controller.py
High-level playback control layer.
Coordinates playback commands with live state tracking.
Integrates with external playback endpoints.
"""

from __future__ import annotations
import logging
from typing import Any, Dict

from models.playback import PlaybackState
from services.playback_state import PlaybackStateTracker

logger = logging.getLogger(__name__)


class PlaybackController:
    """
    Provides high-level playback operations backed by the orchestrator
    and real-time state from PlaybackStateTracker.
    """

    def __init__(
        self,
        orchestrator: Any,
        state_tracker: PlaybackStateTracker,
        default_target: str = "sagetv",
    ) -> None:
        self.orchestrator = orchestrator
        self.state_tracker = state_tracker
        self.default_target = default_target

    async def play(
        self, target: str | None = None, payload: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        """Start playback on the specified target."""
        t = target or self.default_target
        logger.info("PlaybackController.play target=%s", t)
        try:
            return await self.orchestrator.execute(f"{t}.play", payload or {})
        except Exception as exc:
            logger.exception("play failed")
            return {"error": str(exc)}

    async def pause(self, target: str | None = None) -> Dict[str, Any]:
        """Pause playback on the specified target."""
        t = target or self.default_target
        logger.info("PlaybackController.pause target=%s", t)
        try:
            return await self.orchestrator.execute(f"{t}.pause", {})
        except Exception as exc:
            logger.exception("pause failed")
            return {"error": str(exc)}

    async def stop(self, target: str | None = None) -> Dict[str, Any]:
        """Stop playback on the specified target."""
        t = target or self.default_target
        logger.info("PlaybackController.stop target=%s", t)
        try:
            return await self.orchestrator.execute(f"{t}.stop", {})
        except Exception as exc:
            logger.exception("stop failed")
            return {"error": str(exc)}

    async def seek(self, position: int, target: str | None = None) -> Dict[str, Any]:
        """Seek to a position (seconds) on the specified target."""
        t = target or self.default_target
        logger.info("PlaybackController.seek target=%s position=%d", t, position)
        try:
            return await self.orchestrator.execute(
                f"{t}.seek", {"position": position}
            )
        except Exception as exc:
            logger.exception("seek failed")
            return {"error": str(exc)}

    async def now_playing(self, target: str | None = None) -> Dict[str, PlaybackState]:
        """
        Return the current playback state.
        If target is None, returns state for all backends.
        """
        return self.state_tracker.get_state(target)
