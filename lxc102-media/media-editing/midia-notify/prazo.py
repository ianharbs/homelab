"""Prazo no catálogo para pedidos de convidados (regra do Ian, 25/11/2026).

Pedido no Seerr que NÃO é do Ian nem da Ana (PRAZO_SEM_PRAZO) fica no catálogo:
  filmes 7 dias, séries 30 dias (a série recomeça a contar a cada episódio novo avisado).
O prazo sai no aviso de "disponível". 2 dias antes: aviso no grupo do tipo (marcando quem pediu) e no grupo
Aprovações, onde o Ian responde "manter". Ao vencer: apaga do Radarr/Sonarr COM os arquivos e libera a mídia
no Seerr (pode ser pedida de novo).

Sem prazo quando: ninguém pediu pelo Seerr (adicionado à mão); Ian/Ana também pediram; ou o item já estava
no Radarr/Sonarr antes do pedido (ex.: pediram a T3 de uma série que o Ian já tinha — apagar levaria tudo).
"""
import sqlite3
import time
from datetime import datetime

from common import DB_PATH, E, http, log

SONARR, SONARR_KEY = E("SONARR_URL", "http://sonarr:8989"), E("SONARR_API_KEY", "")
RADARR, RADARR_KEY = E("RADARR_URL", "http://radarr:7878"), E("RADARR_API_KEY", "")
SEERR, SEERR_KEY = E("SEERR_URL", "http://seerr:5055"), E("SEERR_API_KEY", "")
SEM_PRAZO = {x.strip().lower() for x in E("PRAZO_SEM_PRAZO", "ianharbs,anapgo").split(",") if x.strip()}
DIAS = {"movie": int(E("PRAZO_DIAS_FILME", "7")), "series": int(E("PRAZO_DIAS_SERIE", "30"))}
AVISO_DIAS = float(E("PRAZO_AVISO_DIAS", "2"))
APAGAR = E("PRAZO_APAGAR", "1") == "1"  # 0 = só avisa, não apaga (modo de teste)


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS prazo (
        kind TEXT NOT NULL,            -- 'movie' | 'series'
        arr_id INTEGER NOT NULL,       -- radarr movieId | sonarr seriesId
        title TEXT, provider TEXT, users TEXT, dias INTEGER,
        expires_at REAL NOT NULL, warned INTEGER DEFAULT 0, kept INTEGER DEFAULT 0, removed_at REAL,
        PRIMARY KEY (kind, arr_id))""")
    return c


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _seerr_requests_for(provider):
    prov, pid = provider.split(":")
    res = http("GET", f"{SEERR}/api/v1/request?take=1000&filter=all&sort=added", {"X-Api-Key": SEERR_KEY}, timeout=30)
    out = []
    for r in res.get("results", []):
        m = r.get("media") or {}
        if r.get("status") != 3 and str(m.get(f"{prov}Id")) == pid:  # 3 = recusado
            out.append(r)
    return out


def _user(r):
    u = r.get("requestedBy") or {}
    return (u.get("jellyfinUsername") or u.get("username") or u.get("email") or "").lower()


def avaliar(head):
    """Prazo deste aviso, ou None se não tem prazo. head = 1º item do aviso (notify.item)."""
    if not (SEERR_KEY and head.get("provider")) or head.get("kind") not in ("ep", "movie"):
        return None
    kind = "series" if head["kind"] == "ep" else "movie"
    try:
        reqs = _seerr_requests_for(head["provider"])
        if not reqs:
            return None
        users = sorted({_user(r) for r in reqs if _user(r)})
        if not users or set(users) & SEM_PRAZO:
            return None
        base, key, path = (SONARR, SONARR_KEY, "series") if kind == "series" else (RADARR, RADARR_KEY, "movie")
        added = _ts(http("GET", f"{base}/api/v3/{path}/{head['parent_id']}", {"X-Api-Key": key}).get("added"))
        first_req = min(t for t in (_ts(r.get("createdAt")) for r in reqs) if t)
        if added and added < first_req - 86400:
            log.info("prazo: %s já estava no %s antes do pedido — sem prazo", head.get("title"), path)
            return None
    except Exception as e:
        log.warning("prazo: não consegui avaliar %s (%s) — sem prazo", head.get("title"), e)
        return None
    dias = DIAS[kind]
    expires = time.time() + dias * 86400
    with db() as c:
        old = c.execute("SELECT * FROM prazo WHERE kind=? AND arr_id=?", (kind, head["parent_id"])).fetchone()
    if old and old["kept"]:
        return None
    if old and kind == "movie" and not old["removed_at"]:
        expires = old["expires_at"]  # reimport/upgrade do filme não renova o prazo
    return {"kind": kind, "arr_id": head["parent_id"], "title": head.get("title"), "provider": head["provider"],
            "users": users, "dias": dias, "expires_at": expires}


def linha(p):
    return f"⏳ Fica no catálogo até <b>{time.strftime('%d/%m', time.localtime(p['expires_at']))}</b> ({p['dias']} dias)"


def salvar(p):
    with db() as c:
        c.execute("INSERT INTO prazo (kind, arr_id, title, provider, users, dias, expires_at) VALUES (?,?,?,?,?,?,?)"
                  " ON CONFLICT(kind, arr_id) DO UPDATE SET title=excluded.title, users=excluded.users,"
                  " dias=excluded.dias, expires_at=excluded.expires_at, warned=0, removed_at=NULL",
                  (p["kind"], p["arr_id"], p["title"], p["provider"], ",".join(p["users"]), p["dias"], p["expires_at"]))


def manter(kind, arr_id):
    with db() as c:
        c.execute("UPDATE prazo SET kept=1 WHERE kind=? AND arr_id=?", (kind, arr_id))


def listar():
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM prazo WHERE removed_at IS NULL AND kept=0 ORDER BY expires_at")]
    return "\n".join(f"{'📺' if r['kind'] == 'series' else '🎬'} {r['title']} — até "
                     f"{time.strftime('%d/%m', time.localtime(r['expires_at']))} ({r['users']})" for r in rows) \
        or "Nada com prazo no catálogo agora."


def _apagar(r):
    if r["kind"] == "series":
        http("DELETE", f"{SONARR}/api/v3/series/{r['arr_id']}?deleteFiles=true&addImportListExclusion=false",
             {"X-Api-Key": SONARR_KEY}, timeout=120)
    else:
        http("DELETE", f"{RADARR}/api/v3/movie/{r['arr_id']}?deleteFiles=true&addImportExclusion=false",
             {"X-Api-Key": RADARR_KEY}, timeout=120)
    try:  # libera no Seerr (volta a poder ser pedido)
        for req in _seerr_requests_for(r["provider"]):
            mid = (req.get("media") or {}).get("id")
            if mid:
                http("DELETE", f"{SEERR}/api/v1/media/{mid}", {"X-Api-Key": SEERR_KEY})
                break
    except Exception as e:
        log.warning("prazo: Seerr não liberou %s: %s", r["title"], e)


def tick():
    import aprovacoes  # aqui para evitar import circular
    from whatsapp import mencao, post
    now = time.time()
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM prazo WHERE removed_at IS NULL AND kept=0")]
    for r in rows:
        when = time.strftime("%d/%m", time.localtime(r["expires_at"]))
        group = "series" if r["kind"] == "series" else "filmes"
        quem = " e ".join(mencao(u) for u in (r["users"] or "").split(",") if u)
        if not r["warned"] and r["expires_at"] - now <= AVISO_DIAS * 86400:
            post(group, f"⏳ <b>{r['title']}</b> sai do catálogo em <b>{when}</b>."
                        + (f"\n🙋 Pedido por {quem}" if quem else ""))
            aprovacoes.aviso_prazo(r, when)
            with db() as c:
                c.execute("UPDATE prazo SET warned=1 WHERE kind=? AND arr_id=?", (r["kind"], r["arr_id"]))
        if r["expires_at"] <= now:
            # alguém da casa pode ter pedido no meio do caminho
            try:
                if {_user(x) for x in _seerr_requests_for(r["provider"])} & SEM_PRAZO:
                    manter(r["kind"], r["arr_id"])
                    continue
            except Exception as e:
                log.warning("prazo: conferência no Seerr falhou (%s); adio %s", e, r["title"])
                continue
            if not APAGAR:
                log.info("prazo: [teste] apagaria %s", r["title"])
                continue
            try:
                _apagar(r)
            except Exception as e:
                log.error("prazo: falhou ao apagar %s: %s", r["title"], e)
                aprovacoes.avisar(f"⚠️ Não consegui tirar <b>{r['title']}</b> do catálogo: {e}")
                continue
            with db() as c:
                c.execute("UPDATE prazo SET removed_at=? WHERE kind=? AND arr_id=?", (now, r["kind"], r["arr_id"]))
            log.info("prazo: %s removido (%s dias)", r["title"], r["dias"])
            aprovacoes.avisar(f"🗑️ <b>{r['title']}</b> saiu do catálogo (prazo de {r['dias']} dias, pedido por "
                              f"{r['users']}).")


def worker():
    db().close()
    while True:
        try:
            tick()
        except Exception as e:
            log.exception("prazo: %s", e)
        time.sleep(1800)
