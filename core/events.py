"""Internal event bus for DAMU Core Engine."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

log = logging.getLogger("core.events")

EventHandler = Callable[..., Awaitable[None]]


class EventBus:
    """Simple asynchronous event bus for engine hooks and audit tracking."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        self._handlers[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: EventHandler) -> None:
        if handler in self._handlers[event_name]:
            self._handlers[event_name].remove(handler)

    async def emit(self, event_name: str, *args: Any, **kwargs: Any) -> None:
        handlers = list(self._handlers.get(event_name, []))
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(*args, **kwargs)
                else:
                    handler(*args, **kwargs)
            except Exception as exc:
                log.error("Error in event handler for %s: %s", event_name, exc, exc_info=True)


bus = EventBus()
