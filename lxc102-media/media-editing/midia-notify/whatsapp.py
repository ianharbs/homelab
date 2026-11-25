"""WhatsApp pelo GOWA (container `whatsapp`, conta do chip do bot pareada como aparelho conectado).

- post(kind, ...): avisos de mídia nos grupos da Comunidade iHome, criados por create_groups() (jid no kv
  wa_group_<tipo>); WA_GROUP_<TIPO>=<jid>@g.us força à mão.
  Quem participa de cada grupo é decidido no próprio WhatsApp (admins da Comunidade), não aqui.
- on_webhook(raw, sig): perguntas de status. No grupo Status responde tudo; nos outros só comando (/fila)
  ou menção ao bot; no privado só números em WA_ALLOWED. Mensagens do próprio bot são ignoradas.
"""
import base64
import hashlib
import hmac
import html
import json
import re
import sqlite3
import threading
import time
import urllib.request
import uuid

from common import DB_PATH, E, http, kv_get, kv_set, log

WA_URL = E("WA_URL", "http://whatsapp:3000").rstrip("/")
WA_AUTH = E("WA_AUTH", "")  # usuário:senha do APP_BASIC_AUTH do GOWA
WA_DEVICE = E("WA_DEVICE", "ihome")
WA_SECRET = E("WA_WEBHOOK_SECRET", "")
WA_ALLOWED = {re.sub(r"\D", "", x) for x in E("WA_ALLOWED", "").split(",") if x.strip()}

_seen = {}
_seen_lock = threading.Lock()


def enabled():
    return bool(WA_AUTH)


def _headers(extra=None):
    h = {"Authorization": "Basic " + base64.b64encode(WA_AUTH.encode()).decode(), "X-Device-Id": WA_DEVICE}
    h.update(extra or {})
    return h


def api(method, path, body=None, timeout=30):
    return http(method, WA_URL + path, _headers(), body, timeout=timeout)


# Grupos criados pelo próprio bot (create_groups); o jid fica no kv "wa_group_<tipo>". NÃO procura por nome:
# a conta é o número pessoal do Ian, com dezenas de grupos dele (um "Filmes da família" receberia os avisos).
GROUPS = {  # tipo -> (nome, descrição, só admins enviam)
    "series": ("📺 Séries", "Avisos de episódios novos disponíveis no Jellyfin (iHome).", True),
    "filmes": ("🎬 Filmes", "Avisos de filmes novos disponíveis no Jellyfin (iHome).", True),
    "livros": ("📚 Livros", "Avisos de livros e audiolivros que chegaram (Kavita / Audiobookshelf).", True),
    "musica": ("🎵 Música", "Álbuns novos no Navidrome e lançamentos das bandas favoritas.", True),
    "status": ("🤖 Status e Pedidos", "Pergunte: \"cadê o filme Duna?\", \"status da série Severance\", /fila, /recentes. "
               "Peça: \"quero o filme Duna\" ou \"/pedir Severance temporada 2\" e responda com o número da opção.", False),
    # só o Ian, FORA da Comunidade: pedidos para aprovar e prazos vencendo (aprovacoes.py)
    "aprovacoes": ("✅ Aprovações", "Só o Ian. Responda a mensagem com ok / não (pedidos) ou manter (prazo). /prazos lista.", False),
}


def group_of(kind):
    return E(f"WA_GROUP_{kind.upper()}", "") or kv_get(f"wa_group_{kind}", "")


def create_groups():
    """Cria os grupos que faltam (só com o dono da conta) e guarda os jids. Idempotente."""
    out = {}
    for kind, (name, desc, announce) in GROUPS.items():
        jid = group_of(kind)
        if not jid:
            r = api("POST", "/group", {"title": name, "participants": [my_jid()]})  # GOWA exige ≥1 participante
            jid = (r.get("results") or {}).get("group_id") or (r.get("results") or {}).get("jid")
            if not jid:
                raise RuntimeError(f"criar {name}: resposta sem id: {r}")
            kv_set(f"wa_group_{kind}", jid)
            log.info("whatsapp: grupo '%s' criado (%s)", name, jid)
            time.sleep(2)
        for path, body in (("/group/topic", {"group_id": jid, "topic": desc}),
                           ("/group/announce", {"group_id": jid, "announce": announce})):
            try:
                api("POST", path, body)
            except Exception as e:
                log.warning("whatsapp: %s em %s: %s", path, name, e)
        out[kind] = jid
    return out


