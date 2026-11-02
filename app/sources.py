"""Выбор источника ответа: два API-режима через zro и браузерный Qwen."""
from . import qwen_browser, zro

QWEN = qwen_browser.QWEN
SOURCES = zro.MODELS + [QWEN]


def is_qwen(source):
    return source == QWEN


def last_user_text(messages):
    """Qwen ведёт разговор сам, поэтому из истории берём только новую реплику."""
    for m in reversed(messages):
        if m["role"] == "user" and m["content"].strip():
            return m["content"].strip()
    return ""


async def stream(messages, source, code):
    """Один протокол для всех источников: событие content несёт ВЕСЬ текст ответа.

    zro отдаёт настоящий поток кусочков, но интерфейс и так перерисовывает ответ
    целиком, а браузерный Qwen физически не может давать прирост — сайт
    переписывает середину блока. Держать два смысла в одном поле значило бы
    снова ловить дубли, поэтому кусочки zro сворачиваются в нарастающий текст здесь.
    """
    if is_qwen(source):
        text = last_user_text(messages)
        if not text:
            yield {"type": "error", "message": "Нечего отправлять — пустая реплика."}
            return
        async for ev in qwen_browser.stream(text, code):
            yield ev
        return

    full = ""
    async for ev in zro.stream(messages, source):
        if ev["type"] == "content":
            full += ev["text"]
            yield {"type": "content", "text": full}
        else:
            yield ev
