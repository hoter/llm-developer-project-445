"""Guardrail-сервис: отдельный stateless-контур проверки текста.

    uvicorn --factory guardrail.service:build_app --host 127.0.0.1 --port 8300

Guardrail смотрит текст — реплику абонента (mode=input), ответ и карточку
(mode=output). Права и операции он не решает: там свои барьеры и
политика. Он только ставит флаг «safe/unsafe», и решения принимает код.

Движок по умолчанию — RULE: детерминированные маркеры атак стенда.
Это лабораторная эвристика, для прод её мало: настоящее распознавание
инъекций вероятностное. GUARDRAIL_MODEL=1 включает классификатор
HiveTraceGuard-Pro (если он установлен), но вердикт всё равно сводится
к тому же безопасному бинарному ответу — без вероятностной «серой зоны».

Стендовые /admin/down и /admin/up имитируют отказ сервиса: /check тогда
отвечает 503, /health — 503.
"""
from __future__ import annotations

import os
import re

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# Маркеры атак: (категория, регулярка). Порядок = приоритет категории.
RULE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # джейлбрейк и подмена роли
    ("jailbreak", re.compile(
        r"суперпользовател|суперадмин|\bты\s+теперь\s+(админ|root|систем)|"
        r"отключи\s+(проверк|защит|ограничен|фильтр)|"
        r"jailbreak|дан\s+mode|developer\s+mode|режим\s+разработчика|"
        r"обойди\s+(правила|ограничения|проверки)|ignore\s+(all\s+)?(previous\s+)?instructions",
        re.I,
    )),
    # инъекция инструкций в реплике
    ("prompt_injection", re.compile(
        r"игнорируй\s+(все\s+)?(предыдущ|инструкц|правил)|"
        r"забудь\s+(все\s+)?(инструкц|правил)|"
        r"притворись|ты\s+не\s+ассистент|<\|im_start\|>|<\|system\|>|"
        r"prompt\s+injection",
        re.I,
    )),
    # попытка подтолкнуть модель к инструментам действий
    ("tool_hijack", re.compile(
        r"(смени|поменяй|переведи|подключи|отключи)\s+[^.!?]{0,40}\bв\s+(аккаунт|карточк|профил|тариф)|"
        r"выполни\s+(действие|операц|команд)|"
        r"(вызови|запусти)\s+(инструмент|команду)|"
        r"добавь\s+в\s+карточку",
        re.I,
    )),
    # встроенная инструкция (ловушка в базе знаний, цитата статьи)
    ("injected_instruction", re.compile(
        r"немедленно\s+(переве|смени|отключ|подключ|выполн)|"
        r"без\s+подтверждени|"
        r"срочное\s+указание|"
        r"системн(ая|ое|ые)\s+(инструкц|сообщени)|"
        r"не\s+спрашивай\s+(абонент|пользоват)|"
        r"автоматически\s+переве|"
        r"подтвержд\w*\s+не\s+требу",
        re.I,
    )),
]

ENGINE = "rule"


def _model_engine_enabled() -> bool:
    return os.environ.get("GUARDRAIL_MODEL", "0").strip() in {"1", "true", "yes"}


class ModelEngine:
    """Опциональный классификатор. Без установки — не включается."""

    name = "hivetraceguard-pro"

    def __init__(self) -> None:
        try:  # pragma: no cover - тяжёлые зависимости не в requirements
            from transformers import pipeline  # type: ignore

            self._pipe = pipeline("text-classification", model="HiveTrace/HiveTraceGuard-Pro")
        except Exception as e:  # noqa: BLE001 — нет модели: работаем на RULE
            raise RuntimeError(f"GUARDRAIL_MODEL=1, но классификатор недоступен: {e}") from e

    def classify(self, text: str) -> tuple[bool, str]:  # pragma: no cover
        out = self._pipe(text[:2000], truncation=True)[0]
        unsafe = out.get("label", "").lower() in {"unsafe", "toxic", "block", "label_1"}
        return (not unsafe), ("model_unsafe" if unsafe else "safe")


_MODEL: ModelEngine | None = None


def _model() -> ModelEngine | None:
    global _MODEL
    if not _model_engine_enabled():
        return None
    if _MODEL is None:
        _MODEL = ModelEngine()
    return _MODEL


def rule_verdict(text: str) -> tuple[bool, str]:
    """Детерминированные маркеры → (safe, category)."""
    for category, pattern in RULE_PATTERNS:
        if pattern.search(text or ""):
            return False, category
    return True, "safe"


def analyze(text: str, mode: str) -> dict:
    """Вердикт по тексту. Модель (если включена) дополняет правило."""
    engine = _model()
    safe, category = rule_verdict(text)
    if safe and engine is not None:
        safe, category = engine.classify(text)
    return {
        "verdict": "safe" if safe else "unsafe",
        "category": category,
        "engine": engine.name if engine is not None else ENGINE,
        "mode": mode,
    }


class _State:
    down = False


async def check(request: Request) -> JSONResponse:
    if _State.down:
        return JSONResponse({"error": "guardrail_unavailable"}, status_code=503)
    body = await request.json()
    text = str(body.get("text", ""))
    mode = body.get("mode", "input")
    if mode not in ("input", "output"):
        return JSONResponse({"error": "bad_mode", "detail": "input | output"}, status_code=400)
    return JSONResponse(analyze(text, mode))


async def health(request: Request) -> JSONResponse:
    if _State.down:
        return JSONResponse({"status": "down"}, status_code=503)
    return JSONResponse({"status": "ok", "engine": ENGINE})


async def admin_down(request: Request) -> JSONResponse:
    _State.down = True
    return JSONResponse({"ok": True, "state": "down"})


async def admin_up(request: Request) -> JSONResponse:
    _State.down = False
    return JSONResponse({"ok": True, "state": "up"})


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/check", check, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
            Route("/admin/down", admin_down, methods=["POST"]),
            Route("/admin/up", admin_up, methods=["POST"]),
        ],
    )


if __name__ == "__main__":
    uvicorn.run(build_app(), host="127.0.0.1", port=8300, log_level="warning")