COMMUNITY = E("WA_COMMUNITY", "120363433701703657@g.us")  # Comunidade iHome (criada pelo Ian no celular)


# ---------------------------------------------------------------- pessoas (usuário do Jellyfin/Seerr -> WhatsApp)
def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS pessoa (usuario TEXT PRIMARY KEY, nome TEXT, telefone TEXT, jid TEXT,"
              " convite_em REAL)")
    return c


def resolve_jid(telefone):
    """Número -> usuário exato do WhatsApp. Contas antigas no Brasil existem SEM o 9 (ex.: 55 47 9xxxx-xxxx existe como 55 47 xxxx-xxxx),
    então testa com e sem o 9 e fica com o que tem aparelho registrado."""
    n = re.sub(r"\D", "", telefone)
    if len(n) in (10, 11):  # sem o 55
        n = "55" + n
    cands = [n]
    if n.startswith("55") and len(n) == 13 and n[4] == "9":
        cands.append(n[:4] + n[5:])
    elif n.startswith("55") and len(n) == 12:
        cands.append(n[:4] + "9" + n[4:])
    for i, cnd in enumerate(cands):
        if i:
            time.sleep(4)  # WhatsApp devolve 429 (rate-overlimit) com consultas seguidas — e a conta é pessoal
        try:
            d = (api("GET", f"/user/info?phone={cnd}").get("results") or {}).get("data") or []
            devs = d[0].get("devices") if d else None
            if devs:
                return str(devs[0].get("User") or cnd)
        except Exception as e:
            log.warning("whatsapp: /user/info %s: %s", cnd, e)
    return ""


def pessoa_set(usuario, telefone, nome=None):
    jid = resolve_jid(telefone)
    if not jid:
        return f"❌ {telefone} não tem WhatsApp (testei com e sem o 9)."
    with _db() as c:
        c.execute("INSERT INTO pessoa (usuario, nome, telefone, jid) VALUES (?,?,?,?) ON CONFLICT(usuario) DO UPDATE"
                  " SET nome=COALESCE(excluded.nome, pessoa.nome), telefone=excluded.telefone, jid=excluded.jid",
                  (usuario.lower(), nome, telefone, jid))
    return f"✅ {nome or usuario}: {jid}"


def pessoa(name):
    """Linha da pessoa pelo usuário do Jellyfin/Seerr ou pelo nome (sem diferenciar maiúsculas)."""
    if not name:
        return None
    with _db() as c:
        return c.execute("SELECT * FROM pessoa WHERE usuario=? OR lower(nome)=?",
                         (name.lower(), name.lower())).fetchone()


def mencao(usuario):
    """<a href="wa:jid">Nome</a> (vira @menção no WhatsApp) ou só o nome/usuário."""
    p = pessoa(usuario)
    if p and p["jid"]:
        return f'<a href="wa:{p["jid"]}">{html.escape(p["nome"] or usuario)}</a>'
    return html.escape(usuario)


def pessoa_por_jid(num):
    if not num:
        return None
    with _db() as c:
        return c.execute("SELECT * FROM pessoa WHERE jid=?", (str(num).split("@")[0].split(":")[0],)).fetchone()


def pessoas():
    with _db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM pessoa ORDER BY usuario")]


def convidar(usuario):
    p = pessoa(usuario)
    if not p:
        return f"❌ {usuario} não tem telefone cadastrado (use /pessoa {usuario} <telefone> [nome])."
    link = (api("GET", f"/group/invite-link?group_id={COMMUNITY}").get("results") or {}).get("invite_link")
    primeiro = (p["nome"] or p["usuario"]).split()[0]
    send_text(p["jid"], f"Oi, {primeiro}! 👋 Aqui é o *iHome* 🏠\n\n"
                        "Criamos uma comunidade no WhatsApp com os avisos do servidor: quando chega filme ou série "
                        "nova, e quando o que você pediu fica disponível (você é marcado no aviso).\n\n"
                        f"Entre por aqui: {link}\n\n"
                        "Lá dentro, no grupo 🤖 Status e Pedidos, dá para perguntar (\"cadê o filme Duna?\") "
                        "e pedir filmes e séries (\"quero o filme Duna\").")
    with _db() as c:
        c.execute("UPDATE pessoa SET convite_em=? WHERE usuario=?", (time.time(), p["usuario"]))
    return f"📨 Convite enviado para {p['nome'] or p['usuario']} ({p['jid']})."


