"""Клиент guardrail-сервиса: вердикт вероятностный, решения — за кодом.

Недоступность сервиса — не исключение: Verdict(safe=True, available=False).
Тогда чтение продолжается, а флаг хода guard_ok становится false, и
политика не пропускает подготовку карточки (fail-closed).
"""
from __future__ import annotations

from dataclasses import dataclass

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from core import config


@dataclass
class Verdict:
    safe: bool
    category: str = "safe"
    available: bool = True


async def check(text: str, mode: str = "input") -> Verdict:
    """POST /check. Сбой сервиса → safe=True, available=False (деградация)."""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            r = await client.post(f"{config.GUARDRAIL_URL}/check",
                                  json={"text": str(text or ""), "mode": mode})
            r.raise_for_status()
            body = r.json()
    except Exception:  # noqa: BLE001 — guardrail не должен ронять ход
        return Verdict(safe=True, category="unavailable", available=False)
    return Verdict(
        safe=body.get("verdict", "unsafe") == "safe",
        category=body.get("category", ""),
        available=True,
    )
