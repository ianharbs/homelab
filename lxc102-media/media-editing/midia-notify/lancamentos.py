"""Lançamentos das bandas favoritas -> tópico Música.

Bandas seguidas = artistas favoritados (estrela) no Navidrome + os RELEASE_TOP_PLAYED mais ouvidos
+ os adicionados com /seguir (menos os tirados com /naoseguir). Lista recalculada a cada volta.
Discografia vem da API pública do Deezer (sem chave). Avisa álbum/EP (RELEASE_TYPES) ainda não visto
com data de lançamento nos últimos RELEASE_RECENT_DAYS (ou futura: "lança em"). O resto da discografia
é marcado como visto na 1ª consulta do artista, sem aviso.
"""
import sqlite3
import time
import urllib.parse
from collections import Counter
from datetime import date, timedelta

import musica
from common import DB_PATH, E, esc, http, log, norm, post

POLL_H = float(E("RELEASE_POLL_HOURS", "12"))
TOP = int(E("RELEASE_TOP_PLAYED", "30"))
RECENT = int(E("RELEASE_RECENT_DAYS", "14"))
TYPES = {t.strip() for t in E("RELEASE_TYPES", "album,ep").split(",") if t.strip()}
TYPE_PT = {"album": "álbum", "ep": "EP", "single": "single", "compile": "coletânea"}
IGNORE = {"various artists", "varios artistas", "trilha sonora", "soundtrack"}


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS follow (
            key TEXT PRIMARY KEY, name TEXT, mode TEXT,      -- mode: 'manual' | 'off' (só ajustes à mão)
            at REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS dz_artist (
            key TEXT PRIMARY KEY, name TEXT, dz_id INTEGER, checked REAL)""")
        c.execute("CREATE TABLE IF NOT EXISTS dz_release (id INTEGER PRIMARY KEY, dz_artist INTEGER, title TEXT,"
                  " rdate TEXT, rtype TEXT, sent_at REAL)")


def dz(path):
    return http("GET", f"https://api.deezer.com/{path}", timeout=20)


def followed():
    """{chave: (nome, origem)} das bandas seguidas agora."""
    out = {}
    try:
        st = musica.subsonic("getStarred2").get("starred2", {})
        for a in st.get("artist", []):
            out[norm(a["name"])] = (a["name"], "favorita")
        plays = Counter()
        for a in musica.subsonic("getAlbumList2", {"type": "frequent", "size": 500}).get("albumList2", {}).get("album", []):
            if a.get("artist"):
                plays[a["artist"]] += a.get("playCount", 0)
        n = 0
        for name, _ in plays.most_common():
            if n >= TOP:
                break
            if norm(name) in IGNORE or any(sep in name for sep in (" & ", "; ", " feat")):
                continue  # parcerias e coletâneas não são "banda"
            out.setdefault(norm(name), (name, "mais ouvida"))
            n += 1
    except Exception as e:
        log.warning("lançamentos: Navidrome: %s", e)
    with db() as c:
        for r in c.execute("SELECT * FROM follow"):
            if r["mode"] == "off":
                out.pop(r["key"], None)
            else:
                out[r["key"]] = (r["name"], "seguida")
    return out


def deezer_id(key, name):
    with db() as c:
        r = c.execute("SELECT dz_id FROM dz_artist WHERE key=?", (key,)).fetchone()
    if r:
        return r["dz_id"]
    res = dz(f"search/artist?q={urllib.parse.quote(name)}&limit=10").get("data", [])
    same = [a for a in res if norm(a["name"]) == key]
    pick = max(same, key=lambda a: a.get("nb_fan", 0)) if same else None
    with db() as c:
        c.execute("INSERT OR REPLACE INTO dz_artist VALUES (?,?,?,?)", (key, name, pick["id"] if pick else 0, 0))
    if not pick:
        log.info("lançamentos: '%s' não encontrado no Deezer", name)
    return pick["id"] if pick else 0


def in_library(artist, title):
    try:
        r = musica.subsonic("search3", {"query": title, "albumCount": 10, "artistCount": 0, "songCount": 0})
        return any(norm(a.get("name")) == norm(title) and norm(artist) in norm(a.get("artist"))
                   for a in r.get("searchResult3", {}).get("album", []))
    except Exception:
        return False


def announce(artist, rel, why):
    today = date.today().isoformat()
    future = rel["release_date"] > today
    kind = TYPE_PT.get(rel["record_type"], rel["record_type"])
    d = date.fromisoformat(rel["release_date"]).strftime("%d/%m/%Y")
    lines = [f"🆕 <b>{esc(rel['title'])}</b> — novo {kind}" if not future else
             f"🗓️ <b>{esc(rel['title'])}</b> — {kind} anunciado",
             f"🎤 {esc(artist)} <i>({why})</i>",
             ("📅 Lança em " if future else "📅 Lançado em ") + d]
    try:
        n = dz(f"album/{rel['id']}").get("nb_tracks")
        if n:
            lines.append(f"💿 {n} faixa{'s' if n != 1 else ''}")
    except Exception:
        pass
    if not future and in_library(artist, rel["title"]):
        lines.append("✅ Já está na biblioteca")
    else:
        lines.append('<a href="https://dn.ihome.dev.br">⬇️ Pedir no DroppedNeedle</a>')
    lines.append(f'<a href="https://www.deezer.com/album/{rel["id"]}">🎧 Ver no Deezer</a>')
    return post("musica", "\n".join(lines), photo=rel.get("cover_xl") or rel.get("cover_big"))


def check_artist(key, name, why):
    aid = deezer_id(key, name)
    if not aid:
        return
    rels = dz(f"artist/{aid}/albums?limit=200").get("data", [])
    cut = (date.today() - timedelta(days=RECENT)).isoformat()
    with db() as c:
        seen = {r["id"] for r in c.execute("SELECT id FROM dz_release WHERE dz_artist=?", (aid,))}
    for rel in sorted(rels, key=lambda r: r.get("release_date") or ""):
        if rel["id"] in seen or not rel.get("release_date"):
            continue
        new = rel.get("record_type") in TYPES and rel["release_date"] >= cut
        sent = None
        if new:
            log.info("lançamento: %s — %s (%s)", name, rel["title"], rel["release_date"])
            if not announce(name, rel, why):
                continue  # tenta de novo na próxima volta
            sent = time.time()
        with db() as c:
            c.execute("INSERT OR IGNORE INTO dz_release VALUES (?,?,?,?,?,?)",
                      (rel["id"], aid, rel["title"], rel["release_date"], rel.get("record_type"), sent))
    with db() as c:
        c.execute("UPDATE dz_artist SET checked=? WHERE key=?", (time.time(), key))


def run_once():
    arts = followed()
    for key, (name, why) in arts.items():
        try:
            check_artist(key, name, why)
        except Exception as e:
            log.warning("lançamentos %s: %s", name, e)
        time.sleep(0.5)  # Deezer aceita ~50 req / 5 s
    log.info("lançamentos: %d bandas verificadas", len(arts))


# ---------------------------------------------------------------- comandos (statusbot)
def cmd_follow(name):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO follow VALUES (?,?,?,?)", (norm(name), name, "manual", time.time()))
    try:
        check_artist(norm(name), name, "seguida")
    except Exception as e:
        log.warning("seguir %s: %s", name, e)
    ok = deezer_id(norm(name), name)
    return (f"Seguindo {name}. Aviso novos álbuns e EPs no tópico Música." if ok else
            f"Anotei {name}, mas não achei esse nome exato no Deezer — confira a grafia.")


def cmd_unfollow(name):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO follow VALUES (?,?,?,?)", (norm(name), name, "off", time.time()))
    return f"Não sigo mais {name}."


def cmd_list():
    arts = followed()
    by = {}
    for name, why in arts.values():
        by.setdefault(why, []).append(name)
    lines = [f"Sigo {len(arts)} bandas (lançamentos de álbum/EP vão para o tópico Música):"]
    for why in ("favorita", "seguida", "mais ouvida"):
        if by.get(why):
            lines.append(f"\n{why.capitalize()}s: " + ", ".join(sorted(by[why], key=str.lower)))
    lines.append("\n/seguir <banda> · /naoseguir <banda> · favoritar o artista no Navidrome também conta.")
    return "\n".join(lines)


def worker():
    if not (musica.ND_USER and musica.ND_PASS):
        return
    init_db()
    time.sleep(60)
    while True:
        try:
            run_once()
        except Exception as e:
            log.warning("lançamentos: %s", e)
        time.sleep(POLL_H * 3600)
