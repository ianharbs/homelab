"""midia-notify: avisa no Telegram quando um filme/episódio está DISPONÍVEL (importado + legendado).

Fluxo:
  Sonarr/Radarr (Webhook, evento On File Import) -> POST /sonarr | /radarr -> item "pendente" no SQLite
  a cada POLL_SECONDS: pergunta ao Bazarr se ainda falta legenda (missing_subtitles).
    - nada faltando, ou áudio já em português  -> pronto
    - passou de TIMEOUT_HOURS                   -> pronto "sem legenda"
  episódios prontos da mesma série são agrupados numa única mensagem (GROUP_MINUTES / QUIET_MINUTES).

Só biblioteca padrão do Python (sem pip).
"""
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
PORT = int(E("PORT", "8095"))
DB_PATH = E("DB_PATH", "/state/notify.db")
POLL = int(E("POLL_SECONDS", "300"))
TIMEOUT_H = float(E("TIMEOUT_HOURS", "12"))
GROUP_MIN = float(E("GROUP_MINUTES", "60"))  # manda o que estiver pronto no máximo X min depois do 1º pronto
QUIET_MIN = float(E("QUIET_MINUTES", "5"))  # ...ou antes, se a série não tem mais nada pendente há X min
RESCAN_MIN = float(E("RESCAN_MINUTES", "30"))  # scan-disk no Bazarr (pega legenda do Lingarr / colocada à mão)
WEBHOOK_TOKEN = E("WEBHOOK_TOKEN", "")

SONARR, SONARR_KEY = E("SONARR_URL", "http://sonarr:8989"), E("SONARR_API_KEY", "")
RADARR, RADARR_KEY = E("RADARR_URL", "http://radarr:7878"), E("RADARR_API_KEY", "")
BAZARR, BAZARR_KEY = E("BAZARR_URL", "http://bazarr:6767"), E("BAZARR_API_KEY", "")
JELLYFIN, JELLYFIN_KEY = E("JELLYFIN_URL", "http://jellyfin:8096"), E("JELLYFIN_API_KEY", "")
JELLYFIN_PUBLIC = E("JELLYFIN_PUBLIC_URL", "https://jf.ihome.dev.br").rstrip("/")
SEERR, SEERR_KEY = E("SEERR_URL", "http://seerr:5055"), E("SEERR_API_KEY", "")


def kv(s):  # "a=1,b=2" -> {"a": "1", "b": "2"}  (chaves em minúsculas)
    return {k.strip().lower(): v.strip() for k, v in (p.split("=", 1) for p in s.split(",") if "=" in p)}


REQ_NAMES = kv(E("REQUESTER_NAMES", ""))  # usuário do Seerr -> nome bonito
REQ_TG = kv(E("REQUESTER_TELEGRAM", ""))  # usuário do Seerr -> id do Telegram (menciona de verdade)
TG_TOKEN, TG_CHAT = E("TELEGRAM_TOKEN", ""), E("TELEGRAM_CHAT_ID", "")
TG_THREAD = E("TELEGRAM_THREAD_ID", "")
PT_AUDIO = {"por", "pob", "pt", "pb"}

