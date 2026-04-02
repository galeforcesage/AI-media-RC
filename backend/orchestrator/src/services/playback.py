"""
playback.py
High-level playback service that abstracts SageTV + ChannelsDVR.
Integrates with external playback endpoints through the orchestrator.
"""

from __future__ import annotations
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class PlaybackService:
    """Unified playback abstraction across media backends."""

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def play(self, target: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Start playback on the specified backend.

        Args:
            target: Backend name ("sagetv" or "channels").
            payload: Parameters (e.g. program_id, channel).
        """
        logger.info("Playback play: target=%s", target)
        try:
            return await self.orchestrator.execute(f"{target}.play", payload)
        except Exception as exc:
            logger.exception("Playback play failed")
            return {"error": str(exc)}

    async def pause(self, target: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Pause playback on the specified backend."""
        logger.info("Playback pause: target=%s", target)
        try:
            return await self.orchestrator.execute(f"{target}.pause", payload or {})
        except Exception as exc:
            logger.exception("Playback pause failed")
            return {"error": str(exc)}

    async def stop(self, target: str) -> Dict[str, Any]:
        """Stop playback on the specified backend."""
        logger.info("Playback stop: target=%s", target)
        try:
            return await self.orchestrator.execute(f"{target}.stop", {})
        except Exception as exc:
            logger.exception("Playback stop failed")
            return {"error": str(exc)}

    async def seek(self, target: str, position: int) -> Dict[str, Any]:
        """
        Seek to a position on the specified backend.

        Args:
            target: Backend name.
            position: Position in seconds.
        """
        logger.info("Playback seek: target=%s position=%d", target, position)
        try:
            return await self.orchestrator.execute(
                f"{target}.seek", {"position": position}
            )
        except Exception as exc:
            logger.exception("Playback seek failed")
            return {"error": str(exc)}

    async def status(self, target: str) -> Dict[str, Any]:
        """Query current playback status for a backend."""
        logger.info("Playback status: target=%s", target)
        try:
            return await self.orchestrator.execute(f"{target}.status", {})
        except Exception as exc:
            logger.exception("Playback status query failed")
            return {"error": str(exc)}
