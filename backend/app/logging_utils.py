import json
import logging
from typing import Any

event_logger = logging.getLogger("mcp_ai.events")
if not event_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    event_logger.addHandler(handler)
event_logger.setLevel(logging.INFO)
event_logger.propagate = False


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    payload = {
        "event": event,
        "component": logger.name,
        **{key: value for key, value in fields.items() if value is not None},
    }
    event_logger.log(level, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
