import hashlib
import hmac
import time

from . import db


def _sig(payload, secret):
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:32]


def sign(code, ttl=12 * 3600):
    exp = int(time.time() + ttl)
    payload = f"{code}.{exp}"
    return f"{payload}.{_sig(payload, db.secret())}"


def unsign(token):
    """Возвращает код, только если подпись целая и срок не вышел.

    Годен ли сам код — решает вызывающий: у талона из документа свои часы и своё
    закрепление за устройством, а сверять его с таблицей ручных пропусков бессмысленно.
    """
    if not token or token.count(".") != 2:
        return None
    code, exp, sig = token.split(".")
    # Код талона буквенно-цифровой, старый пропуск — цифровой. Требовать только
    # цифры нельзя: подпись талона перестала бы распознаваться.
    if not (code.isalnum() and exp.isdigit()):
        return None
    if int(exp) < time.time():
        return None
    if not hmac.compare_digest(_sig(f"{code}.{exp}", db.secret()), sig):
        return None
    return code
