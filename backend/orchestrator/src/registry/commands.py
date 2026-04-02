"""
commands.py
Central command registry for the orchestrator.
Maps command names to handler callables with schema validation and introspection.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

CommandHandler = Callable[..., Awaitable[Dict[str, Any]]]


@dataclass
class CommandDefinition:
    """Definition of a single registered command."""

    name: str
    namespace: str
    description: str
    schema: Dict[str, Any] = field(default_factory=dict)
    handler: Optional[CommandHandler] = None

    @property
    def full_name(self) -> str:
        """Return the fully-qualified 'namespace.name' identifier."""
        return f"{self.namespace}.{self.name}"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable description."""
        return {
            "name": self.full_name,
            "namespace": self.namespace,
            "description": self.description,
            "schema": self.schema,
            "has_handler": self.handler is not None,
        }


class CommandRegistry:
    """
    Central registry mapping namespaced command names to handler callables.

    Namespaces: sagetv, channels, system, llm, whisper, tts
    """

    VALID_NAMESPACES = {"sagetv", "channels", "system", "llm", "whisper", "tts"}

    def __init__(self) -> None:
        self._commands: Dict[str, CommandDefinition] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        namespace: str,
        name: str,
        description: str,
        schema: Dict[str, Any] | None = None,
        handler: CommandHandler | None = None,
    ) -> CommandDefinition:
        """Register a command under a namespace."""
        if namespace not in self.VALID_NAMESPACES:
            raise ValueError(
                f"Invalid namespace '{namespace}'. "
                f"Must be one of: {', '.join(sorted(self.VALID_NAMESPACES))}"
            )

        cmd = CommandDefinition(
            name=name,
            namespace=namespace,
            description=description,
            schema=schema or {},
            handler=handler,
        )
        self._commands[cmd.full_name] = cmd
        logger.info("Registered command: %s", cmd.full_name)
        return cmd

    def unregister(self, full_name: str) -> bool:
        """Remove a command from the registry. Returns True if removed."""
        removed = self._commands.pop(full_name, None)
        if removed:
            logger.info("Unregistered command: %s", full_name)
        return removed is not None

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, full_name: str) -> CommandDefinition | None:
        """Look up a command by its full namespaced name."""
        return self._commands.get(full_name)

    def resolve(self, full_name: str) -> CommandHandler | None:
        """
        Resolve a command name to its handler callable.

        Returns the handler function or None if the command is unknown
        or has no handler attached.
        """
        cmd = self._commands.get(full_name)
        if cmd is None or cmd.handler is None:
            return None
        return cmd.handler

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_commands(self, namespace: str | None = None) -> List[CommandDefinition]:
        """List all registered commands, optionally filtered by namespace."""
        commands = list(self._commands.values())
        if namespace:
            commands = [c for c in commands if c.namespace == namespace]
        return sorted(commands, key=lambda c: c.full_name)

    def describe_command(self, full_name: str) -> Dict[str, Any] | None:
        """Return a JSON-serializable description of a command."""
        cmd = self.get(full_name)
        if cmd is None:
            return None
        return cmd.to_dict()

    def list_namespaces(self) -> List[str]:
        """Return namespaces that have at least one registered command."""
        return sorted({c.namespace for c in self._commands.values()})

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, full_name: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Execute a registered command by name."""
        handler = self.resolve(full_name)
        if handler is None:
            logger.warning("Cannot execute unknown or unhandled command: %s", full_name)
            return {"error": f"Unknown or unhandled command '{full_name}'"}
        try:
            return await handler(payload or {})
        except Exception as exc:
            logger.exception("Command execution failed: %s", full_name)
            return {"error": str(exc)}
