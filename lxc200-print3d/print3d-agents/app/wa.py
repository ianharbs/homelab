"""WhatsApp (GOWA no CT 102, mesmo chip do "iHome Bot") para os agentes do negócio — desde 25/11/2026.

Comunidade "Impressão 3D" (criada pelo Ian no celular) com um grupo por agente (TOPICS do common.py); os grupos
são criados aqui (create_groups) com o Ian como admin e os ids ficam no kv (wa_group_<tipo>).
- Envio: API do GOWA (WA_URL/WA_AUTH/WA_DEVICE no secrets.env).
- Recebimento: o GOWA manda os eventos também para este app (POST :8099/whatsapp, assinado com HMAC
  WA_WEBHOOK_SECRET); só valem mensagens dos nossos grupos e do Ian (WA_OWNER).
- "Botões" do Telegram viram opções numeradas: responder com o número OU reagir com o emoji do botão
  (👍 = ✅, 👎 = ❌). O mapa mensagem -> botões fica no kv (wa_btn_<id>).
"""
import base64
import hashlib
import hmac
import html
import json
import re
import urllib.request
import uuid

from common import E, TOPICS, http, kv_get, kv_set, log

WA_URL = E("WA_URL", "").rstrip("/")
WA_AUTH = E("WA_AUTH", "")
WA_DEVICE = E("WA_DEVICE", "chip")
WA_SECRET = E("WA_WEBHOOK_SECRET", "")
WA_OWNER = re.sub(r"\D", "", E("WA_OWNER", ""))
ON = bool(WA_URL and WA_AUTH)
DESC = {
    "geral": "Conversa com a equipe de agentes. /ajuda lista os comandos.",
    "curadoria": "Modelos sugeridos para vender. Reaja 👍 para aprovar ou 👎 para recusar.",
    "anuncios": "Rascunhos de anúncio (Mercado Livre / Shopee) para aprovar.",
    "estoque": "Filamento e insumos acabando, compras.",
    "pedidos": "Pedidos e prazos. Escreva o pedido aqui para registrar.",
    "relatorios": "Relatório semanal do negócio.",
}
ALIAS = {"👍": "✅", "👌": "✅", "👎": "❌", "🔄": "🔁"}


def _headers(extra=None):
    h = {"Authorization": "Basic " + base64.b64encode(WA_AUTH.encode()).decode(), "X-Device-Id": WA_DEVICE}
    h.update(extra or {})
    return h


def api(method, path, body=None, timeout=30):
    return http(method, WA_URL + path, body=body, headers=_headers(), timeout=timeout)


def num(jid):
    return re.sub(r"\D", "", str(jid or "").split("@")[0].split(":")[0])


def group_of(kind):
    return kv_get(f"wa_group_{kind}", "")


def kind_of_group(jid):
    for k in TOPICS:
        if jid and group_of(k) == jid:
            return k
    return None


def create_groups(max_new=1):
    """Cria até `max_new` grupos que faltam (o chip cria, o Ian entra e vira admin). Idempotente.
    Devagar de propósito: o chip é novo e o WhatsApp devolve 429 (rate-overlimit) se criar vários seguidos."""
    out, novos = {}, 0
    for kind, (name, _) in TOPICS.items():
        jid = group_of(kind)
        if not jid and novos >= max_new:
            continue
        if not jid:
            novos += 1
            r = api("POST", "/group", {"title": name, "participants": [WA_OWNER]})
            jid = (r.get("results") or {}).get("group_id")
            if not jid:
                raise RuntimeError(f"criar {name}: {r}")
            kv_set(f"wa_group_{kind}", jid)
            log.info("whatsapp: grupo %s criado (%s)", name, jid)
            for path in ("/group/participants/promote",):
                try:
                    api("POST", path, {"group_id": jid, "participants": [WA_OWNER]})
                except Exception as e:
                    log.warning("whatsapp: promover o Ian em %s: %s", name, e)
            try:
                api("POST", "/group/topic", {"group_id": jid, "topic": DESC.get(kind, "")})
            except Exception as e:
                log.warning("whatsapp: descrição de %s: %s", name, e)
        out[kind] = jid
    return out


