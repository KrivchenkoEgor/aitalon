import hashlib
import json
import os
import pathlib
import secrets
import time

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import db, gate, qwen_browser, sheet, sources, tickets, zro

load_dotenv(pathlib.Path(db.ROOT) / ".env")
# Креды Qwen живут рядом с самим проектом qwwen; AITalon/.env имеет приоритет.
load_dotenv(pathlib.Path(db.ROOT) / "qwwen" / ".env")

COOKIE = "aitalon_session"
DEV_COOKIE = "aitalon_dev"
ADMIN_COOKIE = "aitalon_admin"
MAX_MESSAGES = 20
MAX_CHARS = 8000

app = FastAPI()
WEB = pathlib.Path(__file__).parent / "web"


@app.on_event("startup")
def startup():
    db.init_db()
    tickets.init()


class Code(BaseModel):
    code: str = ""


class Chat(BaseModel):
    model: str
    messages: list


class DocForm(BaseModel):
    count: int
    talon_hours: float
    doc_hours: float


def who(request: Request):
    return gate.unsign(request.cookies.get(COOKIE, ""))


def device_of(request: Request):
    return request.cookies.get(DEV_COOKIE, "")


def session(request: Request):
    """(код, причина отказа). Причина пуста, если сессия жива.

    Талон из документа проверяется заново на каждый запрос: документ могли
    закрыть посреди разговора, а старый cookie об этом не знает.
    """
    code = who(request)
    if code is None:
        return None, "Войдите по талону."
    if tickets.talon(code):
        ok, reason = tickets.validate(code, device_of(request))
        if not ok:
            return None, reason
        return code, ""
    # Ручной пропуск из таблицы: годен, пока числится в ней.
    if code.isdigit() and db.verify_pass(code):
        return code, ""
    return None, "Талон больше не действует."


def deny(status, detail):
    return JSONResponse({"detail": detail}, status_code=status)


def admin_key():
    return os.environ.get("AITALON_ADMIN_KEY", "")


def _key_hash(given):
    """В cookie кладём хеш, а не ключ: cookie кодируется latin-1, поэтому ключ на
    кириллице в него физически не помещается, и светить сам ключ в браузере незачем."""
    return hashlib.sha256(given.encode("utf-8")).hexdigest()


def _key_matches(given_hash):
    key = admin_key()
    return bool(key) and secrets.compare_digest(given_hash.encode("ascii", "ignore"),
                                                _key_hash(key).encode("ascii"))


def require_admin(request: Request):
    if not admin_key():
        return deny(503, "Операторская закрыта: задайте AITALON_ADMIN_KEY в .env")
    if not _key_matches(request.cookies.get(ADMIN_COOKIE, "")):
        return deny(401, "Нужен ключ оператора.")
    return None


def with_device(request: Request, response: Response):
    if not device_of(request):
        response.set_cookie(DEV_COOKIE, secrets.token_hex(8), httponly=True,
                            samesite="lax", max_age=365 * 86400, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request, response: Response):
    html = (WEB / "index.html").read_text(encoding="utf-8")
    return with_device(request, HTMLResponse(html))


@app.post("/api/enter")
def enter(body: Code, request: Request, response: Response):
    code = body.code.strip().upper()
    if not code:
        return deny(400, "Введите код с листка.")
    device = device_of(request)
    talon = tickets.talon(code)
    if talon:
        ok, msg = tickets.enter(code, device)
        if not ok:
            return deny(403, msg)
        doc = talon["doc"]
        until = talon["activated_at"] or time.time()
        left = int(doc["talon_hours"] * 3600)
        resp = JSONResponse({"ok": True, "code": code, "kind": "талон",
                             "hours": doc["talon_hours"], "label": talon["label"],
                             "total": talon["total"], "doc": doc["num"]})
        resp.set_cookie(COOKIE, gate.sign(code, ttl=left), httponly=True,
                        samesite="lax", max_age=left, path="/")
        return with_device(request, resp)
    # Старый ручной способ: число из таблицы пропусков.
    if not code.isdigit():
        return deny(403, "Такого талона нет. Проверь код с листка.")
    if not db.verify_pass(code):
        return deny(403, "Такого числа нет. Проверь у того, кто его выдавал.")
    resp = JSONResponse({"ok": True, "code": code, "kind": "пропуск"})
    resp.set_cookie(COOKIE, gate.sign(code), httponly=True, samesite="lax",
                    max_age=12 * 3600, path="/")
    return with_device(request, resp)


