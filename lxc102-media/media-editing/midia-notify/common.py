"""Utilidades compartilhadas (só biblioteca padrão): segredos, HTTP, Telegram."""
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid


def load_secrets(path):
    """Segredos ficam fora do git, em /state/secrets.env (lido aqui, não por env_file do compose)."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if not os.environ.get(k.strip()):  # variável de ambiente tem prioridade
                        os.environ[k.strip()] = v.strip()
    except FileNotFoundError:
        pass


load_secrets(os.environ.get("SECRETS_FILE", "/state/secrets.env"))
E = os.environ.get
log = logging.getLogger("midia-notify")

TG_TOKEN = E("TELEGRAM_TOKEN", "")
TG_GROUP = E("TELEGRAM_CHAT_ID", "")  # grupo antigo: destino padrão de séries/filmes sem central própria
DB_PATH = E("DB_PATH", "/state/notify.db")
CHANNELS = {  # canal separado por tipo (opcional; o grupo com tópicos abaixo tem prioridade)
    "series": E("TELEGRAM_CHAT_SERIES", "") or TG_GROUP,
    "filmes": E("TELEGRAM_CHAT_FILMES", "") or TG_GROUP,
    "livros": E("TELEGRAM_CHAT_LIVROS", ""),
    "musica": E("TELEGRAM_CHAT_MUSICA", ""),
    "status": E("TELEGRAM_CHAT_STATUS", ""),
}
# Grupo com Tópicos: um tópico por central. O bot detecta o grupo sozinho quando vira admin (kv forum_id)
# e cria os tópicos que faltarem (kv topic_<tipo>); TELEGRAM_FORUM_ID / TELEGRAM_TOPIC_<TIPO> forçam à mão.
TOPICS = {  # tipo -> (nome do tópico, cor do ícone — só as 6 cores aceitas pelo Telegram)
    "series": ("📺 Séries", 7322096), "filmes": ("🎬 Filmes", 16766590), "livros": ("📚 Livros", 13338331),
    "musica": ("🎵 Música", 9367192), "status": ("🤖 Status", 16749490),
}


def kv_get(k, default=None):
    try:
        with sqlite3.connect(DB_PATH) as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default
    except sqlite3.OperationalError:
        return default


def kv_set(k, v):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def forum_id():
    return E("TELEGRAM_FORUM_ID", "") or kv_get("forum_id", "")


def topic_id(kind, create=True):
    tid = E(f"TELEGRAM_TOPIC_{kind.upper()}", "") or kv_get(f"topic_{kind}", "")
    if tid or not create or not forum_id():
        return tid
    name, color = TOPICS[kind]
    try:
        tid = str(tg("createForumTopic", {"chat_id": forum_id(), "name": name, "icon_color": color})
                  ["result"]["message_thread_id"])
        kv_set(f"topic_{kind}", tid)
        log.info("tópico '%s' criado (thread %s)", name, tid)
    except Exception as e:
        log.error("não consegui criar o tópico '%s' (o bot precisa ser admin com 'Gerenciar tópicos'): %s", name, e)
    return tid


def dest(kind):
    """(chat, thread) da central `kind`: tópico do grupo-fórum, senão canal próprio, senão nada."""
    if forum_id():
        tid = topic_id(kind)
        if tid:
            return forum_id(), tid
    return CHANNELS.get(kind), None


def http(method, url, headers=None, body=None, timeout=20, raw=False):
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        content = r.read()
    if raw:
        return content
    return json.loads(content) if content.strip() else None


DN_PUBLIC = E("DROPPEDNEEDLE_PUBLIC_URL", "https://dn.ihome.dev.br").rstrip("/")


def dn_album_link(artist, title):
    """Página do álbum no DroppedNeedle (/album/<release-group MusicBrainz>), onde se pede o download.
    Álbum que o MusicBrainz ainda não tem (lançamento muito recente) cai na busca de álbuns do DN."""
    t, a = (str(x).replace('"', " ").replace("\\", " ") for x in (title, artist))
    q = f'releasegroup:"{t}" AND artist:"{a}"'
    url = "https://musicbrainz.org/ws/2/release-group/?fmt=json&limit=5&query=" + urllib.parse.quote(q)
    try:
        for tries in range(3):  # MusicBrainz: 1 pedido/s, 503 quando passa disso
            try:
                r = http("GET", url, headers={"User-Agent": "midia-notify/1.0 (homelab)"}, timeout=15)
                break
            except urllib.error.HTTPError as e:
                if e.code != 503 or tries == 2:
                    raise
                time.sleep(2)
        for g in r.get("release-groups", []):
            names = " ".join(norm(c.get("name")) for c in g.get("artist-credit", []) if isinstance(c, dict))
            if g.get("score", 0) >= 90 and norm(g.get("title")) == norm(title) and norm(artist) in names:
                return f"{DN_PUBLIC}/album/{g['id']}"
    except Exception as e:
        log.warning("MusicBrainz %s — %s: %s", artist, title, e)
    return f"{DN_PUBLIC}/search/albums?q=" + urllib.parse.quote(f"{artist} {title}")


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def norm(s):
    """minúsculas, sem acento e sem pontuação (para comparar títulos)."""
    s = re.sub(r"[\u2010-\u2015\u2212]", "-", str(s or ""))  # hífens tipográficos (Anti‐Flag) viram espaço
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def tg(method, body=None, timeout=30):
    return http("POST", f"https://api.telegram.org/bot{TG_TOKEN}/{method}", body=body or {}, timeout=timeout)


def tg_photo_upload(chat, photo_bytes, caption, extra=None):
    """sendPhoto com o arquivo no corpo (multipart) — para capas que o Telegram não consegue baixar sozinho."""
    boundary = uuid.uuid4().hex
    fields = {"chat_id": str(chat), "caption": caption, "parse_mode": "HTML", **(extra or {})}
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="capa.jpg"\r\n'
                 f"Content-Type: image/jpeg\r\n\r\n".encode() + photo_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data=b"".join(parts),
                                 method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def post(kind, text, photo=None, photo_bytes=None):
    """Manda uma mensagem para a central `kind`. Sem destino configurado, só registra no log."""
    chat, thread = dest(kind)
    if not (TG_TOKEN and chat):
        log.info("[sem central '%s' configurada] mensagem:\n%s", kind, text)
        return True
    msg = {"chat_id": chat, "parse_mode": "HTML"}
    if thread:
        msg["message_thread_id"] = int(thread)
    try:
        if photo_bytes:
            try:
                tg_photo_upload(chat, photo_bytes, text, {"message_thread_id": thread} if thread else None)
                return True
            except Exception as e:
                log.warning("upload da capa falhou (%s), mandando só texto", e)
        elif photo:
            try:
                tg("sendPhoto", {**msg, "photo": photo, "caption": text})
                return True
            except Exception as e:
                log.warning("sendPhoto falhou (%s), mandando só texto", e)
        tg("sendMessage", {**msg, "text": text, "link_preview_options": {"is_disabled": True}})
        return True
    except Exception as e:
        log.error("telegram (%s): %s", kind, e)
        return False
