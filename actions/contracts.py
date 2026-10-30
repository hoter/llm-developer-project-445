"""Контракты действий: схемы, нормализация, таксономия ошибок.

Три класса исходов действия:

* ошибка валидации (unknown_tariff, bad_phone, …) — возвращается модели
  машиночитаемо с подсказкой, по которой она исправляет вызов;
* отказ бизнес-правила (BUSINESS_RULE_CODES) — повтор бессмысленен,
  модель пересказывает отказ абоненту;
* сбой инфраструктуры до модели не доходит: клиент один раз повторяет
  вызов, иначе это исключение MCP-сервера.
"""
from __future__ import annotations

import re

# Повтор такого вызова бессмысленен: состояние уже такое, как просят.
BUSINESS_RULE_CODES = frozenset({
    "same_tariff",
    "tariff_not_available",
    "service_already_on",
    "service_already_off",
    "same_contacts",
    "nothing_to_update",
    "not_found",
})

# Ошибки валидации: модель читает hints и исправляет аргументы.
VALIDATION_CODES = frozenset({
    "unknown_tariff",
    "unknown_service",
    "bad_phone",
    "bad_email",
    "empty_ticket",
    "unknown_action",
    "bad_details",
})


class ActionError(Exception):
    """Отказ действия: код, сообщение для модели и подсказки.

    to_tool_result() — то, что видит модель: код и данные для исправления.
    """

    def __init__(self, code: str, message: str = "", **hints) -> None:
        self.code = code
        self.message = message or code
        self.hints = hints
        super().__init__(self.message)

    @property
    def is_business_rule(self) -> bool:
        return self.code in BUSINESS_RULE_CODES

    def to_tool_result(self) -> dict:
        return {"error": self.code, "detail": self.message, **self.hints}

    def __repr__(self) -> str:  # в логах видно код и подсказки
        return f"ActionError({self.code!r}, {self.hints!r})"


def _fold(text: str) -> str:
    """Сравнимая форма: нижний регистр, ё → е, без лишних пробелов."""
    return re.sub(r"\s+", " ", str(text or "").strip().lower().replace("ё", "е"))


def _stem(word: str) -> str:
    """Грубая основа для падежей: «музыку» → «музык», «музыка» → «музык»."""
    word = _fold(word)
    return word[:-1] if len(word) > 3 and word[-1] in "аяыиоуеюь" else word


def _like(word_a: str, word_b: str) -> bool:
    """Одна основа в падежах: «семейного» ~ «семейный», «музыку» ~ «музыка»."""
    a, b = _stem(word_a), _stem(word_b)
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    prefix = min(len(a), len(b), 5)
    return prefix >= 4 and a[:prefix] == b[:prefix]


def _close(raw: str, candidates: list[str]) -> str | None:
    """Точное совпадение или однозначное совпадение по основе.

    Модель слышит «музыку», «семейный тариф», «тариф семейный», «семейного» —
    все формы приводятся к точному имени из каталога. Неоднозначность
    (несколько кандидатов) — не угадываем: подсказка available.
    """
    wanted = _fold(raw)
    if not wanted:
        return None
    for name in candidates:
        if _fold(name) == wanted:
            return name
    wanted_words = wanted.split()
    hits = []
    for name in candidates:
        folded = _fold(name)
        name_words = folded.split()
        if wanted in folded or folded in wanted:
            hits.append(name)
        elif any(_like(w, n) for w in wanted_words for n in name_words):
            hits.append(name)
    return hits[0] if len(hits) == 1 else None


def normalize_tariff(raw: str, catalog: list[dict]) -> str:
    """«семейный», «Семейный тариф», «semejnyi» → точное имя из каталога.

    catalog — строки таблицы tariffs (name, available). Неизвестное имя →
    ActionError("unknown_tariff", available=[...]) — модель предложит
    существующие. Архивный тариф — отказ бизнес-правила.
    """
    names = [row["name"] for row in catalog]
    name = _close(raw, names)
    if name is None:
        raise ActionError(
            "unknown_tariff",
            f"тарифа «{raw}» нет в каталоге",
            requested=str(raw),
            available=[row["name"] for row in catalog if row["available"]],
            unavailable=[row["name"] for row in catalog if not row["available"]],
        )
    row = next(r for r in catalog if r["name"] == name)
    if not row["available"]:
        raise ActionError(
            "tariff_not_available",
            f"тариф «{name}» архивный, подключить его нельзя",
            tariff=name,
            available=[r["name"] for r in catalog if r["available"]],
        )
    return name


def normalize_service(raw: str, catalog: list[str]) -> str:
    """«музыку», «музыка» → «Музыка»; неизвестное → unknown_service."""
    name = _close(raw, catalog)
    if name is None:
        raise ActionError(
            "unknown_service",
            f"услуги «{raw}» нет в каталоге",
            requested=str(raw),
            available=list(catalog),
        )
    return name


def normalize_phone(raw: str) -> str:
    """Телефон → «+7 900 111-22-33»; 11 цифр, иначе bad_phone с примером."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        raise ActionError(
            "bad_phone",
            "телефон должен содержать 11 цифр: +7 и десять цифр",
            example="+7 900 123-45-67",
        )
    return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def normalize_email(raw: str) -> str:
    """Почта в нижнем регистре; кривая → bad_email с примером."""
    value = str(raw or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-zА-Яа-я]{2,}", value):
        raise ActionError(
            "bad_email",
            "почта не похожа на адрес",
            example="anna@example.ru",
        )
    return value.lower()