logging.basicConfig(level=E("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("midia-notify")
lock = threading.Lock()


# ---------------------------------------------------------------- util
def http(method, url, headers=None, body=None, timeout=20):
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS item (
              kind TEXT NOT NULL,            -- 'ep' | 'movie'
              item_id INTEGER NOT NULL,      -- sonarr episodeId | radarr movieId
              parent_id INTEGER NOT NULL,    -- sonarr seriesId  | radarr movieId
              title TEXT, year INTEGER, season INTEGER, number INTEGER, ep_title TEXT,
              poster TEXT, provider TEXT,    -- provider = 'tvdb:123' / 'tmdb:456' (link Jellyfin)
              imported_at REAL NOT NULL, ready_at REAL, nosub INTEGER DEFAULT 0,
              last_rescan REAL DEFAULT 0, sent_at REAL,
              PRIMARY KEY (kind, item_id)
            );
            """
        )


# ---------------------------------------------------------------- entrada (webhooks)
def poster_of(images):
    for i in images or []:
        if i.get("coverType") == "poster":
            return i.get("remoteUrl") or i.get("url")
    return None


def add_items(rows):
    now = time.time()
    with lock, db() as c:
        for r in rows:
            # reimport do mesmo item que já foi avisado não gera aviso de novo
            c.execute(
                """INSERT INTO item (kind,item_id,parent_id,title,year,season,number,ep_title,poster,provider,imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(kind,item_id) DO NOTHING""",
                (r["kind"], r["item_id"], r["parent_id"], r["title"], r.get("year"), r.get("season"),
                 r.get("number"), r.get("ep_title"), r.get("poster"), r.get("provider"), now),
            )
    for r in rows:
        log.info("pendente: %s %s %s", r["kind"], r["title"],
                 f"S{r['season']:02d}E{r['number']:02d}" if r["kind"] == "ep" else r.get("year") or "")


def on_sonarr(p):
    if p.get("eventType") not in ("Download", "ImportComplete") or p.get("isUpgrade"):
        return
    s = p.get("series") or {}
    poster = poster_of(s.get("images"))
    if not poster and SONARR_KEY:
        try:
            poster = poster_of(http("GET", f"{SONARR}/api/v3/series/{s['id']}", {"X-Api-Key": SONARR_KEY}).get("images"))
        except Exception as e:
            log.warning("poster sonarr: %s", e)
    add_items([
        dict(kind="ep", item_id=ep["id"], parent_id=s["id"], title=s.get("title"), year=s.get("year"),
             season=ep.get("seasonNumber"), number=ep.get("episodeNumber"), ep_title=ep.get("title"),
             poster=poster, provider=f"tvdb:{s.get('tvdbId')}" if s.get("tvdbId") else None)
        for ep in p.get("episodes") or []
    ])


def on_radarr(p):
    if p.get("eventType") != "Download" or p.get("isUpgrade"):
        return
    m = p.get("movie") or {}
    poster = poster_of(m.get("images"))
    if not poster and RADARR_KEY:
        try:
            poster = poster_of(http("GET", f"{RADARR}/api/v3/movie/{m['id']}", {"X-Api-Key": RADARR_KEY}).get("images"))
        except Exception as e:
            log.warning("poster radarr: %s", e)
    add_items([dict(kind="movie", item_id=m["id"], parent_id=m["id"], title=m.get("title"), year=m.get("year"),
                    poster=poster, provider=f"tmdb:{m.get('tmdbId')}" if m.get("tmdbId") else None)])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, text="ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        if self.path.startswith("/health"):
            return self.reply(200)
        if self.path.startswith("/pending"):
            with db() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM item WHERE sent_at IS NULL ORDER BY imported_at")]
            body = json.dumps(rows, ensure_ascii=False, indent=1)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            return self.wfile.write(body.encode())
        self.reply(404, "not found")

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if WEBHOOK_TOKEN and urllib.parse.parse_qs(url.query).get("token", [""])[0] != WEBHOOK_TOKEN:
            return self.reply(403, "forbidden")
        try:
            p = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            return self.reply(400, "bad json")
        try:
            if p.get("eventType") == "Test":
                log.info("teste recebido de %s", url.path)
                send({"kind": "test", "title": f"Teste do {url.path.strip('/').capitalize()}"}, [])
            elif url.path == "/sonarr":
                on_sonarr(p)
            elif url.path == "/radarr":
                on_radarr(p)
            else:
                return self.reply(404, "not found")
        except Exception as e:
            log.exception("webhook %s: %s", url.path, e)
            return self.reply(500, "error")
        self.reply(200)


# ---------------------------------------------------------------- Bazarr
def bz(method, path, params):
    q = urllib.parse.urlencode(params, doseq=True)
    return http(method, f"{BAZARR}/api/{path}?{q}", {"X-API-KEY": BAZARR_KEY})


def has_pt_audio(d):
    return any((a.get("code3") or "") in PT_AUDIO or (a.get("code2") or "") in PT_AUDIO
               for a in d.get("audio_language") or [])


def check_pending():
    now = time.time()
    with lock, db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM item WHERE ready_at IS NULL")]
    if not rows:
        return
    eps = [r for r in rows if r["kind"] == "ep"]
    movies = [r for r in rows if r["kind"] == "movie"]
    status = {}  # (kind,id) -> dados do Bazarr
    try:
        for i in range(0, len(eps), 50):
            for d in bz("GET", "episodes", {"episodeid[]": [r["item_id"] for r in eps[i:i + 50]]}).get("data", []):
                status[("ep", d["sonarrEpisodeId"])] = d
        for i in range(0, len(movies), 50):
            for d in bz("GET", "movies", {"radarrid[]": [r["item_id"] for r in movies[i:i + 50]]}).get("data", []):
                status[("movie", d["radarrId"])] = d
    except Exception as e:
        log.warning("bazarr indisponível: %s", e)

    rescan = set()
    updates = []
    for r in rows:
        d = status.get((r["kind"], r["item_id"]))
        if d is not None and (not d.get("missing_subtitles") or has_pt_audio(d)):
            updates.append((now, 0, r["kind"], r["item_id"]))
            log.info("pronto: %s %s", r["title"], r["item_id"])
        elif now - r["imported_at"] > TIMEOUT_H * 3600:
            updates.append((now, 1, r["kind"], r["item_id"]))
            log.info("prazo esgotado (sem legenda): %s %s", r["title"], r["item_id"])
        elif d is not None and now - r["last_rescan"] > RESCAN_MIN * 60 and now - r["imported_at"] > RESCAN_MIN * 60:
            rescan.add((r["kind"], r["parent_id"]))
    with lock, db() as c:
        c.executemany("UPDATE item SET ready_at=?, nosub=? WHERE kind=? AND item_id=?", updates)
        for kind, pid in rescan:
            c.execute("UPDATE item SET last_rescan=? WHERE kind=? AND parent_id=? AND ready_at IS NULL", (now, kind, pid))
    for kind, pid in rescan:  # o resultado do scan aparece na próxima volta
        try:
            if kind == "ep":
                bz("PATCH", "series", {"seriesid": pid, "action": "scan-disk"})
            else:
                bz("PATCH", "movies", {"radarrid": pid, "action": "scan-disk"})
        except Exception as e:
            log.warning("scan-disk %s %s: %s", kind, pid, e)


# ---------------------------------------------------------------- envio
def ranges(nums):
    nums = sorted(set(n for n in nums if n is not None))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(f"E{nums[i]:02d}" + (f"–E{nums[j]:02d}" if j > i else ""))
        i = j + 1
    return ", ".join(out)


def jellyfin_link(item):
    """Link direto para o item no Jellyfin; sem chave/achado, cai na busca."""
    if JELLYFIN_KEY and item.get("provider"):
        try:
            prov, pid = item["provider"].split(":")
            t = "Series" if item["kind"] == "ep" else "Movie"
            q = urllib.parse.urlencode({"searchTerm": item["title"], "IncludeItemTypes": t, "Recursive": "true",
                                        "Fields": "ProviderIds", "Limit": 20})
            res = http("GET", f"{JELLYFIN}/Items?{q}", {"Authorization": f'MediaBrowser Token="{JELLYFIN_KEY}"'})
            for it in res.get("Items", []):
                if str((it.get("ProviderIds") or {}).get(prov.capitalize())) == pid:
                    return f"{JELLYFIN_PUBLIC}/web/#/details?id={it['Id']}"
        except Exception as e:
            log.warning("jellyfin: %s", e)
    return f"{JELLYFIN_PUBLIC}/web/#/search?query={urllib.parse.quote(item['title'] or '')}"


_req_cache = {"at": 0, "data": []}


def seerr_requests():
    if not SEERR_KEY:
        return []
    if time.time() - _req_cache["at"] > 300:
        try:
            res = http("GET", f"{SEERR}/api/v1/request?take=1000&filter=all&sort=added", {"X-Api-Key": SEERR_KEY})
            _req_cache.update(at=time.time(), data=res.get("results", []))
        except Exception as e:
            log.warning("seerr: %s", e)
    return _req_cache["data"]


def requesters(head, items):
    """Quem pediu no Seerr (série: só pedidos que incluem as temporadas avisadas)."""
    if not head.get("provider"):
        return []
    prov, pid = head["provider"].split(":")
    seasons = {i["season"] for i in items}
    out = []
    for r in seerr_requests():
        m = r.get("media") or {}
        if r.get("status") == 3 or str(m.get(f"{prov}Id")) != pid:  # 3 = recusado
            continue
        if head["kind"] == "ep":
            rs = {s.get("seasonNumber") for s in r.get("seasons") or []}
            if rs and not rs & seasons:
                continue
        u = r.get("requestedBy") or {}
        name = u.get("displayName") or u.get("jellyfinUsername") or u.get("username") or u.get("email")
        if name and name not in out:
            out.append(name)
    return out


def who(name):
    nice = esc(REQ_NAMES.get(name.lower(), name))
    tg = REQ_TG.get(name.lower())
    return f'<a href="tg://user?id={tg}">{nice}</a>' if tg else nice


def caption(head, items):
    h = esc(head["title"] or "?")
    if head["kind"] == "test":
        return f"🔔 <b>{h}</b>\nNotificações de mídia funcionando."
    nosub = any(i["nosub"] for i in items)
    if head["kind"] == "movie":
        lines = [f"🎬 <b>{h}</b>" + (f" ({head['year']})" if head.get("year") else "")]
    else:
        lines = [f"📺 <b>{h}</b>"]
        by_season = {}
        for i in items:
            by_season.setdefault(i["season"], []).append(i)
        for season in sorted(by_season, key=lambda s: (s is None, s)):
            its = by_season[season]
            label = "Especiais" if season == 0 else f"Temporada {season}"
            if len(its) == 1 and its[0].get("ep_title"):
                n = its[0]["number"]
                lines.append(f"{label} · E{n:02d} — {esc(its[0]['ep_title'])}" if n is not None else label)
            else:
                lines.append(f"{label} · {ranges(i['number'] for i in its)}")
    if nosub and all(i["nosub"] for i in items):
        lines.append("⚠️ Disponível, mas ainda sem legenda em português")
    elif nosub:
        lines.append("✅ Disponível (alguns episódios ainda sem legenda)")
    else:
        lines.append("✅ Disponível e legendado")
    req = requesters(head, items)
    if req:
        lines.append("🙋 Pedido por " + " e ".join(who(n) for n in req))
    lines.append(f'<a href="{jellyfin_link(head)}">▶️ Assistir no Jellyfin</a>')
    return "\n".join(lines)


def send(head, items):
    text = caption(head, items)
    if not (TG_TOKEN and TG_CHAT):
        log.info("[sem Telegram configurado] mensagem:\n%s", text)
        return True
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    msg = {"chat_id": TG_CHAT, "parse_mode": "HTML"}
    if TG_THREAD:
        msg["message_thread_id"] = int(TG_THREAD)
    try:
        if head.get("poster"):
            try:
                http("POST", f"{base}/sendPhoto", body={**msg, "photo": head["poster"], "caption": text})
                return True
            except Exception as e:
                log.warning("sendPhoto falhou (%s), mandando só texto", e)
        http("POST", f"{base}/sendMessage", body={**msg, "text": text, "link_preview_options": {"is_disabled": True}})
        return True
    except Exception as e:
        log.error("telegram: %s", e)
        return False


def flush_ready():
    now = time.time()
    with lock, db() as c:
        ready = [dict(r) for r in c.execute("SELECT * FROM item WHERE ready_at IS NOT NULL AND sent_at IS NULL")]
        waiting = {(r["kind"], r["parent_id"]) for r in c.execute("SELECT kind,parent_id FROM item WHERE ready_at IS NULL")}
    groups = {}
    for r in ready:
        groups.setdefault((r["kind"], r["parent_id"]), []).append(r)
    for key, items in groups.items():
        first = min(i["ready_at"] for i in items)
        last = max(i["ready_at"] for i in items)
        due = (key not in waiting and now - last >= QUIET_MIN * 60) or now - first >= GROUP_MIN * 60
        if not due:
            continue
        items.sort(key=lambda i: (i["season"] or 0, i["number"] or 0))
        if send(items[0], items):
            with lock, db() as c:
                c.executemany("UPDATE item SET sent_at=? WHERE kind=? AND item_id=?",
                              [(now, i["kind"], i["item_id"]) for i in items])
            log.info("enviado: %s (%d itens)", items[0]["title"], len(items))
    with lock, db() as c:  # histórico de 90 dias evita aviso repetido em reimport
        c.execute("DELETE FROM item WHERE sent_at IS NOT NULL AND sent_at < ?", (now - 90 * 86400,))


def worker():
    while True:
        try:
            check_pending()
            flush_ready()
        except Exception as e:
            log.exception("worker: %s", e)
        time.sleep(min(POLL, QUIET_MIN * 60))


if __name__ == "__main__":
    init_db()
    threading.Thread(target=worker, daemon=True).start()
    log.info("ouvindo em :%d (telegram %s)", PORT, "ok" if TG_TOKEN and TG_CHAT else "NÃO configurado")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