@app.post("/api/exit")
def sign_out():
    """Гасит сессию. Браузер остаётся тем же устройством — талон за ним."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/me")
def me(request: Request, response: Response):
    code, reason = session(request)
    return with_device(request, JSONResponse(
        {"authorized": code is not None, "code": code, "reason": reason,
         "models": sources.SOURCES}))


@app.get("/api/usage")
def usage(request: Request):
    code, reason = session(request)
    if code is None:
        return deny(401, reason or "Нет доступа.")
    return db.usage_totals()


@app.post("/api/chat")
async def chat(body: Chat, request: Request):
    code, reason = session(request)
    if code is None:
        return deny(403, reason or "Сначала войдите по талону.")
    if body.model not in sources.SOURCES:
        return deny(400, "Неизвестный источник.")
    msgs = [{"role": m.get("role"), "content": str(m.get("content", ""))}
            for m in body.messages if m.get("role") in ("user", "assistant")]
    if not msgs:
        return deny(400, "Пустой разговор.")
    if len(msgs) > MAX_MESSAGES or sum(len(m["content"]) for m in msgs) > MAX_CHARS:
        return deny(400, "Слишком длинный запрос.")

    async def sse():
        async for ev in sources.stream(msgs, body.model, code):
            if ev["type"] == "done":
                u = ev.get("usage") or {}
                if sources.is_qwen(body.model):
                    db.record_usage(code, body.model, 0, 0, 0,
                                    chars_in=u.get("chars_in", 0), chars_out=u.get("chars_out", 0))
                else:
                    db.record_usage(code, body.model, u.get("prompt_tokens", 0),
                                    u.get("completion_tokens", 0), u.get("reasoning_tokens", 0))
            yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- операторская -----------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
def admin():
    """Сама страница ничего не показывает: список она запросит через /admin/documents,
    и вот там уже решает сервер — пускать или нет."""
    return HTMLResponse((WEB / "admin.html").read_text(encoding="utf-8"))


@app.post("/admin/login")
def admin_login(body: dict):
    if not admin_key():
        return deny(503, "Операторская закрыта: задайте AITALON_ADMIN_KEY в .env")
    given = str(body.get("key", ""))
    if not _key_matches(_key_hash(given)):
        return deny(401, "Ключ не тот.")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(ADMIN_COOKIE, _key_hash(given), httponly=True,
                    samesite="lax", max_age=30 * 86400, path="/")
    return resp


@app.get("/admin/documents")
def admin_documents(request: Request):
    blocked = require_admin(request)
    if blocked:
        return blocked
    return {"base_url": sheet.BASE_URL, "documents": tickets.documents()}


@app.post("/admin/documents")
def admin_create(body: DocForm, request: Request):
    blocked = require_admin(request)
    if blocked:
        return blocked
    try:
        doc = tickets.create_document(body.count, body.talon_hours, body.doc_hours)
    except ValueError as exc:
        return deny(400, str(exc))
    return {"ok": True, "document": doc, "sheet": "/documents/{}/sheet".format(doc["id"]),
            "pdf": "/documents/{}.pdf".format(doc["id"])}


@app.post("/admin/documents/{doc_id}/close")
def admin_close(doc_id: int, request: Request):
    blocked = require_admin(request)
    if blocked:
        return blocked
    if not tickets.document(doc_id):
        return deny(404, "Нет такого документа.")
    tickets.close_document(doc_id)
    # Чистка чатов — отдельная фоновая работа движка: десять чатов удаляются десятки
    # секунд, и держать админку открытым запросом нельзя. Прогресс читается
    # следующим эндпоинтом.
    return {"ok": True, "cleanup": qwen_browser.engine().submit_cleanup(
        doc_id, tickets.document_codes(doc_id))}


@app.get("/admin/documents/{doc_id}/cleanup")
def admin_cleanup(doc_id: int, request: Request):
    blocked = require_admin(request)
    if blocked:
        return blocked
    return {"cleanup": qwen_browser.engine().cleanup_status(doc_id)}


@app.get("/documents/{doc_id}/sheet", response_class=HTMLResponse)
def document_sheet(doc_id: int):
    """Печатная форма. Доступ по ссылке без ключа: сам лист не раскрывает ничего,
    чего нет на отпечатанном листке, который уносят с собой."""
    return HTMLResponse(sheet.sheet_html(doc_id))


@app.get("/documents/{doc_id}.pdf")
def document_pdf(doc_id: int):
    if not tickets.document(doc_id):
        return deny(404, "Нет такого документа.")
    return Response(content=sheet.sheet_pdf(doc_id), media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="talony-{}.pdf"'.format(doc_id)})
