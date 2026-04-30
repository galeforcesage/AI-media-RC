"""
entity_context.py
Conversation-scoped entity memory for multi-turn interactions.

Extracts and stores resolved entities (show titles, IDs, channels, etc.)
from tool results so that subsequent queries can reference them via
pronouns or implicit context ("play the next episode", "delete that one").

This is a named subsystem per PRD Gap #2 — not a tool the LLM calls,
but a structured store the orchestrator manages automatically.
"""

from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default entity TTL: 10 minutes (single-user household, short sessions)
DEFAULT_ENTITY_TTL = 600.0


@dataclass
class ResolvedEntity:
    """A single resolved entity from a tool result."""
    entity_type: str       # "show", "recording", "channel", "episode", "genre"
    name: str              # human-readable: "NCIS", "NBC", "S03E17"
    entity_id: str = ""    # backend ID: media_file_id, channel number, etc.
    source_tool: str = ""  # which tool resolved this: "channels_search_recordings"
    backend: str = ""      # "sagetv" or "channelsdvr"
    confidence: float = 1.0  # 1.0 = exact match, 0.5 = fuzzy
    metadata: Dict[str, Any] = field(default_factory=dict)  # extra fields
    resolved_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def __post_init__(self):
        if self.expires_at == 0.0:
            self.expires_at = self.resolved_at + DEFAULT_ENTITY_TTL

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at


