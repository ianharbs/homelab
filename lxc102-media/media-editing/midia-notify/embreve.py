"""Filmes e séries "em breve" (para o widget do iHome Player / Kodi).

Mesma regra das seções "Filmes em breve"/"Séries em breve" do Jellyfin (Home Screen Sections):
- filmes: Radarr, monitorados e ainda sem arquivo, com lançamento em mídia física ou digital nos
  próximos EMBREVE_DIAS_FILME dias (padrão 90);
- séries: Sonarr, próximo episódio monitorado e ainda sem arquivo nos próximos EMBREVE_DIAS_SERIE dias (30).
GET /embreve (sem token: só títulos e capas, usado pelo Kodi na rede de casa) -> JSON.
"""
import time
from datetime import datetime, timedelta, timezone

from common import E, http, log

SONARR, SONARR_KEY = E("SONARR_URL", "http://sonarr:8989"), E("SONARR_API_KEY", "")
RADARR, RADARR_KEY = E("RADARR_URL", "http://radarr:7878"), E("RADARR_API_KEY", "")
DIAS_FILME = int(E("EMBREVE_DIAS_FILME", "90"))
DIAS_SERIE = int(E("EMBREVE_DIAS_SERIE", "30"))
_cache = {"at": 0, "data": None}


def _img(images, kind):
    for i in images or []:
        if i.get("coverType") == kind:
            return i.get("remoteUrl") or i.get("url") or ""
    return ""


def _dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def filmes():
    hoje = datetime.now(timezone.utc)
    fim = hoje + timedelta(days=DIAS_FILME)
    res = http("GET", f"{RADARR}/api/v3/calendar?unmonitored=false&start={hoje:%Y-%m-%d}&end={fim:%Y-%m-%d}",
               {"X-Api-Key": RADARR_KEY}, timeout=30)
    out = []
    for m in res or []:
        if m.get("hasFile"):
            continue
        datas = []
        for campo, rotulo in (("physicalRelease", "mídia física"), ("digitalRelease", "digital")):
            d = _dt(m.get(campo)) if m.get(campo) else None
            if d and hoje - timedelta(days=1) <= d <= fim:
                datas.append((d, rotulo))
        if not datas:
            continue
        d, rotulo = min(datas)
        out.append({"tipo": "filme", "titulo": m.get("title"), "ano": m.get("year"), "data": d.date().isoformat(),
                    "lancamento": rotulo, "sinopse": m.get("overview") or "", "tmdb": m.get("tmdbId"),
                    "poster": _img(m.get("images"), "poster"), "fanart": _img(m.get("images"), "fanart")})
    return sorted(out, key=lambda x: x["data"])


def series():
    hoje = datetime.now(timezone.utc)
    fim = hoje + timedelta(days=DIAS_SERIE)
    res = http("GET", f"{SONARR}/api/v3/calendar?unmonitored=false&includeSeries=true"
               f"&start={hoje:%Y-%m-%d}&end={fim:%Y-%m-%d}", {"X-Api-Key": SONARR_KEY}, timeout=30)
    prox = {}
    for e in res or []:
        if e.get("hasFile") or not e.get("airDateUtc"):
            continue
        sid = e.get("seriesId")
        if sid not in prox or e["airDateUtc"] < prox[sid]["airDateUtc"]:
            prox[sid] = e
    out = []
    for e in prox.values():
        s = e.get("series") or {}
        d = _dt(e["airDateUtc"]).astimezone()
        out.append({"tipo": "serie", "titulo": s.get("title"), "ano": s.get("year"), "data": d.date().isoformat(),
                    "episodio": f"T{e.get('seasonNumber', 0)}E{e.get('episodeNumber', 0):02d}",
                    "nome_episodio": e.get("title") or "", "sinopse": e.get("overview") or s.get("overview") or "",
                    "tvdb": s.get("tvdbId"), "poster": _img(s.get("images"), "poster"),
                    "fanart": _img(s.get("images"), "fanart")})
    return sorted(out, key=lambda x: x["data"])


def dados():
    """Cache de 15 min (o Kodi pede a cada vez que a tela inicial abre)."""
    if _cache["data"] is None or time.time() - _cache["at"] > 900:
        d = {"filmes": [], "series": [], "gerado": time.strftime("%Y-%m-%d %H:%M")}
        for k, fn in (("filmes", filmes), ("series", series)):
            try:
                d[k] = fn()
            except Exception as e:
                log.warning("embreve %s: %s", k, e)
        _cache.update(at=time.time(), data=d)
    return _cache["data"]
