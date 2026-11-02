import os

from openai import AsyncOpenAI

MODELS = ["deepseek-v4.1-flash", "glm-5.3-flash"]

# Ниже ~2000 ставить нельзя: «размышление» считается в этот же бюджет,
# и запрос приходит с HTTP 200, но пустым текстом.
MAX_TOKENS = 2500


class EmptyAnswer(Exception):
    pass


def guard_finish(finish_reason, content_chars):
    if content_chars == 0:
        if finish_reason == "length":
            raise EmptyAnswer("Модель израсходовала весь лимит на размышления и не успела ответить.")
        raise EmptyAnswer("Модель вернула пустой ответ.")


def client():
    key = os.environ.get("ZRO_API_KEY")
    if not key:
        raise RuntimeError("Не задан ZRO_API_KEY — положи его в .env")
    return AsyncOpenAI(base_url=os.environ.get("ZRO_BASE_URL", "https://zro.moonmath.ai/v1"),
                       api_key=key, timeout=120)


def _usage(u):
    if not u:
        return None
    d = getattr(u, "completion_tokens_details", None)
    return {"prompt_tokens": u.prompt_tokens or 0,
            "completion_tokens": u.completion_tokens or 0,
            "reasoning_tokens": getattr(d, "reasoning_tokens", 0) or 0}


async def stream(messages, model):
    state = {"content": 0, "finish": None, "usage": None}
    try:
        chunks = await client().chat.completions.create(
            model=model, messages=messages, max_tokens=MAX_TOKENS,
            stream=True, stream_options={"include_usage": True})
        async for ch in chunks:
            if getattr(ch, "usage", None):
                state["usage"] = _usage(ch.usage)
            if not ch.choices:
                continue
            delta = ch.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield {"type": "reasoning", "text": reasoning}
            if delta.content:
                state["content"] += len(delta.content)
                yield {"type": "content", "text": delta.content}
            if ch.choices[0].finish_reason:
                state["finish"] = ch.choices[0].finish_reason
        guard_finish(state["finish"], state["content"])
        yield {"type": "done", "usage": state["usage"]}
    except EmptyAnswer as e:
        yield {"type": "error", "message": str(e)}
    except Exception as e:
        yield {"type": "error", "message": "Ответ не пришёл: " + str(e)[:200]}
