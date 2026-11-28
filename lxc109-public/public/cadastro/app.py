"""Cadastro com aprovação para o Jellyfin (video.ihome.dev.br/cadastro).

Fluxo: visitante pede acesso -> DM no Telegram para o admin com Aprovar/Rejeitar (links assinados) ->
aprovado: convite de uso único no Wizarr, enviado por e-mail (Resend) e mostrado na página de status -> visitante cria a conta
no Wizarr -> este serviço aplica as permissões fixas no Jellyfin e avisa o admin.

Também roda o "guardião de prioridade": se um usuário prioritário estiver assistindo, convidados ficam
limitados a MAX_GUEST_TRANSCODES transcodificações simultâneas (as mais novas são paradas com aviso).

Só usa a biblioteca padrão do Python.
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

E = os.environ.get
PUBLIC_URL = E("PUBLIC_URL", "").rstrip("/")
SECRET = E("APP_SECRET", "").encode()
TG_TOKEN = E("TG_TOKEN", "")
TG_ADMIN = E("TG_ADMIN_CHAT", "")
# Desde 25/11/2026 os pedidos vão para o grupo "✅ Aprovações" do WhatsApp (midia-notify no CT 102, porta 8095,
# token próprio). Com WA_NOTIFY_URL definido o Telegram não é usado; vazio = volta ao Telegram.
WA_NOTIFY_URL = E("WA_NOTIFY_URL", "")
WIZARR_URL = E("WIZARR_URL", "http://wizarr:5690").rstrip("/")
WIZARR_KEY = E("WIZARR_API_KEY", "")
WIZARR_SERVER_ID = int(E("WIZARR_SERVER_ID", "1") or 1)
JF_URL = E("JELLYFIN_URL", "").rstrip("/")
JF_KEY = E("JELLYFIN_API_KEY", "")
LIBRARIES = [x.strip() for x in E("LIBRARIES", "Filmes,Séries,Desenhos,Coleções").split(",") if x.strip()]
MAX_SESSIONS = int(E("MAX_SESSIONS", "2"))
PRIORITY = {x.strip().lower() for x in E("PRIORITY_USERS", "").split(",") if x.strip()}
MAX_GUEST_TRANSCODES = int(E("MAX_GUEST_TRANSCODES", "2"))
INVITE_DAYS = 7
DB_PATH = E("DB_PATH", "/data/cadastro.db")
OPEN = E("ACCEPT_REQUESTS", "true").lower() == "true"
RESEND_KEY = E("RESEND_API_KEY", "")
MAIL_FROM = E("MAIL_FROM", "iHome <convite@ihome.dev.br>")
MAIL_ON_REJECT = E("MAIL_ON_REJECT", "false").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cadastro")
db_lock = threading.Lock()


# ----------------------------------------------------------------------------- infra
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_lock, db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS pedidos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                nome TEXT NOT NULL,
                email TEXT,
                recado TEXT,
                ip TEXT,
                criado TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pendente',   -- pendente|aprovado|rejeitado|concluido
                decidido TEXT,
                convite_codigo TEXT,
                convite_url TEXT,
                convite_expira TEXT,
                tg_msg INTEGER,
                jf_usuario TEXT
            );
            CREATE TABLE IF NOT EXISTS ajustados (jf_id TEXT PRIMARY KEY, nome TEXT, quando TEXT);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            """
        )
        cols = {r[1] for r in con.execute("PRAGMA table_info(pedidos)")}
        if "email_status" not in cols:
            con.execute("ALTER TABLE pedidos ADD COLUMN email_status TEXT")
        if "telefone" not in cols:  # WhatsApp (desde 28/11/2026): convite da Comunidade iHome quando a conta é criada
            con.execute("ALTER TABLE pedidos ADD COLUMN telefone TEXT")


