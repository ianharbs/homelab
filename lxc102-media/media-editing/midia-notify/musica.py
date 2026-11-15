"""Central de música: avisa álbuns novos que apareceram no Navidrome (getAlbumList2 type=newest).

- 1ª execução (tabela vazia) só marca o que já existe como visto, para não inundar o canal.
- Espera o álbum parar de crescer (MUSIC_QUIET_MINUTES sem mudar o nº de faixas) antes de avisar,
  porque o DroppedNeedle importa faixa a faixa e o Navidrome pode ver o álbum pela metade.
- Chave = artista + álbum normalizados: reorganização de pasta (id novo no Navidrome) não gera aviso repetido.
- Álbum com `created` mais antigo que MUSIC_MAX_AGE_DAYS é marcado como visto sem aviso.
- Muitos álbuns de uma vez (> MUSIC_BATCH_LIMIT) viram uma única mensagem-resumo.
"""
import hashlib
import secrets
import sqlite3
import time
import urllib.parse
from datetime import datetime

from common import E, esc, http, log, norm, post

ND = E("NAVIDROME_URL", "http://navidrome:4533").rstrip("/")
ND_PUBLIC = E("NAVIDROME_PUBLIC_URL", "https://music.ihome.dev.br").rstrip("/")
ND_USER, ND_PASS = E("NAVIDROME_USER", ""), E("NAVIDROME_PASSWORD", "")
DB_PATH = E("DB_PATH", "/state/notify.db")
POLL = int(E("MUSIC_POLL_SECONDS", "600"))
QUIET_MIN = float(E("MUSIC_QUIET_MINUTES", "15"))
MAX_AGE_D = float(E("MUSIC_MAX_AGE_DAYS", "3"))
BATCH = int(E("MUSIC_BATCH_LIMIT", "6"))


def subsonic(method, params=None, raw=False):
    salt = secrets.token_hex(6)
    q = {"u": ND_USER, "t": hashlib.md5((ND_PASS + salt).encode()).hexdigest(), "s": salt,
         "v": "1.16.1", "c": "midia-notify", "f": "json", **(params or {})}
    res = http("GET", f"{ND}/rest/{method}?{urllib.parse.urlencode(q)}", raw=raw, timeout=30)
    if raw:
        return res
    r = res["subsonic-response"]
    if r.get("status") != "ok":
        raise RuntimeError(r.get("error"))
    return r


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS album (
            key TEXT PRIMARY KEY, nd_id TEXT, artist TEXT, name TEXT, year INTEGER, songs INTEGER,
            cover TEXT, first_seen REAL, last_change REAL, sent_at REAL)""")


def age_days(created):
    try:
        return (time.time() - datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) / 86400
    except Exception:
        return 0


def album_link(a):
    return f"{ND_PUBLIC}/app/#/album/{a['nd_id']}/show"


def announce(rows):
    if len(rows) > BATCH:
        lines = [f"🎵 <b>{len(rows)} álbuns novos na biblioteca</b>"]
        lines += [f"• {esc(a['artist'])} — <a href=\"{album_link(a)}\">{esc(a['name'])}</a>"
                  + (f" ({a['year']})" if a["year"] else "") for a in rows[:40]]
        if len(rows) > 40:
            lines.append(f"… e mais {len(rows) - 40}")
        return post("musica", "\n".join(lines))
    ok = True
    for a in rows:
        text = (f"🎵 <b>{esc(a['name'])}</b>" + (f" ({a['year']})" if a["year"] else "")
                + f"\n🎤 {esc(a['artist'])}\n💿 {a['songs']} faixa{'s' if a['songs'] != 1 else ''}"
                + f"\n✅ Disponível\n<a href=\"{album_link(a)}\">▶️ Ouvir no Navidrome</a>")
        cover = None
        if a["cover"]:
            try:
                cover = subsonic("getCoverArt", {"id": a["cover"], "size": 600}, raw=True)
            except Exception as e:
                log.warning("capa %s: %s", a["name"], e)
        ok = post("musica", text, photo_bytes=cover) and ok
    return ok


def check():
    albums = subsonic("getAlbumList2", {"type": "newest", "size": 100}).get("albumList2", {}).get("album", [])
    now = time.time()
    with db() as c:
        baseline = c.execute("SELECT COUNT(*) FROM album").fetchone()[0] == 0
        for a in albums:
            key = norm(a.get("artist")) + "|" + norm(a.get("name"))
            row = c.execute("SELECT * FROM album WHERE key=?", (key,)).fetchone()
            songs = a.get("songCount") or 0
            if row is None:
                old = baseline or age_days(a.get("created", "")) > MAX_AGE_D
                c.execute("INSERT INTO album VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (key, a["id"], a.get("artist"), a.get("name"), a.get("year"), songs, a.get("coverArt"),
                           now, now, now if old else None))
                if not old:
                    log.info("álbum novo (esperando completar): %s — %s", a.get("artist"), a.get("name"))
            elif row["songs"] != songs or row["nd_id"] != a["id"]:
                c.execute("UPDATE album SET songs=?, nd_id=?, cover=?, last_change=? WHERE key=?",
                          (songs, a["id"], a.get("coverArt"), now if row["songs"] != songs else row["last_change"], key))
        ready = [dict(r) for r in c.execute(
            "SELECT * FROM album WHERE sent_at IS NULL AND last_change <= ? ORDER BY first_seen", (now - QUIET_MIN * 60,))]
    if baseline:
        log.info("música: %d álbuns existentes marcados como vistos (sem aviso)", len(albums))
    if ready and announce(ready):
        with db() as c:
            c.executemany("UPDATE album SET sent_at=? WHERE key=?", [(now, a["key"]) for a in ready])
        log.info("música: avisados %d álbuns", len(ready))


def worker():
    if not (ND_USER and ND_PASS):
        log.warning("música: NAVIDROME_USER/NAVIDROME_PASSWORD não configurados — central de música desligada")
        return
    init_db()
    while True:
        try:
            check()
        except Exception as e:
            log.warning("música: %s", e)
        time.sleep(POLL)
