"""Cadastro com aprovação para o Jellyfin (video.ihome.dev.br/cadastro).

Fluxo: visitante pede acesso -> DM no Telegram para o admin com Aprovar/Rejeitar (links assinados) ->
aprovado: convite de uso único no Wizarr, mostrado na página de status do visitante -> visitante cria a conta
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
WIZARR_URL = E("WIZARR_URL", "http://wizarr:5690").rstrip("/")
WIZARR_KEY = E("WIZARR_API_KEY", "")
WIZARR_SERVER_ID = int(E("WIZARR_SERVER_ID", "1") or 1)
JF_URL = E("JELLYFIN_URL", "").rstrip("/")
JF_KEY = E("JELLYFIN_API_KEY", "")
LIBRARIES = [x.strip() for x in E("LIBRARIES", "Filmes,Séries,Coleções").split(",") if x.strip()]
MAX_SESSIONS = int(E("MAX_SESSIONS", "2"))
PRIORITY = {x.strip().lower() for x in E("PRIORITY_USERS", "").split(",") if x.strip()}
MAX_GUEST_TRANSCODES = int(E("MAX_GUEST_TRANSCODES", "2"))
INVITE_DAYS = 7
DB_PATH = E("DB_PATH", "/data/cadastro.db")
OPEN = E("ACCEPT_REQUESTS", "true").lower() == "true"

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


# ----------------------------------------------------------------------------- telegram
def tg_text(p):
    linhas = [
        "🎬 <b>Pedido de acesso ao Jellyfin</b>",
        f"<b>Nome:</b> {html.escape(p['nome'])}",
    ]
    if p["email"]:
        linhas.append(f"<b>E-mail:</b> {html.escape(p['email'])}")
    if p["recado"]:
        linhas.append(f"<b>Recado:</b> {html.escape(p['recado'])}")
    linhas.append(f"<i>#{p['id']} · {fmt(p['criado'])} · IP {html.escape(p['ip'] or '?')}</i>")
    st = {
        "aprovado": "\n✅ <b>Aprovado</b> — convite enviado para a página do pedido.",
        "rejeitado": "\n❌ <b>Rejeitado</b>.",
        "concluido": f"\n🎉 <b>Conta criada</b>: {html.escape(p['jf_usuario'] or '')}",
    }.get(p["status"], "")
    return "\n".join(linhas) + st


def tg_send_pedido(p):
    base = f"{PUBLIC_URL}/cadastro/admin/{p['id']}"
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


def criar_pedido(nome, email, recado, ip):
    token = secrets.token_urlsafe(24)
    with db_lock, db() as con:
        cur = con.execute("INSERT INTO pedidos (token,nome,email,recado,ip,criado) VALUES (?,?,?,?,?,?)",
                          (token, nome, email, recado, ip, now()))
        pid = cur.lastrowid
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
    tg_update(pid)
    return get_pedido(id=pid)


def rejeitar(pid):
    p = get_pedido(id=pid)
    if not p or p["status"] != "pendente":
        return p
    with db_lock, db() as con:
        con.execute("UPDATE pedidos SET status='rejeitado', decidido=? WHERE id=?", (now(), pid))
    log.info("pedido #%s rejeitado", pid)
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


def loop_fundo():
    tick = 0
    while True:
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
CSS = """
:root{--bg:#0b0d12;--panel:#151821;--line:#2a2f3c;--text:#e9ecf2;--muted:#9aa3b2;--accent:#00a4dc;--ok:#3fb950;--bad:#f85149}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:520px;margin:0 auto;padding:48px 16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:26px}
h1{font-size:24px;margin:0 0 6px}p{color:var(--muted);margin:0 0 18px}
label{display:block;font-size:14px;color:#c9d1dc;margin:14px 0 6px}
input,textarea{width:100%;padding:11px 12px;border-radius:9px;border:1px solid var(--line);background:#0f1218;color:var(--text);font:inherit}
textarea{min-height:90px;resize:vertical}
input:focus,textarea:focus{outline:2px solid var(--accent);border-color:transparent}
.btn{display:inline-block;margin-top:20px;border:0;border-radius:9px;padding:12px 20px;font:inherit;font-weight:600;background:var(--accent);color:#fff;cursor:pointer;text-decoration:none}
.btn.bad{background:var(--bad)}.btn.ghost{background:#262b38}
.hp{position:absolute;left:-9999px}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-size:13px;font-weight:600;margin-bottom:12px}
.pend{background:#3a3220;color:#f0c674}.ok{background:#16351f;color:#7ee2a0}.no{background:#3d1d1f;color:#ff9b95}
.small{font-size:13px;color:var(--muted);margin-top:18px}.err{color:#ff9b95;margin-top:10px}
dl{margin:0}dt{color:var(--muted);font-size:13px;margin-top:10px}dd{margin:2px 0 0}
"""


def page(title, body, refresh=None):
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">{meta}<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap"><div class="card">{body}</div></div></body></html>"""


def form_page(err="", nome="", email="", recado=""):
    if not OPEN:
        return page("Cadastro fechado", "<h1>Cadastro fechado</h1><p>No momento não estamos aceitando novos pedidos.</p>")
    e = f'<div class="err">{html.escape(err)}</div>' if err else ""
    return page("Pedir acesso", f"""
<h1>Pedir acesso</h1>
<p>Preencha e aguarde a aprovação. Depois você mesmo escolhe o usuário e a senha.</p>
<form method="post" action="/cadastro" autocomplete="on">
<label for="nome">Seu nome</label><input id="nome" name="nome" required maxlength="80" value="{html.escape(nome)}">
<label for="email">E-mail (opcional)</label><input id="email" name="email" type="email" maxlength="120" value="{html.escape(email)}">
<label for="recado">Quem é você / como conhece a gente?</label><textarea id="recado" name="recado" maxlength="500" required>{html.escape(recado)}</textarea>
<input class="hp" name="site" tabindex="-1" autocomplete="off" aria-hidden="true">
{e}<button class="btn" type="submit">Enviar pedido</button>
</form>""")


def status_page(p):
    link = f"{PUBLIC_URL}/cadastro/s/{p['token']}"
    guarde = f'<p class="small">Guarde este endereço para acompanhar: <br><a href="{link}" style="color:#7cd3f7;word-break:break-all">{link}</a></p>'
    if p["status"] == "pendente":
        return page("Pedido enviado", f"""<span class="badge pend">Aguardando aprovação</span>
<h1>Pedido enviado, {html.escape(p['nome'].split()[0])}!</h1>
<p>Assim que for aprovado, esta página mostra o botão para criar sua conta. Ela se atualiza sozinha.</p>{guarde}""", refresh=30)
    if p["status"] == "rejeitado":
        return page("Pedido não aprovado", '<span class="badge no">Não aprovado</span><h1>Pedido não aprovado</h1><p>Desculpe, este pedido não foi aprovado.</p>')
    if p["status"] == "aprovado":
        return page("Pedido aprovado", f"""<span class="badge ok">Aprovado</span>
<h1>Pode criar sua conta!</h1>
<p>Use o botão abaixo para escolher usuário e senha. O convite vale por {INVITE_DAYS} dias e só pode ser usado uma vez.</p>
<a class="btn" href="{html.escape(p['convite_url'])}">Criar minha conta</a>{guarde}""")
    return page("Conta criada", f"""<span class="badge ok">Conta criada</span>
<h1>Tudo pronto!</h1>
<p>Entre em <a href="{PUBLIC_URL}/" style="color:#7cd3f7">{PUBLIC_URL.replace('https://', '')}</a> com o usuário
<b>{html.escape(p['jf_usuario'] or '')}</b>, ou use o app Jellyfin (celular/TV) com o servidor <b>{PUBLIC_URL}</b>.</p>""")


def admin_page(p, acao, s):
    if p["status"] != "pendente":
        return page("Já decidido", f"<h1>Pedido #{p['id']} já foi {p['status']}</h1><p>Nada a fazer.</p>")
    verbo = "Aprovar" if acao == "aprovar" else "Rejeitar"
    return page(f"{verbo} pedido", f"""
<h1>{verbo} o pedido #{p['id']}?</h1>
<dl><dt>Nome</dt><dd>{html.escape(p['nome'])}</dd>
<dt>E-mail</dt><dd>{html.escape(p['email'] or '—')}</dd>
<dt>Recado</dt><dd>{html.escape(p['recado'] or '—')}</dd>
<dt>Enviado</dt><dd>{fmt(p['criado'])} · IP {html.escape(p['ip'] or '?')}</dd></dl>
<form method="post" action="/cadastro/admin/{p['id']}?a={acao}&s={s}">
<button class="btn {'bad' if acao == 'rejeitar' else ''}" type="submit">{verbo}</button></form>""")


class H(BaseHTTPRequestHandler):
    server_version = "cadastro"
    sys_version = ""

    def log_message(self, fmt_, *args):
        log.debug(fmt_, *args)

    def ip(self):
        return (self.headers.get("X-Real-IP") or self.client_address[0])[:64]

    def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
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
        m = re.fullmatch(r"/cadastro/s/([A-Za-z0-9_-]{20,64})", path)
        if m:
            p = get_pedido(token=m.group(1))
            return self.send(200, status_page(p)) if p else self.send(404, page("Não encontrado", "<h1>Pedido não encontrado</h1>"))
        m = re.fullmatch(r"/cadastro/admin/(\d+)", path)
        if m:
            pid, acao, s = int(m.group(1)), q.get("a", ""), q.get("s", "")
            p = get_pedido(id=pid)
            if not p or acao not in ("aprovar", "rejeitar") or not hmac.compare_digest(s, sign(pid, acao)):
                return self.send(404, page("Não encontrado", "<h1>Link inválido</h1>"))
            return self.send(200, admin_page(p, acao, s))
        self.send(404, page("Não encontrado", "<h1>Página não encontrada</h1>"))

    def do_POST(self):
        path, q = self.route()
        if path == "/cadastro":
            if not OPEN:
                return self.send(403, form_page())
            f = self.form()
            nome, email, recado = (f.get("nome", "").strip()[:80], f.get("email", "").strip()[:120], f.get("recado", "").strip()[:500])
            if f.get("site"):  # honeypot
                return self.send(200, page("Pedido enviado", "<h1>Pedido enviado</h1><p>Obrigado!</p>"))
            if len(nome) < 2 or len(recado) < 3:
                return self.send(400, form_page("Preencha nome e recado.", nome, email, recado))
            if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                return self.send(400, form_page("E-mail inválido.", nome, email, recado))
            if pedidos_recentes(self.ip()) >= 3:
                return self.send(429, form_page("Muitos pedidos deste endereço. Tente amanhã.", nome, email, recado))
            p = criar_pedido(nome, email, recado, self.ip())
            return self.redirect(f"/cadastro/s/{p['token']}")
        m = re.fullmatch(r"/cadastro/admin/(\d+)", path)
        if m:
            pid, acao, s = int(m.group(1)), q.get("a", ""), q.get("s", "")
            if acao not in ("aprovar", "rejeitar") or not hmac.compare_digest(s, sign(pid, acao)):
                return self.send(404, page("Não encontrado", "<h1>Link inválido</h1>"))
            try:
                p = aprovar(pid) if acao == "aprovar" else rejeitar(pid)
            except Exception as e:  # noqa: BLE001
                log.exception("decisão #%s", pid)
                return self.send(500, page("Erro", f"<h1>Erro</h1><p>{html.escape(str(e))}</p>"))
            if not p:
                return self.send(404, page("Não encontrado", "<h1>Pedido não encontrado</h1>"))
            msg = "Aprovado ✅ — o convite já aparece na página do pedido." if p["status"] == "aprovado" else f"Pedido {p['status']}."
            return self.send(200, page("Feito", f"<h1>{msg}</h1><p>Pode fechar esta página.</p>"))
        self.send(404, page("Não encontrado", "<h1>Página não encontrada</h1>"))


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