def http_json(method, url, body=None, headers=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def jf(method, path, body=None):
    return http_json(method, JF_URL + path, body, {"Authorization": f'MediaBrowser Token="{JF_KEY}"'})


def wizarr(method, path, body=None):
    return http_json(method, WIZARR_URL + "/api" + path, body, {"X-API-Key": WIZARR_KEY})


def tg(method, payload):
    if not TG_TOKEN or not TG_ADMIN:
        return None
    try:
        return http_json("POST", f"https://api.telegram.org/bot{TG_TOKEN}/{method}", payload)
    except Exception as e:  # noqa: BLE001
        log.warning("telegram %s: %s", method, e)
        return None


def sign(pid, acao):
    return hmac.new(SECRET, f"{pid}:{acao}".encode(), hashlib.sha256).hexdigest()[:40]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt(ts):
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except Exception:  # noqa: BLE001
        return ts or ""


# ----------------------------------------------------------------------------- e-mail
def send_mail(to, subject, html_body, text_body):
    """Envia pelo Resend. Retorna 'enviado' ou a mensagem de erro (ou None se não configurado)."""
    if not RESEND_KEY or not to:
        return None
    try:
        r = http_json("POST", "https://api.resend.com/emails",
                      {"from": MAIL_FROM, "to": [to], "subject": subject, "html": html_body, "text": text_body},
                      {"Authorization": f"Bearer {RESEND_KEY}", "User-Agent": "ihome-cadastro/1.0"})
        log.info("e-mail '%s' para %s: %s", subject, to, (r or {}).get("id"))
        return "enviado"
    except urllib.error.HTTPError as e:
        msg = f"erro {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
    except Exception as e:  # noqa: BLE001
        msg = f"erro: {e}"
    log.warning("e-mail para %s falhou: %s", to, msg)
    return msg


def mail_layout(titulo, corpo_html, botao_txt=None, botao_url=None):
    botao = (f'''<tr><td style="padding:8px 0 24px"><a href="{html.escape(botao_url)}" style="display:inline-block;background:#00a4dc;color:#ffffff;text-decoration:none;font-weight:600;padding:13px 24px;border-radius:9px">{html.escape(botao_txt)}</a></td></tr>'''
             if botao_url else "")
    return f'''<!doctype html><html lang="pt-BR"><body style="margin:0;padding:0;background:#f2f4f7">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f4f7;padding:32px 12px"><tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:14px;padding:28px;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1d2330;font-size:16px;line-height:1.5">
<tr><td style="font-size:22px;font-weight:700;padding-bottom:10px">{html.escape(titulo)}</td></tr>
<tr><td style="padding-bottom:16px;color:#3c4453">{corpo_html}</td></tr>
{botao}
<tr><td style="font-size:12.5px;color:#7a8394;border-top:1px solid #e6e9ef;padding-top:14px">iHome · {html.escape(PUBLIC_URL.replace("https://", ""))}</td></tr>
</table></td></tr></table></body></html>'''


def mail_convite(p):
    primeiro = p["nome"].split()[0]
    corpo = (f"Oi, {html.escape(primeiro)}! Seu pedido de acesso foi <b>aprovado</b>.<br><br>"
             f"Use o botão abaixo para escolher seu usuário e senha. O convite vale por {INVITE_DAYS} dias e só pode ser usado uma vez.<br><br>"
             f"Depois é só entrar em <a href=\"{PUBLIC_URL}\" style=\"color:#0086b3\">{html.escape(PUBLIC_URL.replace('https://', ''))}</a> "
             "ou usar o app Jellyfin (celular/TV) com esse endereço de servidor.")
    texto = (f"Oi, {primeiro}! Seu pedido de acesso foi aprovado.\n\nCrie sua conta (vale {INVITE_DAYS} dias, uso único):\n"
             f"{p['convite_url']}\n\nDepois entre em {PUBLIC_URL} ou use o app Jellyfin com esse endereço de servidor.\n")
    return send_mail(p["email"], "Seu acesso foi aprovado 🎬", mail_layout("Acesso aprovado!", corpo, "Criar minha conta", p["convite_url"]), texto)


def mail_rejeicao(p):
    primeiro = p["nome"].split()[0]
    msg = ("Obrigado pelo interesse! No momento não temos vaga disponível. "
           "Tente fazer o cadastro novamente na semana que vem.")
    corpo = f"Oi, {html.escape(primeiro)}. {msg}"
    texto = f"Oi, {primeiro}. {msg}\n\n{PUBLIC_URL}/cadastro\n"
    return send_mail(p["email"], "Sobre seu pedido de acesso",
                     mail_layout("Sem vagas no momento", corpo, "Pedir de novo", f"{PUBLIC_URL}/cadastro"), texto)


def set_email_status(pid, st):
    if st is not None:
        with db_lock, db() as con:
            con.execute("UPDATE pedidos SET email_status=? WHERE id=?", (st, pid))


# ----------------------------------------------------------------------------- telegram
def tg_text(p):
    linhas = [
        "🎬 <b>Pedido de acesso ao Jellyfin</b>",
        f"<b>Nome:</b> {html.escape(p['nome'])}",
    ]
    if p["email"]:
        linhas.append(f"<b>E-mail:</b> {html.escape(p['email'])}")
    if p["telefone"]:
        linhas.append(f"<b>WhatsApp:</b> {html.escape(fmt_tel(p['telefone']))}")
    if p["recado"]:
        linhas.append(f"<b>Recado:</b> {html.escape(p['recado'])}")
    linhas.append(f"<i>#{p['id']} · {fmt(p['criado'])} · IP {html.escape(p['ip'] or '?')}</i>")
    st = {
        "aprovado": "\n✅ <b>Aprovado</b> — convite enviado para a página do pedido.",
        "rejeitado": "\n❌ <b>Rejeitado</b>.",
        "concluido": f"\n🎉 <b>Conta criada</b>: {html.escape(p['jf_usuario'] or '')}",
    }.get(p["status"], "")
    em = p["email_status"] if "email_status" in p.keys() else None
    if em:
        st += "\n📧 e-mail enviado" if em == "enviado" else f"\n⚠️ e-mail falhou: {html.escape(em[:120])}"
    return "\n".join(linhas) + st


def wa_post(body):
    try:  # o midia-notify responde texto ("ok"), não JSON: vale só o código HTTP
        req = urllib.request.Request(WA_NOTIFY_URL, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        log.warning("whatsapp (midia-notify): %s", e)
        return False


def wa_status_text(p):
    nome = html.escape(p["nome"])
    if p["status"] == "aprovado":
        t = f"✅ <b>{nome}</b> aprovado — convite na página do pedido."
    elif p["status"] == "rejeitado":
        t = f"❌ <b>{nome}</b> rejeitado."
    elif p["status"] == "concluido":
        t = f"🎉 <b>{nome}</b> criou a conta <b>{html.escape(p['jf_usuario'] or '')}</b> no Jellyfin."
    else:
        return ""
    em = p["email_status"] if "email_status" in p.keys() else None
    if em and p["status"] != "concluido":
        t += "\n📧 e-mail enviado" if em == "enviado" else f"\n⚠️ e-mail falhou: {html.escape(em[:120])}"
    return t


def tg_send_pedido(p):
    base = f"{PUBLIC_URL}/cadastro/admin/{p['id']}"
    if WA_NOTIFY_URL and wa_post({
            "evento": "pedido", "id": p["id"], "nome": p["nome"], "email": p["email"], "recado": p["recado"],
            "telefone": fmt_tel(p["telefone"]),
            "ip": p["ip"], "criado": fmt(p["criado"]),
            "aprovar_url": f"{base}?a=aprovar&s={sign(p['id'], 'aprovar')}",
            "rejeitar_url": f"{base}?a=rejeitar&s={sign(p['id'], 'rejeitar')}"}):
        return
    kb = {"inline_keyboard": [[
        {"text": "✅ Aprovar", "url": f"{base}?a=aprovar&s={sign(p['id'], 'aprovar')}"},
        {"text": "❌ Rejeitar", "url": f"{base}?a=rejeitar&s={sign(p['id'], 'rejeitar')}"},
    ]]}
    r = tg("sendMessage", {"chat_id": TG_ADMIN, "text": tg_text(p), "parse_mode": "HTML",
                           "reply_markup": kb, "disable_web_page_preview": True})
    if r and r.get("ok"):
        with db_lock, db() as con:
            con.execute("UPDATE pedidos SET tg_msg=? WHERE id=?", (r["result"]["message_id"], p["id"]))


def tg_update(pid):
    p = get_pedido(id=pid)
    if p and WA_NOTIFY_URL:
        t = wa_status_text(p)
        if t:
            wa_post({"evento": "status", "id": pid, "texto": t})
        if not p["tg_msg"]:
            return
    if p and p["tg_msg"]:
        tg("editMessageText", {"chat_id": TG_ADMIN, "message_id": p["tg_msg"], "text": tg_text(p),
                               "parse_mode": "HTML", "disable_web_page_preview": True})


# ----------------------------------------------------------------------------- domínio
def get_pedido(id=None, token=None):
    with db() as con:
        if id is not None:
            return con.execute("SELECT * FROM pedidos WHERE id=?", (id,)).fetchone()
        return con.execute("SELECT * FROM pedidos WHERE token=?", (token,)).fetchone()


def pedidos_recentes(ip):
    with db() as con:
        desde = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        return con.execute("SELECT COUNT(*) FROM pedidos WHERE ip=? AND criado>=?", (ip, desde)).fetchone()[0]


BLOQUEIO_EMAIL_DIAS = 7


class Bloqueado(Exception):
    """Pedido recusado sem criar registro; a mensagem vai para o formulário."""


def normalizar_tel(t):
    """Telefone digitado -> só dígitos com DDI (55 + DDD + número). None se não parece um celular."""
    t = (t or "").strip()
    n = re.sub(r"\D", "", t)
    if t.startswith("+") and not n.startswith("55"):
        return n if 8 <= len(n) <= 15 else None  # estrangeiro: aceita como veio
    if n.startswith("0"):
        n = n.lstrip("0")
    if len(n) in (10, 11):
        n = "55" + n
    return n if n.startswith("55") and len(n) in (12, 13) else None


def fmt_tel(n):
    if not n:
        return ""
    if n.startswith("55") and len(n) in (12, 13):
        loc = n[4:]
        return f"+55 ({n[2:4]}) {loc[:-4]}-{loc[-4:]}"
    return "+" + n


def criar_pedido(nome, email, recado, ip, telefone=None):
    """Cria o pedido (verificação e inserção atômicas).

    - mesmo IP+nome em 30 min ainda pendente/aprovado (clique duplo, F5) -> devolve o pedido existente;
    - mesmo e-mail com pedido pendente/aprovado nos últimos 7 dias -> devolve o pedido existente;
    - mesmo e-mail rejeitado nos últimos 7 dias -> Bloqueado até completar 7 dias do pedido;
    - e-mail que já virou conta -> Bloqueado.
    """
    email = email.strip().lower()
    token = secrets.token_urlsafe(24)
    agora = datetime.now()
    desde30m = (agora - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    desde7d = (agora - timedelta(days=BLOQUEIO_EMAIL_DIAS)).strftime("%Y-%m-%d %H:%M:%S")
    with db_lock, db() as con:
        if con.execute("SELECT 1 FROM pedidos WHERE lower(email)=? AND status='concluido'", (email,)).fetchone():
            raise Bloqueado("Este e-mail já tem uma conta. Entre direto pelo site ou pelo app Jellyfin.")
        dup = (con.execute("SELECT id FROM pedidos WHERE ip=? AND lower(nome)=lower(?) AND criado>=? "
                           "AND status IN ('pendente','aprovado') ORDER BY id LIMIT 1", (ip, nome, desde30m)).fetchone()
               or con.execute("SELECT id FROM pedidos WHERE lower(email)=? AND criado>=? "
                              "AND status IN ('pendente','aprovado') ORDER BY id LIMIT 1", (email, desde7d)).fetchone())
        if not dup:
            rej = con.execute("SELECT criado FROM pedidos WHERE lower(email)=? AND criado>=? AND status='rejeitado' "
                              "ORDER BY criado DESC LIMIT 1", (email, desde7d)).fetchone()
            if rej:
                libera = datetime.strptime(rej["criado"][:19], "%Y-%m-%d %H:%M:%S") + timedelta(days=BLOQUEIO_EMAIL_DIAS)
                raise Bloqueado("Você já fez um pedido recentemente. Tente novamente a partir de "
                                f"{libera.strftime('%d/%m')} às {libera.strftime('%H:%M')}.")
            cur = con.execute("INSERT INTO pedidos (token,nome,email,recado,ip,criado,telefone) VALUES (?,?,?,?,?,?,?)",
                              (token, nome, email, recado, ip, now(), telefone))
            pid = cur.lastrowid
    if dup:
        log.info("pedido repetido de %s <%s> (%s) -> #%s", nome, email, ip, dup["id"])
        if telefone:  # pedido antigo sem telefone (ou corrigido): guarda o mais recente
            with db_lock, db() as con:
                con.execute("UPDATE pedidos SET telefone=? WHERE id=? AND status IN ('pendente','aprovado')",
                            (telefone, dup["id"]))
        return get_pedido(id=dup["id"])
    p = get_pedido(id=pid)
    log.info("pedido #%s de %s (%s)", pid, nome, ip)
    tg_send_pedido(p)
    return p


def library_ids_wizarr():
    """IDs das bibliotecas permitidas, na numeração do Wizarr."""
    try:
        libs = wizarr("GET", "/libraries")
        libs = libs.get("libraries", libs) if isinstance(libs, dict) else libs
        return [l["id"] for l in libs if l.get("name") in LIBRARIES]
    except Exception as e:  # noqa: BLE001
        log.warning("bibliotecas do Wizarr: %s", e)
        return []


def aprovar(pid):
    p = get_pedido(id=pid)
    if not p or p["status"] != "pendente":
        return p
    body = {
        "server_ids": [WIZARR_SERVER_ID],
        "expires_in_days": INVITE_DAYS,
        "unlimited": False,
        "duration": "unlimited",
        "allow_downloads": True,
        "allow_live_tv": False,
        "allow_mobile_uploads": False,
    }
    libs = library_ids_wizarr()
    if libs:
        body["library_ids"] = libs
    r = wizarr("POST", "/invitations", body)
    inv = r["invitation"]
    url = inv["url"] if inv["url"].startswith("http") else PUBLIC_URL + inv["url"]
    with db_lock, db() as con:
        con.execute("UPDATE pedidos SET status='aprovado', decidido=?, convite_codigo=?, convite_url=?, convite_expira=? WHERE id=?",
                    (now(), inv["code"], url, inv.get("expires") or "", pid))
    log.info("pedido #%s aprovado, convite %s", pid, inv["code"])
    set_email_status(pid, mail_convite(get_pedido(id=pid)))
    tg_update(pid)
    return get_pedido(id=pid)


def rejeitar(pid):
    p = get_pedido(id=pid)
    if not p or p["status"] != "pendente":
        return p
    with db_lock, db() as con:
        con.execute("UPDATE pedidos SET status='rejeitado', decidido=? WHERE id=?", (now(), pid))
    log.info("pedido #%s rejeitado", pid)
    if MAIL_ON_REJECT:
        set_email_status(pid, mail_rejeicao(get_pedido(id=pid)))
    tg_update(pid)
    return get_pedido(id=pid)


# ----------------------------------------------------------------------------- tarefas de fundo
_lib_cache = {}


def jf_library_ids():
    if not _lib_cache.get("ids"):
        folders = jf("GET", "/Library/MediaFolders")["Items"]
        _lib_cache["ids"] = [f["Id"] for f in folders if f["Name"] in LIBRARIES]
    return _lib_cache["ids"]


def aplicar_politica(user):
    pol = dict(user["Policy"])
    if pol.get("IsAdministrator"):
        return False
    pol.update({
        "EnableAllFolders": False,
        "EnabledFolders": jf_library_ids(),
        "EnableContentDownloading": True,
        "MaxActiveSessions": MAX_SESSIONS,
        "EnableContentDeletion": False,
        "EnableContentDeletionFromFolders": [],
        "EnableLiveTvAccess": False,
        "EnableLiveTvManagement": False,
        "EnablePublicSharing": False,
        "EnableRemoteControlOfOtherUsers": False,
        "EnableSharedDeviceControl": False,
        "EnableCollectionManagement": False,
        "EnableSubtitleManagement": False,
        "EnableLyricManagement": False,
        "IsHidden": True,
    })
    jf("POST", f"/Users/{user['Id']}/Policy", pol)
    return True


def _dt(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


def sincronizar_cadastros():
    """Conclui pedidos aprovados cujo convite já foi usado e aplica a política SÓ nessa conta.

    O Wizarr importa todos os usuários do Jellyfin, então nunca se age sobre "usuários do Wizarr" em geral:
    a conta nova é a que apareceu no Wizarr depois do baseline e foi criada perto do used_at do convite.
    """
    wu = wizarr("GET", "/users")
    wu = wu.get("users", wu) if isinstance(wu, dict) else wu
    nomes = {(u.get("username") or ""): u for u in wu or [] if u.get("username")}
    with db() as con:
        row = con.execute("SELECT v FROM kv WHERE k='baseline'").fetchone()
        if row is None:  # primeira execução: todos os usuários atuais ficam fora para sempre
            with db_lock:
                con.execute("INSERT INTO kv VALUES ('baseline', ?)", (json.dumps(sorted(nomes)),))
            log.info("baseline de usuários do Wizarr: %s", sorted(nomes))
            return
        baseline = set(json.loads(row[0]))
        usados = {r[0] for r in con.execute("SELECT jf_usuario FROM pedidos WHERE jf_usuario IS NOT NULL")}
        aprovados = con.execute("SELECT * FROM pedidos WHERE status='aprovado'").fetchall()
    if not aprovados:
        return
    invs = wizarr("GET", "/invitations")
    invs = {i["code"]: i for i in (invs.get("invitations", invs) if isinstance(invs, dict) else invs)}
    candidatos = [u for n, u in nomes.items()
                  if n not in baseline and n not in usados and n.lower() not in PRIORITY]
    jf_users = None
    for p in aprovados:
        inv = invs.get(p["convite_codigo"])
        if not inv or inv.get("status") != "used" or not inv.get("used_at"):
            continue
        usado = _dt(inv["used_at"])
        perto = sorted(((abs((_dt(u.get("created_at")) - usado).total_seconds()), u) for u in candidatos
                        if _dt(u.get("created_at")) and usado), key=lambda t: t[0])
        if not perto or perto[0][0] > 900:
            log.warning("pedido #%s: convite usado mas conta nova não encontrada", p["id"])
            continue
        nome = perto[0][1]["username"]
        candidatos = [u for u in candidatos if u["username"] != nome]
        jf_users = jf_users or {u["Name"].lower(): u for u in jf("GET", "/Users")}
        u = jf_users.get(nome.lower())
        if u and aplicar_politica(u):
            log.info("pedido #%s: política aplicada a %s", p["id"], nome)
            with db_lock, db() as con:
                con.execute("INSERT OR REPLACE INTO ajustados VALUES (?,?,?)", (u["Id"], u["Name"], now()))
        with db_lock, db() as con:
            con.execute("UPDATE pedidos SET status='concluido', jf_usuario=? WHERE id=?", (nome, p["id"]))
        tg_update(p["id"])
        if WA_NOTIFY_URL and p["telefone"]:  # midia-notify cadastra o telefone e manda o link da Comunidade no privado
            wa_post({"evento": "conta", "id": p["id"], "usuario": nome, "nome": p["nome"], "telefone": p["telefone"]})
        if not WA_NOTIFY_URL:  # no WhatsApp o tg_update já avisou a conta criada
            tg("sendMessage", {"chat_id": TG_ADMIN, "parse_mode": "HTML",
                               "text": f"🎉 <b>{html.escape(p['nome'])}</b> criou a conta <b>{html.escape(nome)}</b> no Jellyfin."})


def guardiao_prioridade():
    """Se ianharbs/Anapgo estão assistindo, limita as transcodificações simultâneas dos convidados."""
    if not PRIORITY:
        return
    sessions = jf("GET", "/Sessions?ActiveWithinSeconds=120")
    playing = [s for s in sessions if s.get("NowPlayingItem")]
    if not any((s.get("UserName") or "").lower() in PRIORITY for s in playing):
        return
    guests = [s for s in playing
              if (s.get("UserName") or "").lower() not in PRIORITY
              and s.get("TranscodingInfo") and not s["TranscodingInfo"].get("IsVideoDirect", True)]
    if len(guests) <= MAX_GUEST_TRANSCODES:
        return
    guests.sort(key=lambda s: s.get("LastPlaybackCheckIn") or s.get("LastActivityDate") or "", reverse=True)
    for s in guests[: len(guests) - MAX_GUEST_TRANSCODES]:
        log.info("prioridade: parando transcodificação de %s (%s)", s.get("UserName"), s["NowPlayingItem"].get("Name"))
        try:
            jf("POST", f"/Sessions/{s['Id']}/Message", {
                "Header": "Servidor ocupado",
                "Text": "A reprodução foi pausada para liberar o servidor. Tente de novo em alguns minutos ou escolha uma qualidade menor.",
                "TimeoutMs": 15000})
            time.sleep(2)
            jf("POST", f"/Sessions/{s['Id']}/Playing/Stop")
        except Exception as e:  # noqa: BLE001
            log.warning("parar sessão: %s", e)


# ----------------------------------------------------------------------------- vitrine (números e capas)
VITRINE = {"stats": [], "capas": [], "quando": 0}


def atualizar_vitrine():
    """Quantos filmes/séries/episódios cada biblioteca tem e as capas mais recentes (para o mural da página).

    As capas são servidas pelo próprio Jellyfin em /Items/<id>/Images/Primary, que é público e já passa pelo Caddy."""
    folders = {f["Name"]: f for f in jf("GET", "/Library/MediaFolders")["Items"]}

    def conta(fid, tipo):
        return jf("GET", f"/Items?ParentId={fid}&Recursive=true&IncludeItemTypes={tipo}&Limit=0")["TotalRecordCount"]

    stats, capas = [], []
    for nome in LIBRARIES:
        f = folders.get(nome)
        if not f or f.get("CollectionType") not in ("movies", "tvshows"):
            continue
        if f["CollectionType"] == "movies":
            tipo, s = "Movie", {"nome": nome, "kind": "movies", "itens": conta(f["Id"], "Movie")}
        else:
            tipo, s = "Series", {"nome": nome, "kind": "tvshows", "itens": conta(f["Id"], "Series"),
                                 "eps": conta(f["Id"], "Episode")}
        stats.append(s)
        r = jf("GET", f"/Items?ParentId={f['Id']}&Recursive=true&IncludeItemTypes={tipo}&SortBy=DateCreated"
                      "&SortOrder=Descending&Limit=14&ImageTypes=Primary&EnableImageTypes=Primary")
        capas.append([(i["Id"], (i.get("ImageTags") or {}).get("Primary", ""), i.get("Name", ""))
                      for i in r.get("Items", []) if (i.get("ImageTags") or {}).get("Primary")])
    mistura = []  # intercala as bibliotecas para o mural não ficar em blocos
    for i in range(max((len(c) for c in capas), default=0)):
        mistura += [c[i] for c in capas if i < len(c)]
    VITRINE.update(stats=stats, capas=mistura, quando=time.time())
    log.info("vitrine: %s, %d capas", ", ".join(f"{s['nome']}={s['itens']}" for s in stats), len(mistura))


def loop_fundo():
    tick = 0
    while True:
        try:
            if JF_KEY and tick % 240 == 0:  # 1 h
                atualizar_vitrine()
        except Exception as e:  # noqa: BLE001
            log.warning("vitrine: %s", e)
        try:
            if JF_KEY:
                guardiao_prioridade()
            if tick % 4 == 0 and WIZARR_KEY and JF_KEY:
                sincronizar_cadastros()
        except Exception as e:  # noqa: BLE001
            log.warning("tarefa de fundo: %s", e)
        tick += 1
        time.sleep(15)


# ----------------------------------------------------------------------------- páginas
# Cores do ícone do iHome (imagens/icones): fundo azul-marinho e ciano.
CSS = """
:root{--bg:#070c17;--bg2:#0d1526;--panel:#111b2e;--panel2:#16223a;--line:#22304a;--text:#eaf1fa;--muted:#93a3ba;
--accent:#38d9f5;--accent2:#7fe9ff;--ink:#04121c;--ok:#3fb950;--bad:#f85149;--filmes:#ff6b6b;--series:#a78bfa;--desenhos:#fbbf24}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--accent2)}
.top{position:relative;overflow:hidden;padding:0 0 8px}
.wall{position:absolute;inset:-40px 0 0 0;display:flex;flex-direction:column;gap:12px;transform:rotate(-4deg) scale(1.08);opacity:.5}
.row{display:flex;gap:12px;width:max-content;animation:desliza 140s linear infinite}
.row.r2{animation-direction:reverse;animation-duration:170s;margin-left:-80px}
.row img{width:112px;height:168px;object-fit:cover;border-radius:10px;background:var(--panel2);flex:none}
@keyframes desliza{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@media (prefers-reduced-motion:reduce){.row{animation:none}}
.top::after{content:"";position:absolute;inset:0;background:radial-gradient(120% 90% at 50% 0%,rgba(7,12,23,.35),rgba(7,12,23,.92) 62%,var(--bg) 100%)}
.top.sem-capas{background:radial-gradient(90% 120% at 50% 0%,#1e3150,#121d33 55%,var(--bg))}
.top.sem-capas::after{display:none}
.brand{position:relative;z-index:1;max-width:1040px;margin:0 auto;padding:64px 20px 28px;display:flex;flex-direction:column;align-items:center;text-align:center}
.brand img{width:96px;height:96px;border-radius:24px;box-shadow:0 18px 50px rgba(56,217,245,.25),0 6px 18px rgba(0,0,0,.5)}
.brand .nome{font-size:44px;font-weight:800;letter-spacing:-.02em;margin:16px 0 2px;line-height:1.1}
.brand .nome span{color:var(--accent)}
.brand .tag{color:#c4d1e3;font-size:18px;max-width:560px;margin:0}
.top.compacto .brand{padding:40px 20px 18px}.top.compacto .brand img{width:64px;height:64px;border-radius:16px}
.top.compacto .brand .nome{font-size:30px;margin-top:10px}
.wrap{position:relative;max-width:1040px;margin:0 auto;padding:0 20px 48px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:6px 0 34px}
.stat{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:16px;padding:18px 20px;position:relative;overflow:hidden}
.stat::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--c,var(--accent))}
.stat .rot{font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--c,var(--accent))}
.stat .num{font-size:38px;font-weight:800;letter-spacing:-.02em;line-height:1.15;font-variant-numeric:tabular-nums}
.stat .num small{font-size:16px;font-weight:600;color:var(--muted);letter-spacing:0;margin-left:6px}
.stat .eps{color:#c4d1e3;font-size:15px;font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:1fr 1.05fr;gap:28px;align-items:start}
@media (max-width:520px){.stats{grid-template-columns:1fr}.stat .num{font-size:32px}.card{padding:20px}.wrap{padding:0 14px 40px}}
@media (max-width:820px){.grid{grid-template-columns:1fr}.grid>.card{order:-1}.brand .nome{font-size:36px}.brand{padding-top:48px}}
h2{font-size:15px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 14px}
.passos{list-style:none;margin:0 0 28px;padding:0;counter-reset:p}
.passos li{counter-increment:p;position:relative;padding:0 0 18px 50px}
.passos li::before{content:counter(p);position:absolute;left:0;top:0;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;
font-weight:800;color:var(--ink);background:var(--accent)}
.passos li:not(:last-child)::after{content:"";position:absolute;left:16px;top:38px;bottom:2px;width:2px;background:var(--line)}
.passos b{display:block}.passos span{color:var(--muted);font-size:15px}
.extras{display:flex;flex-wrap:wrap;gap:8px;margin:0;padding:0;list-style:none}
.extras li{background:var(--panel);border:1px solid var(--line);border-radius:99px;padding:6px 13px 6px 11px;font-size:14px;color:#c9d5e6;display:flex;align-items:center;gap:7px}
.extras li::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px var(--accent)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:26px;box-shadow:0 20px 60px rgba(0,0,0,.35)}
.card.so{max-width:560px;margin:0 auto}
h1{font-size:26px;line-height:1.2;margin:0 0 6px;letter-spacing:-.01em}
p{color:var(--muted);margin:0 0 18px}
label{display:block;font-size:14px;font-weight:600;color:#d3dcea;margin:16px 0 6px}
label small{font-weight:400;color:var(--muted)}
input,textarea{width:100%;padding:12px 13px;border-radius:10px;border:1px solid var(--line);background:var(--bg2);color:var(--text);font:inherit}
input::placeholder,textarea::placeholder{color:#5f6f88}
textarea{min-height:92px;resize:vertical}
input:focus,textarea:focus{outline:2px solid var(--accent);border-color:transparent}
.wa{position:relative}.wa input{padding-left:44px}
.wa svg{position:absolute;left:13px;top:50%;transform:translateY(-50%);width:20px;height:20px}
.btn{display:inline-block;margin-top:22px;border:0;border-radius:10px;padding:13px 22px;font:inherit;font-weight:700;background:var(--accent);color:var(--ink);cursor:pointer;text-decoration:none}
form .btn{width:100%}
.btn:hover{background:var(--accent2)}.btn.bad{background:var(--bad);color:#fff}.btn.ghost{background:#262b38;color:var(--text)}
.hp{position:absolute;left:-9999px}
.badge{display:inline-block;padding:4px 11px;border-radius:99px;font-size:13px;font-weight:700;margin-bottom:12px}
.pend{background:#3a3220;color:#f0c674}.ok{background:#16351f;color:#7ee2a0}.no{background:#3d1d1f;color:#ff9b95}
.small{font-size:13.5px;color:var(--muted);margin-top:18px}
.err{color:#ffb4ae;background:#3d1d1f;border-radius:10px;padding:10px 12px;margin-top:16px;font-size:15px}
.aviso{display:flex;gap:10px;align-items:flex-start;background:#0f2a22;border:1px solid #1d4a3b;border-radius:12px;padding:12px 14px;color:#bfeccf;font-size:15px;margin:16px 0 0}
dl{margin:0}dt{color:var(--muted);font-size:13px;margin-top:10px}dd{margin:2px 0 0}
footer{text-align:center;color:#5f6f88;font-size:13px;padding:0 20px 32px}
"""

WA_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#25d366" d="M12 2a10 10 0 0 0-8.6 15.1L2 22l5-1.3A10 10 0 1 0 12 2Z"/>'
           '<path fill="#fff" d="M16.9 14.4c-.3-.1-1.6-.8-1.8-.9-.3-.1-.4-.1-.6.1l-.8 1c-.2.2-.3.2-.6.1a7 7 0 0 1-3.5-3c-.3-.4.3-.4.8-1.4.1-.2 0-.3 0-.5l-.8-2c-.2-.5-.4-.4-.6-.4h-.5a1 1 0 0 0-.7.3 3 3 0 0 0-.9 2.2 5.2 5.2 0 0 0 1.1 2.7 11.8 11.8 0 0 0 4.5 4c1.7.7 2.3.8 3.2.6.5-.1 1.6-.6 1.8-1.3.2-.6.2-1.1.2-1.3l-.8-.2Z"/></svg>')

COR = {"Filmes": "var(--filmes)", "Séries": "var(--series)", "Desenhos": "var(--desenhos)"}


def n_br(n):
    return f"{n:,}".replace(",", ".")


def topo(compacto=False):
    capas = VITRINE["capas"]
    mural = ""
    if len(capas) >= 8:
        metade = len(capas) // 2

        def fila(cls, itens):
            imgs = "".join(f'<img src="/Items/{i}/Images/Primary?fillHeight=252&amp;fillWidth=168&amp;quality=80&amp;tag={html.escape(t)}" '
                           f'alt="" loading="lazy" decoding="async">' for i, t, _ in itens)
            return f'<div class="row {cls}">{imgs}{imgs}</div>'  # duplicado = rolagem sem emenda
        mural = f'<div class="wall" aria-hidden="true">{fila("r1", capas[:metade])}{fila("r2", capas[metade:])}</div>'
    cls = " ".join(c for c in ("compacto" if compacto else "", "" if mural else "sem-capas") if c)
    tag = "" if compacto else '<p class="tag">Filmes, séries e desenhos para assistir na TV, no celular ou no computador.</p>'
    return f"""<header class="top {cls}">{mural}<div class="brand">
<img src="/cadastro/icone.svg" alt="" width="96" height="96"><div class="nome">i<span>Home</span></div>{tag}</div></header>"""


def stats_html():
    cards = []
    for s in VITRINE["stats"]:
        cor = COR.get(s["nome"], "var(--accent)")
        if s["kind"] == "movies":
            corpo = f'<div class="num">{n_br(s["itens"])}<small>filmes</small></div>'
        else:
            corpo = (f'<div class="num">{n_br(s["itens"])}<small>{"séries" if s["nome"] != "Desenhos" else "desenhos"}</small></div>'
                     f'<div class="eps">{n_br(s.get("eps", 0))} episódios</div>')
        cards.append(f'<div class="stat" style="--c:{cor}"><div class="rot">{html.escape(s["nome"])}</div>{corpo}</div>')
    return f'<section class="stats" aria-label="O que tem no iHome">{"".join(cards)}</section>' if cards else ""


def page(title, body, refresh=None, compacto=True, stats=False):
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer"><meta name="theme-color" content="#070c17">{meta}
<link rel="icon" href="/cadastro/icone.svg" type="image/svg+xml"><title>{html.escape(title)} · iHome</title><style>{CSS}</style></head>
<body>{topo(compacto)}<main class="wrap">{stats_html() if stats else ""}{body}</main>
<footer>iHome · {html.escape(PUBLIC_URL.replace("https://", ""))}</footer></body></html>"""


def card(conteudo):
    return f'<div class="card so">{conteudo}</div>'


def form_page(err="", nome="", email="", recado="", telefone=""):
    if not OPEN:
        return page("Cadastro fechado", card("<h1>Cadastro fechado</h1><p>No momento não estamos aceitando novos pedidos.</p>"),
                    compacto=False, stats=True)
    e = f'<div class="err" role="alert">{html.escape(err)}</div>' if err else ""
    return page("Pedir acesso", f"""<div class="grid">
<section>
<h2>Como funciona</h2>
<ol class="passos">
<li><b>Peça acesso</b><span>Preencha o formulário. Cada pedido é aprovado por uma pessoa.</span></li>
<li><b>Receba o convite por e-mail</b><span>Você mesmo escolhe o usuário e a senha.</span></li>
<li><b>Entre no grupo do WhatsApp</b><span>Quando a conta for criada, mandamos o convite da comunidade iHome para o seu número:
avisos de filmes e séries novos e pedidos direto pelo WhatsApp.</span></li>
</ol>
<h2>O que vem junto</h2>
<ul class="extras"><li>App na TV e no celular</li><li>Até {MAX_SESSIONS} telas ao mesmo tempo</li>
<li>Download para ver offline</li><li>Pedidos de filmes e séries</li><li>Legendas em português</li></ul>
</section>
<div class="card">
<h1>Pedir acesso</h1>
<p>Leva menos de um minuto.</p>
<form method="post" action="/cadastro" autocomplete="on">
<label for="nome">Seu nome</label><input id="nome" name="nome" required maxlength="80" autocomplete="name" value="{html.escape(nome)}">
<label for="email">E-mail <small>— o convite chega por aqui</small></label>
<input id="email" name="email" type="email" required maxlength="120" autocomplete="email" value="{html.escape(email)}">
<label for="telefone">WhatsApp com DDD <small>— para o convite do grupo</small></label>
<div class="wa">{WA_ICON}<input id="telefone" name="telefone" type="tel" inputmode="tel" required maxlength="25" autocomplete="tel"
placeholder="(47) 99999-9999" value="{html.escape(telefone)}"></div>
<label for="recado">Quem é você / como conhece a gente?</label>
<textarea id="recado" name="recado" maxlength="500" required>{html.escape(recado)}</textarea>
<input class="hp" name="site" tabindex="-1" autocomplete="off" aria-hidden="true">
{e}<button class="btn" type="submit">Enviar pedido</button>
</form>
<p class="small">Seu telefone só é usado para o convite e os avisos do grupo do iHome.</p>
</div></div>""", compacto=False, stats=True)


def status_page(p):
    link = f"{PUBLIC_URL}/cadastro/s/{p['token']}"
    guarde = f'<p class="small">Guarde este endereço para acompanhar:<br><a href="{link}" style="word-break:break-all">{link}</a></p>'
    tel = p["telefone"] if "telefone" in p.keys() else None
    wa = (f'<div class="aviso">{WA_ICON.replace("<svg ", "<svg width=22 height=22 style=flex:none ")}<span>Quando sua conta for criada, '
          f'o convite do grupo do iHome no WhatsApp vai para <b>{html.escape(fmt_tel(tel))}</b>.</span></div>') if tel else ""
    if p["status"] == "pendente":
        return page("Pedido enviado", card(f"""<span class="badge pend">Aguardando aprovação</span>
<h1>Pedido enviado, {html.escape(p['nome'].split()[0])}!</h1>
<p>Assim que for aprovado, você recebe o convite por e-mail (confira o spam) e esta página também mostra o botão para criar sua conta. Ela se atualiza sozinha.</p>{wa}{guarde}"""), refresh=30)
    if p["status"] == "rejeitado":
        return page("Sem vagas no momento", card('<span class="badge no">Sem vagas</span><h1>Sem vagas no momento</h1><p>Obrigado pelo interesse! No momento não temos vaga disponível. Tente fazer o cadastro novamente na semana que vem.</p>'))
    if p["status"] == "aprovado":
        return page("Pedido aprovado", card(f"""<span class="badge ok">Aprovado</span>
<h1>Pode criar sua conta!</h1>
<p>Use o botão abaixo para escolher usuário e senha (o mesmo link foi para o seu e-mail). O convite vale por {INVITE_DAYS} dias e só pode ser usado uma vez.</p>
<a class="btn" href="{html.escape(p['convite_url'])}">Criar minha conta</a>{wa}{guarde}"""))
    wa_ok = (f'<div class="aviso">{WA_ICON.replace("<svg ", "<svg width=22 height=22 style=flex:none ")}<span>O convite do grupo do iHome '
             f'no WhatsApp foi enviado para <b>{html.escape(fmt_tel(tel))}</b>.</span></div>') if tel else ""
    return page("Conta criada", card(f"""<span class="badge ok">Conta criada</span>
<h1>Tudo pronto!</h1>
<p>Entre em <a href="{PUBLIC_URL}/">{PUBLIC_URL.replace('https://', '')}</a> com o usuário
<b>{html.escape(p['jf_usuario'] or '')}</b>, ou use o app Jellyfin (celular/TV) com o servidor <b>{PUBLIC_URL}</b>.</p>{wa_ok}"""))


def admin_page(p, acao, s):
    if p["status"] != "pendente":
        return page("Já decidido", card(f"<h1>Pedido #{p['id']} já foi {p['status']}</h1><p>Nada a fazer.</p>"))
    verbo = "Aprovar" if acao == "aprovar" else "Rejeitar"
    return page(f"{verbo} pedido", card(f"""
<h1>{verbo} o pedido #{p['id']}?</h1>
<dl><dt>Nome</dt><dd>{html.escape(p['nome'])}</dd>
<dt>E-mail</dt><dd>{html.escape(p['email'] or '—')}</dd>
<dt>WhatsApp</dt><dd>{html.escape(fmt_tel(p['telefone']) or '—')}</dd>
<dt>Recado</dt><dd>{html.escape(p['recado'] or '—')}</dd>
<dt>Enviado</dt><dd>{fmt(p['criado'])} · IP {html.escape(p['ip'] or '?')}</dd></dl>
<form method="post" action="/cadastro/admin/{p['id']}?a={acao}&s={s}">
<button class="btn {'bad' if acao == 'rejeitar' else ''}" type="submit">{verbo}</button></form>"""))


STATIC = {}
for _nome, _tipo in (("icone.svg", "image/svg+xml"),):
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", _nome), "rb") as _f:
            STATIC[f"/cadastro/{_nome}"] = (_f.read(), _tipo)
    except OSError:
        pass


class H(BaseHTTPRequestHandler):
    server_version = "cadastro"
    sys_version = ""

    def log_message(self, fmt_, *args):
        log.debug(fmt_, *args)

    def ip(self):
        return (self.headers.get("X-Real-IP") or self.client_address[0])[:64]

    def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", (extra or {}).pop("Cache-Control", "no-store"))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def redirect(self, url):
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def form(self):
        n = min(int(self.headers.get("Content-Length") or 0), 8192)
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}

    def route(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        return u.path.rstrip("/") or "/", q

    def do_GET(self):
        path, q = self.route()
        if path == "/cadastro/health":
            return self.send(200, "ok", "text/plain")
        if path == "/cadastro":
            return self.send(200, form_page())
        if path in STATIC:
            corpo, tipo = STATIC[path]
            return self.send(200, corpo, tipo, {"Cache-Control": "public, max-age=86400"})
        m = re.fullmatch(r"/cadastro/s/([A-Za-z0-9_-]{20,64})", path)
        if m:
            p = get_pedido(token=m.group(1))
            return self.send(200, status_page(p)) if p else self.send(404, page("Não encontrado", card("<h1>Pedido não encontrado</h1>")))
        m = re.fullmatch(r"/cadastro/admin/(\d+)", path)
        if m:
            pid, acao, s = int(m.group(1)), q.get("a", ""), q.get("s", "")
            p = get_pedido(id=pid)
            if not p or acao not in ("aprovar", "rejeitar") or not hmac.compare_digest(s, sign(pid, acao)):
                return self.send(404, page("Não encontrado", card("<h1>Link inválido</h1>")))
            return self.send(200, admin_page(p, acao, s))
        self.send(404, page("Não encontrado", card("<h1>Página não encontrada</h1>")))

    def do_POST(self):
        path, q = self.route()
        if path == "/cadastro":
            if not OPEN:
                return self.send(403, form_page())
            f = self.form()
            nome, email, recado = (f.get("nome", "").strip()[:80], f.get("email", "").strip()[:120], f.get("recado", "").strip()[:500])
            tel_txt = f.get("telefone", "").strip()[:25]
            tel = normalizar_tel(tel_txt)
            volta = (nome, email, recado, tel_txt)
            if f.get("site"):  # honeypot
                return self.send(200, page("Pedido enviado", card("<h1>Pedido enviado</h1><p>Obrigado!</p>")))
            if len(nome) < 2 or len(recado) < 3 or not email or not tel_txt:
                return self.send(400, form_page("Preencha nome, e-mail, WhatsApp e recado.", *volta))
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                return self.send(400, form_page("E-mail inválido.", *volta))
            if not tel:
                return self.send(400, form_page("WhatsApp inválido: use o DDD + número, por exemplo (47) 99999-9999.", *volta))
            if pedidos_recentes(self.ip()) >= 3:
                return self.send(429, form_page("Muitos pedidos deste endereço. Tente amanhã.", *volta))
            try:
                p = criar_pedido(nome, email, recado, self.ip(), tel)
            except Bloqueado as e:
                return self.send(429, form_page(str(e), *volta))
            return self.redirect(f"/cadastro/s/{p['token']}")
        m = re.fullmatch(r"/cadastro/admin/(\d+)", path)
        if m:
            pid, acao, s = int(m.group(1)), q.get("a", ""), q.get("s", "")
            if acao not in ("aprovar", "rejeitar") or not hmac.compare_digest(s, sign(pid, acao)):
                return self.send(404, page("Não encontrado", card("<h1>Link inválido</h1>")))
            try:
                p = aprovar(pid) if acao == "aprovar" else rejeitar(pid)
            except Exception as e:  # noqa: BLE001
                log.exception("decisão #%s", pid)
                return self.send(500, page("Erro", card(f"<h1>Erro</h1><p>{html.escape(str(e))}</p>")))
            if not p:
                return self.send(404, page("Não encontrado", card("<h1>Pedido não encontrado</h1>")))
            msg = "Aprovado ✅ — o convite já aparece na página do pedido." if p["status"] == "aprovado" else f"Pedido {p['status']}."
            return self.send(200, page("Feito", card(f"<h1>{msg}</h1><p>Pode fechar esta página.</p>")))
        self.send(404, page("Não encontrado", card("<h1>Página não encontrada</h1>")))


def main():
    if not SECRET:
        raise SystemExit("APP_SECRET não definido")
    init_db()
    threading.Thread(target=loop_fundo, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", 8080), H)
    log.info("cadastro ouvindo em :8080 (%s)", PUBLIC_URL)
    srv.serve_forever()


if __name__ == "__main__":
    main()