def admin_cmd(text):
    """Comandos só do dono da conta: /pessoa usuario telefone [nome], /convidar usuario|todos, /pessoas."""
    cmd, _, arg = text.partition(" ")
    cmd, args = cmd.lower(), arg.split()
    if cmd == "/pessoas":
        rows = pessoas()
        return "\n".join(f"• {r['usuario']} ({r['nome'] or '-'}): {r['jid']}"
                         f"{' — convidado ' + time.strftime('%d/%m', time.localtime(r['convite_em'])) if r['convite_em'] else ''}"
                         for r in rows) or "Ninguém cadastrado. Use /pessoa <usuario> <telefone> [nome]."
    if cmd == "/pessoa":
        if len(args) < 2:
            return "Uso: /pessoa <usuario do Jellyfin> <telefone com DDD> [nome]"
        return pessoa_set(args[0], args[1], " ".join(args[2:]) or None)
    if cmd == "/convidar":
        if not args:
            return "Uso: /convidar <usuario> | /convidar todos (só quem ainda não foi convidado)"
        alvos = [r["usuario"] for r in pessoas() if not r["convite_em"]] if args[0].lower() == "todos" else [args[0]]
        out = []
        for u in alvos:
            out.append(convidar(u))
            time.sleep(3)
        return "\n".join(out) or "Todo mundo já foi convidado."
    return None


def to_wa(text):
    """HTML do Telegram -> formatação do WhatsApp."""
    def link(m):
        url, label = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        if url.startswith("wa:"):
            return "@" + url[3:]
        if url.startswith("tg://"):
            return label
        return url if html.unescape(label) == html.unescape(url) else f"{label}: {url}"
    t = re.sub(r'<a href="([^"]+)"[^>]*>(.*?)</a>', link, text, flags=re.S)
    for tag, mark in (("b", "*"), ("strong", "*"), ("i", "_"), ("em", "_"), ("s", "~"), ("code", "`")):
        t = re.sub(rf"<{tag}>(.*?)</{tag}>", lambda m: mark + m.group(1).strip() + mark if m.group(1).strip() else "",
                   t, flags=re.S)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def mentions_of(text):
    return re.findall(r'<a href="wa:(\d+)"', text or "")


def send_text(jid, text, reply_to=None, mentions=None):
    body = {"phone": jid, "message": text}
    if mentions:
        body["mentions"] = mentions
    if reply_to:
        body["reply_message_id"] = reply_to
    return api("POST", "/send/message", body)


def send_image(jid, caption, url=None, data=None, mentions=None):
    boundary = uuid.uuid4().hex
    fields = [("phone", jid), ("caption", caption), ("compress", "false")]
    fields += [("mentions", m) for m in mentions or []]
    if url and not data:
        fields.append(("image_url", url))
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields]
    if data:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="capa.jpg"\r\n'
                     f"Content-Type: image/jpeg\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(WA_URL + "/send/image", data=b"".join(parts), method="POST",
                                 headers=_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def post(kind, text, photo=None, photo_bytes=None):
    jid = group_of(kind)
    msg, men = to_wa(text), mentions_of(text)
    if not jid:
        log.info("[whatsapp: sem grupo de '%s'] mensagem:\n%s", kind, msg)
        return True
    try:
        if photo or photo_bytes:
            try:
                data = photo_bytes
                if photo and not data:  # baixa aqui: o GOWA às vezes não consegue baixar capas (TMDB/Deezer)
                    data = http("GET", photo, raw=True, timeout=30)
                send_image(jid, msg, data=data, mentions=men)
                return True
            except Exception as e:
                log.warning("whatsapp: imagem falhou (%s), mandando só texto", e)
        send_text(jid, msg, mentions=men)
        return True
    except Exception as e:
        log.error("whatsapp (%s): %s", kind, e)
        return False


# ---------------------------------------------------------------- perguntas (webhook)
def my_jid():
    j = kv_get("wa_my_jid", "")
    if j:
        return j
    try:
        for d in api("GET", "/devices").get("results") or []:
            if d.get("id") == WA_DEVICE and d.get("jid"):
                j = d["jid"].split(":")[0].split("@")[0]
                kv_set("wa_my_jid", j)
    except Exception as e:
        log.warning("whatsapp: /devices: %s", e)
    return j


def check_sig(raw, sig):
    if not WA_SECRET:
        return False
    want = hmac.new(WA_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, (sig or "").removeprefix("sha256="))