def to_wa(text):
    """HTML do Telegram -> formatação do WhatsApp."""
    def link(m):
        url, label = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        return url if html.unescape(label) == html.unescape(url) else f"{label}: {url}"
    t = re.sub(r'<a href="([^"]+)"[^>]*>(.*?)</a>', link, text or "", flags=re.S)
    t = re.sub(r"<pre>(.*?)</pre>", lambda m: "```" + m.group(1) + "```", t, flags=re.S)
    for tag, mark in (("b", "*"), ("strong", "*"), ("i", "_"), ("em", "_"), ("s", "~"), ("code", "`")):
        t = re.sub(rf"<{tag}>(.*?)</{tag}>", lambda m: mark + m.group(1).strip() + mark if m.group(1).strip() else "",
                   t, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def _opcoes(kb):
    """kb do Telegram -> (texto com opções numeradas, lista [(rótulo, callback_data)])."""
    btns = [(b["text"], b["callback_data"]) for row in (kb or {}).get("inline_keyboard", []) for b in row]
    if not btns:
        return "", []
    linhas = [f"{i}. {t}" for i, (t, _) in enumerate(btns, 1)]
    return "\n\n" + "\n".join(linhas) + "\n_Responda com o número ou reaja com o emoji._", btns


def send_text(jid, text, reply_to=None):
    body = {"phone": jid, "message": text}
    if reply_to:
        body["reply_message_id"] = reply_to
    return (api("POST", "/send/message", body).get("results") or {}).get("message_id")


def send_image(jid, caption, data):
    boundary = uuid.uuid4().hex
    fields = [("phone", jid), ("caption", caption), ("compress", "false")]
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="img.jpg"\r\n'
                 f"Content-Type: image/jpeg\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(WA_URL + "/send/image", data=b"".join(parts), method="POST",
                                 headers=_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}))
    with urllib.request.urlopen(req, timeout=60) as r:
        return (json.loads(r.read()).get("results") or {}).get("message_id")


def send(jid, text, photo=None, kb=None, reply_to=None):
    """Manda (com 'botões' como opções) e devolve {"message_id": id} como o Telegram."""
    extra, btns = _opcoes(kb)
    msg = to_wa(text) + extra
    mid = None
    if photo:
        try:
            mid = send_image(jid, msg[:1024] if len(msg) <= 1024 else to_wa(text)[:900] + extra, http("GET", photo, raw=True, timeout=30))
        except Exception as e:
            log.warning("whatsapp: imagem falhou (%s), mandando texto", e)
    if not mid:
        for i in range(0, max(len(msg), 1), 4000):
            mid = send_text(jid, msg[i:i + 4000], reply_to=reply_to if i == 0 else None)
    if mid and btns:
        kv_set(f"wa_btn_{mid}", json.dumps(btns, ensure_ascii=False))
    return {"message_id": mid} if mid else None


def botao(alvo, escolha):
    """callback_data escolhido numa mensagem com opções: número (1, 2…) ou emoji do rótulo."""
    raw = kv_get(f"wa_btn_{alvo}") if alvo else None
    if not raw:
        return None
    btns = json.loads(raw)
    e = escolha.strip()
    if e.isdigit() and 1 <= int(e) <= len(btns):
        return btns[int(e) - 1][1]
    e = ALIAS.get(re.sub("[\U0001F3FB-\U0001F3FF️]", "", e), re.sub("[\U0001F3FB-\U0001F3FF️]", "", e))
    for label, data in btns:
        if e and label.startswith(e):
            return data
    return None


def check_sig(raw, sig):
    if not WA_SECRET:
        return False
    want = hmac.new(WA_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, (sig or "").removeprefix("sha256="))


def baixar(message_id, chat, path):
    """Baixa o anexo de uma mensagem pelo GOWA (download automático do GOWA é desligado)."""
    r = api("GET", f"/message/{message_id}/download?phone={chat}", timeout=120).get("results") or {}
    fp = r.get("file_path") or r.get("path") or ""
    url = fp if fp.startswith("http") else f"{WA_URL}/{fp.lstrip('/')}"
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as f:
        f.write(resp.read())
    return path