class EntityContextStore:
    """
    Conversation-scoped entity memory with TTL and conflict resolution.

    Entities are extracted from tool results after each agent loop iteration.
    On subsequent queries, the orchestrator injects relevant entity context
    into the LLM prompt so pronouns and implicit references resolve correctly.

    Conflict resolution: newer entities with the same (type, name) overwrite
    older ones. Multiple entities of the same type coexist (e.g. two shows).
    """

    def __init__(self, ttl: float = DEFAULT_ENTITY_TTL, max_entities: int = 50) -> None:
        self._entities: List[ResolvedEntity] = []
        self._ttl = ttl
        self._max_entities = max_entities

    def clear(self) -> None:
        """Clear all stored entities."""
        self._entities.clear()

    @property
    def entities(self) -> List[ResolvedEntity]:
        """Return non-expired entities."""
        self._prune()
        return list(self._entities)

    def _prune(self) -> None:
        """Remove expired entities."""
        now = time.time()
        before = len(self._entities)
        self._entities = [e for e in self._entities if e.expires_at > now]
        pruned = before - len(self._entities)
        if pruned:
            logger.debug("EntityContextStore: pruned %d expired entities", pruned)

    def add(self, entity: ResolvedEntity) -> None:
        """Add or update an entity. Newer entities overwrite matching ones."""
        self._prune()
        # Conflict resolution: remove existing entity with same type + name
        self._entities = [
            e for e in self._entities
            if not (e.entity_type == entity.entity_type and e.name.lower() == entity.name.lower())
        ]
        self._entities.append(entity)
        # Cap at max_entities (drop oldest)
        if len(self._entities) > self._max_entities:
            self._entities = self._entities[-self._max_entities:]

    def get_by_type(self, entity_type: str) -> List[ResolvedEntity]:
        """Get all non-expired entities of a given type."""
        self._prune()
        return [e for e in self._entities if e.entity_type == entity_type]

    def get_latest(self, entity_type: str | None = None) -> ResolvedEntity | None:
        """Get the most recently resolved entity, optionally filtered by type."""
        self._prune()
        candidates = self._entities if entity_type is None else [
            e for e in self._entities if e.entity_type == entity_type
        ]
        return candidates[-1] if candidates else None

    def extract_from_tool_result(
        self, tool_name: str, result: Dict[str, Any],
    ) -> List[ResolvedEntity]:
        """Extract entities from a tool result and store them.

        Returns the list of newly extracted entities.
        """
        if not isinstance(result, dict) or result.get("error"):
            return []

        backend = ""
        if tool_name.startswith("sagetv_"):
            backend = "sagetv"
        elif tool_name.startswith("channels_"):
            backend = "channelsdvr"

        data = result.get("data", result)
        extracted: List[ResolvedEntity] = []

        # Handle list of recordings/shows
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("results", "scheduled", "recordings", "items"):
                v = data.get(key)
                if isinstance(v, list):
                    items = v
                    break

        for item in items[:10]:  # cap extraction to avoid bloat
            if not isinstance(item, dict):
                continue
            entities = self._extract_entities_from_item(item, tool_name, backend)
            extracted.extend(entities)

        # Single-item results (e.g. get_recording, get_airing)
        if not items and isinstance(data, dict) and "title" in data:
            extracted.extend(self._extract_entities_from_item(data, tool_name, backend))

        for entity in extracted:
            self.add(entity)

        if extracted:
            logger.info(
                "EntityContextStore: extracted %d entities from %s (total: %d)",
                len(extracted), tool_name, len(self._entities),
            )
        return extracted

    def _extract_entities_from_item(
        self, item: Dict[str, Any], source_tool: str, backend: str,
    ) -> List[ResolvedEntity]:
        """Extract entities from a single result item."""
        entities: List[ResolvedEntity] = []
        now = time.time()
        ttl = self._ttl

        title = item.get("title", "")
        if title:
            entity_id = str(item.get("id", item.get("media_file_id", "")))
            meta = {}
            ep_title = item.get("episode_title", "")
            se = item.get("season_episode", "")
            if ep_title:
                meta["episode_title"] = ep_title
            if se:
                meta["season_episode"] = se
            entities.append(ResolvedEntity(
                entity_type="show",
                name=title,
                entity_id=entity_id,
                source_tool=source_tool,
                backend=backend,
                metadata=meta,
                resolved_at=now,
                expires_at=now + ttl,
            ))

        channel = item.get("channel", "")
        if channel:
            entities.append(ResolvedEntity(
                entity_type="channel",
                name=channel,
                source_tool=source_tool,
                backend=backend,
                resolved_at=now,
                expires_at=now + ttl,
            ))

        return entities

    def format_context_for_prompt(self) -> str:
        """Format stored entities as a context block for the LLM prompt.

        Returns empty string if no entities are stored.
        """
        self._prune()
        if not self._entities:
            return ""

        # Group by type for readable output
        by_type: Dict[str, List[ResolvedEntity]] = {}
        for e in self._entities:
            by_type.setdefault(e.entity_type, []).append(e)

        lines = ["CONVERSATION CONTEXT (recently discussed):"]
        for etype, entities in by_type.items():
            if etype == "show":
                for e in entities[-5:]:  # last 5 shows
                    meta_parts = []
                    if e.metadata.get("season_episode"):
                        meta_parts.append(e.metadata["season_episode"])
                    if e.metadata.get("episode_title"):
                        meta_parts.append(f'"{e.metadata["episode_title"]}"')
                    if e.entity_id:
                        meta_parts.append(f"id={e.entity_id}")
                    meta_str = f" ({', '.join(meta_parts)})" if meta_parts else ""
                    lines.append(f'- Show: "{e.name}"{meta_str} [{e.backend}]')
            elif etype == "channel":
                unique = {e.name for e in entities}
                if unique:
                    lines.append(f"- Channels mentioned: {', '.join(sorted(unique)[:5])}")

        return "\n".join(lines) + "\n"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logging/debugging."""
        self._prune()
        return {
            "entity_count": len(self._entities),
            "entities": [
                {
                    "type": e.entity_type,
                    "name": e.name,
                    "id": e.entity_id,
                    "source": e.source_tool,
                    "backend": e.backend,
                    "age_s": round(time.time() - e.resolved_at, 1),
                }
                for e in self._entities[-10:]  # last 10 for brevity
            ],
        }