def on_webhook(raw, sig):
    """Chamado pelo servidor HTTP do notify.py. Retorna o código HTTP."""
    if not check_sig(raw, sig):
        log.warning("whatsapp: webhook com assinatura inválida")
        return 403
    try:
        ev = json.loads(raw or b"{}")
    except ValueError:
        return 400
    if E("WA_DEBUG") == "1":
        log.info("whatsapp evento: %s", raw[:1500])
    if ev.get("event") == "message":
        threading.Thread(target=_handle, args=(ev.get("payload") or {},), daemon=True).start()
    return 200


def _handle(p):
    import statusbot  # aqui para evitar import circular
    mid, chat, text = p.get("id"), p.get("chat_id") or "", (p.get("body") or "").strip()
    if not text or not mid:
        return
    if chat and chat == group_of("aprovacoes"):  # grupo só do Ian: respostas às mensagens do bot
        if not p.get("is_from_me") or mid in _seen:
            return
        _seen[mid] = time.time()
        import aprovacoes
        try:
            out = aprovacoes.on_reply(p.get("replied_to_id"), text)
        except Exception as e:
            log.exception("aprovações: %s", e)
            out = f"⚠️ Erro: {e}"
        if out:
            send_text(chat, to_wa(out) if "<" in out else out, reply_to=mid)
        return
    # Conta do bot = número do Ian (provisório): mensagens "minhas" só contam se forem comando (/fila...).
    # As respostas do bot nunca começam com "/", então não entra em loop.
    if p.get("is_from_me") and not text.startswith("/") and not (p.get("replied_to_id") and text.strip().isdigit()):
        return
    with _seen_lock:  # o GOWA pode reenviar o mesmo evento
        now = time.time()
        for k in [k for k, t in _seen.items() if now - t > 600]:
            _seen.pop(k)
        if mid in _seen:
            return
        _seen[mid] = now
    me = my_jid()
    mentioned = text.startswith("/") or any(me and me in str(m) for m in p.get("mentions") or [])
    # A conta é o número pessoal do Ian: só os grupos do iHome e a conversa dele consigo mesmo contam.
    # (Um "/algo" dele no grupo da família ou no privado de um amigo NÃO pode virar resposta do bot.)
    if chat.endswith("@g.us"):
        if chat not in {group_of(k) for k in GROUPS}:
            return
        if chat != group_of("status") and not mentioned:
            return
    elif p.get("is_from_me"):
        if chat.split("@")[0].split(":")[0] != me:
            return
    else:
        num = re.sub(r"\D", "", (p.get("from") or chat).split("@")[0].split(":")[0])
        if num not in WA_ALLOWED:
            log.info("whatsapp: privado de número não autorizado %s (%s)", num, p.get("from_name"))
            return
    if me:
        text = re.sub(rf"@{me}\b", "", text).strip()
    if not text:
        return
    if chat == group_of("status") or (not chat.endswith("@g.us")):
        import pedidos
        try:
            out = pedidos.escolher(p.get("replied_to_id"), text, p) if text.strip().isdigit() else None
            dados = None
            if out is None and pedidos.eh_pedido(text):
                out, dados = pedidos.pedir(p, text)
        except Exception as e:
            log.exception("whatsapp pedido: %s", e)
            out, dados = f"Deu erro no pedido: {e}", None
        if out is not None:
            log.info("whatsapp: pedido de %s: %s", p.get("sender_display_name") or p.get("from_name"), text)
            try:
                r = send_text(chat, out, reply_to=mid)
                pedidos.ligar((r.get("results") or {}).get("message_id"), dados)
            except Exception as e:
                log.error("whatsapp: resposta do pedido falhou: %s", e)
            return
    log.info("whatsapp: pergunta de %s: %s", p.get("sender_display_name") or p.get("from_name"), text)
    try:
        out = admin_cmd(text) if p.get("is_from_me") else None
        if out is None:  # só o Ian vê livros e música
            out = statusbot.answer(text, so_video=not p.get("is_from_me"))
    except Exception as e:
        log.exception("whatsapp status: %s", e)
        out = f"Deu erro ao consultar: {e}"
    try:
        for i in range(0, max(len(out), 1), 4000):
            send_text(chat, out[i:i + 4000] or "…", reply_to=mid)
    except Exception as e:
        log.error("whatsapp: resposta falhou: %s", e)
