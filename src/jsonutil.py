from __future__ import annotations

from typing import Any

import json


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(s: str | None) -> Any:
    if not s:
        return None
    return json.loads(s)

