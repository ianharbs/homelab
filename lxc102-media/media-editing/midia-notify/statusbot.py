"""Central de status: responde perguntas ("cadê o filme X?", "status da série Y", "o que tá baixando?").

Recebe mensagens por long polling (getUpdates) do @iHomeSCbot:
  - tópico "Status" do grupo com Tópicos (o bot precisa ser admin para ver mensagens sem comando);
    nos outros tópicos só responde a comando/menção;
  - posts no canal TELEGRAM_CHAT_STATUS (alternativa a tópicos; o bot precisa ser admin do canal);
  - conversa privada com quem está em STATUS_ALLOWED_USERS / REQUESTER_TELEGRAM;
  - comandos/menções no grupo antigo (TELEGRAM_CHAT_ID).
No tópico Homelab, o que o Ian escreve vai para o agente no pve (to_agent; pve-host/telegram-agent).
Uma IA com API compatível com OpenAI (LLM_URL/LLM_API_KEY/LLM_MODEL; ex.: Gemini grátis) escolhe as
ferramentas e escreve a resposta. Sem IA (ou se ela falhar), responde direto pelas ferramentas.
O Ollama local (CPU) é lento demais para conversa (> 10 min por resposta em 15/11/2026).
"""
import difflib
import json
import re
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from common import CHANNELS, DN_PUBLIC, E, TG_GROUP, TG_TOKEN, TOPICS, forum_id, http, kv_get, kv_set, log, norm, tg, topic_id
import lancamentos
import musica

SONARR, SONARR_KEY = E("SONARR_URL", "http://sonarr:8989"), E("SONARR_API_KEY", "")
RADARR, RADARR_KEY = E("RADARR_URL", "http://radarr:7878"), E("RADARR_API_KEY", "")
BAZARR, BAZARR_KEY = E("BAZARR_URL", "http://bazarr:6767"), E("BAZARR_API_KEY", "")
SHELFARR, SHELFARR_KEY = E("SHELFARR_URL", "http://shelfarr:80"), E("SHELFARR_API_TOKEN", "")
JELLYFIN_PUBLIC = E("JELLYFIN_PUBLIC_URL", "https://video.ihome.dev.br").rstrip("/")  # Jellyfin público (CT 109)
SEERR_PUBLIC = E("SEERR_PUBLIC_URL", "https://seerr.ihome.dev.br").rstrip("/")
LLM_URL, LLM_KEY, LLM_MODEL = E("LLM_URL", "").rstrip("/"), E("LLM_API_KEY", ""), E("LLM_MODEL", "")
DB_PATH = E("DB_PATH", "/state/notify.db")
ALLOWED = {x.strip() for x in E("STATUS_ALLOWED_USERS", "").split(",") if x.strip()} | {
    p.split("=", 1)[1].strip() for p in E("REQUESTER_TELEGRAM", "").split(",") if "=" in p}
BOT_NAME = {"username": ""}
# Agente do homelab (pve-host/telegram-agent): mensagens dos AGENT_ADMINS no tópico AGENT_THREAD
# (Homelab) e cliques nos botões "ag:…" vão para o Claude Code no pve em vez da central de status.
AGENT_URL, AGENT_TOKEN = E("AGENT_URL", "").rstrip("/"), E("AGENT_TOKEN", "")
AGENT_THREAD = E("AGENT_THREAD", "25")
AGENT_ADMINS = {x.strip() for x in E("AGENT_ADMINS", "").split(",") if x.strip()}

BOOK_STATUS = {"pending": "na fila para buscar", "searching": "procurando", "not_found": "não encontrado",
               "downloading": "baixando", "processing": "importando", "completed": "disponível",
               "failed": "falhou", "awaiting_purchase": "aguardando compra"}
SERIES_STATUS = {"continuing": "em exibição", "ended": "encerrada", "upcoming": "ainda não estreou"}


# ---------------------------------------------------------------- estado
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")  # usado por common.kv_*
        c.execute("CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY, type TEXT, title TEXT, status TEXT, at REAL)")


def known_chats():
    try:
        with db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM chat ORDER BY at DESC")]
    except sqlite3.OperationalError:
        return []


# ---------------------------------------------------------------- busca por título
_cache = {}


def cached(key, fn, ttl=300):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


NUMS = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "um": "1", "dois": "2", "tres": "3", "quatro": "4",
        "one": "1", "two": "2", "three": "3", "four": "4"}


def numbers(s):
    return {NUMS.get(w, w) for w in s.split() if w.isdigit() or w in NUMS}


def score(q, names):
    best = 0.0
    qn = numbers(q)
    for n in names:
        n = norm(n)
        if not n:
            continue
        if qn and numbers(n) and not (qn & numbers(n)):  # "parte 2" não é "parte 1"
            continue
        if n == q:
            return 1.0
        if len(q) >= 3 and re.search(rf"\b{re.escape(q)}\b", n):  # pergunta é parte do título
            best = max(best, 0.85 + 0.1 * len(q) / len(n))
        elif len(n) >= 4 and re.search(rf"\b{re.escape(n)}\b", q):  # título curto dentro da pergunta
            best = max(best, 0.6 + 0.3 * len(n) / len(q))
        best = max(best, difflib.SequenceMatcher(None, q, n).ratio())
    return best


def find(items, query, names, limit=3, cut=0.62):
    q = norm(query)
    if not q:
        return []
    ranked = sorted(((score(q, names(i)), i) for i in items), key=lambda x: -x[0])
    top = [i for s, i in ranked if s >= cut]
    if ranked and ranked[0][0] >= 0.97:  # título exato: não mistura com os parecidos
        top = [i for s, i in ranked if s >= 0.97]
    elif ranked and ranked[0][0] >= 0.85:  # achou bem: descarta os só "parecidos"
        top = [i for s, i in ranked if s >= 0.85]
    return top[:limit]


def strong(items, query, names):
    return bool(items) and score(norm(query), names(items[0])) >= 0.85


def not_found(kind, titulo, near, look):
    """Nada bateu bem: lista os parecidos da biblioteca e o que existe no TMDB/TVDB para pedir."""
    app, base = ("Radarr", "TMDB") if kind == "filme" else ("Sonarr", "TVDB")
    msg = [f"Não achei {'o filme' if kind == 'filme' else 'a série'} '{titulo}' no {app}."]
    if near:
        msg.append("Parecidos que estão na biblioteca: " + "; ".join(f"{i['title']} ({i.get('year')})" for i in near))
    if look:
        msg.append(f"No {base} existe: " + "; ".join(f"{i.get('title')} ({i.get('year')})" for i in look[:3])
                   + f". Para pedir: {SEERR_PUBLIC}")
    return "\n".join(msg)


def ago_or_when(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso
    return d.strftime("%d/%m/%Y %H:%M") if d.hour or d.minute else d.strftime("%d/%m/%Y")


def is_past(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")) < datetime.now(timezone.utc)
    except Exception:
        return False


def arr(base, key, path):
    return http("GET", f"{base}/api/v3/{path}", {"X-Api-Key": key}, timeout=30)


def queue_of(base, key, extra):
    res = arr(base, key, f"queue?pageSize=500&{extra}")
    return res.get("records", []) if isinstance(res, dict) else res


def fmt_queue(q):
    size, left = q.get("size") or 0, q.get("sizeleft") or 0
    pct = f"{100 * (size - left) / size:.0f}%" if size else "?"
    st = q.get("trackedDownloadState") or q.get("status") or ""
    st = {"downloading": "baixando", "importPending": "esperando importar", "importing": "importando",
          "failedPending": "falhou", "importBlocked": "importação bloqueada"}.get(st, st)
    line = f"{st} {pct}"
    if q.get("timeleft") and st == "baixando":
        line += f", falta {q['timeleft']}"
    if q.get("status") in ("paused", "queued"):
        line += f" ({'pausado' if q['status'] == 'paused' else 'na fila do cliente'})"
    msgs = [m for s in q.get("statusMessages") or [] for m in s.get("messages") or []]
    if msgs:
        line += " — " + "; ".join(msgs[:2])
    if q.get("errorMessage"):
        line += f" — {q['errorMessage']}"
    return line


def pending_subs(kind, parent_id):
    try:
        with db() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM item WHERE kind=? AND parent_id=? AND sent_at IS NULL", (kind, parent_id))]
    except sqlite3.OperationalError:
        return []


def jf_search(title):
    return f"{JELLYFIN_PUBLIC}/web/#/search?query={urllib.parse.quote(title or '')}"


# ---------------------------------------------------------------- ferramentas
def status_filme(titulo):
    movies = cached("radarr", lambda: arr(RADARR, RADARR_KEY, "movie"))
    names = lambda m: [m.get("title"), m.get("originalTitle")] + [a.get("title") for a in m.get("alternateTitles") or []]  # noqa: E731
    hits = find(movies, titulo, names)
    if not strong(hits, titulo, names):
        try:
            look = arr(RADARR, RADARR_KEY, f"movie/lookup?term={urllib.parse.quote(titulo)}")[:3]
        except Exception:
            look = []
        have = {m["tmdbId"] for m in movies}
        return not_found("filme", titulo, hits, [x for x in look if x.get("tmdbId") not in have])
    out = []
    queue = queue_of(RADARR, RADARR_KEY, "includeMovie=false")
    for m in hits:
        lines = [f"🎬 {m['title']} ({m.get('year')})"]
        qs = [q for q in queue if q.get("movieId") == m["id"]]
        pend = pending_subs("movie", m["id"])
        if m.get("hasFile"):
            if pend:
                lines.append("Baixado e importado; esperando legenda em português (o aviso sai quando legendar"
                             " ou em até 12 h).")
            else:
                lines.append("✅ Disponível no Jellyfin.")
            try:
                b = http("GET", f"{BAZARR}/api/movies?radarrid[]={m['id']}", {"X-API-KEY": BAZARR_KEY}).get("data", [])
                if b:
                    miss = [s.get("name") or s.get("code2") for s in b[0].get("missing_subtitles") or []]
                    have = [s.get("name") or s.get("code2") for s in b[0].get("subtitles") or [] if s.get("path")]
                    lines.append("Legendas: " + (", ".join(have) or "nenhuma")
                                 + (f" | faltando: {', '.join(miss)}" if miss else ""))
            except Exception as e:
                log.warning("bazarr: %s", e)
            q = (m.get("movieFile") or {}).get("quality", {}).get("quality", {}).get("name")
            if q:
                lines.append(f"Qualidade: {q}")
            lines.append(f"Link: {jf_search(m['title'])}")
        elif qs:
            lines += [f"Download: {fmt_queue(q)}" for q in qs]
        else:
            st = {"announced": "anunciado", "inCinemas": "nos cinemas", "released": "lançado"}.get(m.get("status"), m.get("status"))
            lines.append(f"Ainda não baixado ({st}).")
            dates = [(n, m.get(k)) for n, k in (("cinema", "inCinemas"), ("digital", "digitalRelease"),
                                                 ("físico", "physicalRelease")) if m.get(k)]
            if dates:
                lines.append("Lançamento: " + ", ".join(f"{n} {ago_or_when(d)}" for n, d in dates))
            if not m.get("monitored"):
                lines.append("⚠️ Não está monitorado (o Radarr não vai procurar).")
            elif m.get("isAvailable"):
                lines.append("Já foi lançado: o Radarr está procurando um release que sirva.")
            else:
                lines.append("O Radarr só começa a procurar depois do lançamento digital/físico.")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def status_serie(titulo, temporada=None):
    series = cached("sonarr", lambda: arr(SONARR, SONARR_KEY, "series"))
    names = lambda s: [s.get("title"), s.get("sortTitle")] + [a.get("title") for a in s.get("alternateTitles") or []]  # noqa: E731
    hits = find(series, titulo, names)
    if not strong(hits, titulo, names):
        try:
            look = arr(SONARR, SONARR_KEY, f"series/lookup?term={urllib.parse.quote(titulo)}")[:3]
        except Exception:
            look = []
        have = {s["tvdbId"] for s in series}
        return not_found("serie", titulo, hits, [x for x in look if x.get("tvdbId") not in have])
    queue = queue_of(SONARR, SONARR_KEY, "includeEpisode=true")
    out = []
    for s in hits[:2]:
        st = s.get("statistics") or {}
        lines = [f"📺 {s['title']} ({s.get('year')}) — {SERIES_STATUS.get(s.get('status'), s.get('status'))}"
                 + ("" if s.get("monitored") else " — ⚠️ não monitorada")]
        lines.append(f"Episódios baixados: {st.get('episodeFileCount', 0)} de {st.get('episodeCount', 0)} já exibidos"
                     f" (total na série: {st.get('totalEpisodeCount', 0)})")
        complete = 0
        for se in s.get("seasons") or []:
            n, ss = se.get("seasonNumber"), se.get("statistics") or {}
            if n == 0 and not ss.get("episodeFileCount"):
                continue
            if temporada is not None and n != int(temporada):
                continue
            if temporada is None and ss.get("episodeFileCount", 0) >= ss.get("totalEpisodeCount", 0) > 0:
                complete += 1
                continue
            mark = "" if se.get("monitored") else " (não monitorada)"
            lines.append(f"  {'Especiais' if n == 0 else f'T{n}'}: {ss.get('episodeFileCount', 0)}/"
                         f"{ss.get('episodeCount', 0)} exibidos, {ss.get('totalEpisodeCount', 0)} no total{mark}")
        if complete:
            lines.append(f"  {complete} temporada{'s' if complete > 1 else ''} completa{'s' if complete > 1 else ''}"
                         + (" (as que faltam estão acima)" if len(lines) > 2 else ""))
        if s.get("nextAiring"):
            lines.append(f"Próximo episódio: {ago_or_when(s['nextAiring'])}")
        if temporada is not None:
            try:
                eps = arr(SONARR, SONARR_KEY, f"episode?seriesId={s['id']}&seasonNumber={int(temporada)}")
                miss = [e for e in eps if not e.get("hasFile") and e.get("airDateUtc") and is_past(e["airDateUtc"])]
                fut = [e for e in eps if e.get("airDateUtc") and not is_past(e["airDateUtc"])]
                if miss:
                    lines.append("Faltando (já exibidos): " + ", ".join(f"E{e['episodeNumber']:02d}" for e in miss))
                if fut:
                    lines.append("Ainda vão passar: " + ", ".join(
                        f"E{e['episodeNumber']:02d} {ago_or_when(e['airDateUtc'])}" for e in fut[:6]))
            except Exception as e:
                log.warning("sonarr episodes: %s", e)
        qs = [q for q in queue if q.get("seriesId") == s["id"]]
        for q in qs[:8]:
            ep = q.get("episode") or {}
            lines.append(f"Baixando S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}: {fmt_queue(q)}")
        if len(qs) > 8:
            lines.append(f"… e mais {len(qs) - 8} na fila")
        pend = pending_subs("ep", s["id"])
        if pend:
            lines.append("Importados esperando legenda PT: " + ", ".join(
                f"S{p['season']:02d}E{p['number']:02d}" for p in pend[:10]))
        lines.append(f"Link: {jf_search(s['title'])}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def shelfarr_requests():
    return http("GET", f"{SHELFARR}/api/v1/requests?limit=100",
                {"Authorization": f"Bearer {SHELFARR_KEY}"}).get("requests", [])


def fmt_book(r):
    b = r.get("book") or {}
    line = (f"{'🎧' if b.get('book_type') == 'audiobook' else '📚'} {b.get('title')}"
            + (f" — {b['author']}" if b.get("author") else "")
            + f": {BOOK_STATUS.get(r.get('status'), r.get('status'))}")
    if r.get("attention_needed"):
        line += " ⚠️ precisa de atenção (escolher release manualmente no Shelfarr)"
    if r.get("issue_description"):
        line += f" — {r['issue_description']}"
    return line + f" (pedido em {ago_or_when(r.get('created_at'))})"


def status_livro(busca):
    if not SHELFARR_KEY:
        return "Shelfarr não configurado (falta SHELFARR_API_TOKEN)."
    reqs = cached("shelfarr", shelfarr_requests, ttl=60)
    hits = find(reqs, busca, lambda r: [(r.get("book") or {}).get("title"), (r.get("book") or {}).get("author"),
                                        r.get("collection_title")], limit=6, cut=0.6)
    if not hits:
        return f"Nenhum pedido de livro parecido com '{busca}' no Shelfarr."
    return "\n".join(fmt_book(r) for r in hits)


def status_musica(busca):
    if not (musica.ND_USER and musica.ND_PASS):
        return "Navidrome não configurado."
    r = musica.subsonic("search3", {"query": busca, "artistCount": 3, "albumCount": 8, "songCount": 5}).get("searchResult3", {})
    lines = []
    for a in r.get("artist", []):
        lines.append(f"🎤 {a.get('name')}: {a.get('albumCount', 0)} álbuns na biblioteca")
    for a in r.get("album", []):
        lines.append(f"💿 {a.get('artist')} — {a.get('name')}" + (f" ({a['year']})" if a.get("year") else "")
                     + f", {a.get('songCount', 0)} faixas — {musica.ND_PUBLIC}/app/#/album/{a['id']}/show")
    for s in r.get("song", [])[:5]:
        lines.append(f"🎵 {s.get('artist')} — {s.get('title')} (álbum {s.get('album')})")
    return "\n".join(lines) or (f"Nada parecido com '{busca}' na biblioteca do Navidrome (pode ainda estar baixando "
                                f"no DroppedNeedle: {DN_PUBLIC}/search?q={urllib.parse.quote(busca)}).")


def fila_downloads():
    lines = []
    try:
        for q in queue_of(RADARR, RADARR_KEY, "includeMovie=true"):
            lines.append(f"🎬 {(q.get('movie') or {}).get('title') or q.get('title')}: {fmt_queue(q)}")
    except Exception as e:
        lines.append(f"(Radarr indisponível: {e})")
    try:
        by = {}
        for q in queue_of(SONARR, SONARR_KEY, "includeSeries=true&includeEpisode=true"):
            by.setdefault((q.get("series") or {}).get("title") or q.get("title"), []).append(q)
        for title, qs in by.items():
            eps = ", ".join(f"S{(q.get('episode') or {}).get('seasonNumber', 0):02d}E"
                            f"{(q.get('episode') or {}).get('episodeNumber', 0):02d}" for q in qs[:6])
            lines.append(f"📺 {title} ({eps}{'…' if len(qs) > 6 else ''}): {fmt_queue(qs[0])}")
    except Exception as e:
        lines.append(f"(Sonarr indisponível: {e})")
    if SHELFARR_KEY:
        try:
            for r in shelfarr_requests():
                if r.get("status") in ("pending", "searching", "downloading", "processing") or r.get("attention_needed"):
                    lines.append(fmt_book(r))
        except Exception as e:
            lines.append(f"(Shelfarr indisponível: {e})")
    try:
        with db() as c:
            pend = [dict(x) for x in c.execute("SELECT * FROM item WHERE sent_at IS NULL ORDER BY imported_at")]
        if pend:
            names = {}
            for p in pend:
                names.setdefault(p["title"], []).append(p)
            lines.append("⏳ Importados esperando legenda PT: " + "; ".join(
                f"{t} ({len(v)} ep.)" if v[0]["kind"] == "ep" else t for t, v in names.items()))
    except sqlite3.OperationalError:
        pass
    return "\n".join(lines) or "Nada baixando nem esperando legenda agora."


def recentes(dias=3):
    since = time.time() - float(dias) * 86400
    lines = []
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM item WHERE sent_at >= ? ORDER BY sent_at DESC", (since,))]
        try:
            albums = [dict(r) for r in c.execute(
                "SELECT * FROM album WHERE sent_at >= ? AND first_seen < sent_at ORDER BY sent_at DESC", (since,))]
        except sqlite3.OperationalError:
            albums = []
    seen = {}
    for r in rows:
        seen.setdefault((r["kind"], r["title"]), []).append(r)
    for (kind, title), v in seen.items():
        when = time.strftime("%d/%m %H:%M", time.localtime(max(x["sent_at"] for x in v)))
        lines.append(f"{'📺' if kind == 'ep' else '🎬'} {title}" + (f" ({len(v)} ep.)" if kind == "ep" else "") + f" — {when}")
    for a in albums:
        lines.append(f"🎵 {a['artist']} — {a['name']} — {time.strftime('%d/%m %H:%M', time.localtime(a['sent_at']))}")
    return "\n".join(lines) or f"Nada novo nos últimos {dias} dias."


TOOLS = {
    "status_filme": (status_filme, "Situação de um filme: se já está disponível, baixando (progresso), quando lança, legendas.",
                     {"titulo": {"type": "string", "description": "título do filme (pode ser em português ou o original)"}}, ["titulo"]),
    "status_serie": (status_serie, "Situação de uma série: episódios baixados por temporada, faltando, baixando, próximo episódio.",
                     {"titulo": {"type": "string"}, "temporada": {"type": "integer", "description": "opcional: detalhar só essa temporada"}},
                     ["titulo"]),
    "status_livro": (status_livro, "Situação de pedidos de livros/audiolivros no Shelfarr (por título ou autor).",
                     {"busca": {"type": "string"}}, ["busca"]),
    "status_musica": (status_musica, "Procura artista, álbum ou música na biblioteca do Navidrome.",
                      {"busca": {"type": "string"}}, ["busca"]),
    "fila_downloads": (fila_downloads, "Tudo que está baixando agora (filmes, séries, livros) e o que espera legenda.", {}, []),
    "recentes": (recentes, "O que ficou disponível nos últimos dias (filmes, episódios, álbuns).",
                 {"dias": {"type": "integer", "description": "padrão 3"}}, []),
}


def run_tool(name, args):
    fn = TOOLS[name][0]
    try:
        return fn(**{k: v for k, v in (args or {}).items() if k in TOOLS[name][2]})
    except Exception as e:
        log.warning("ferramenta %s: %s", name, e)
        return f"Erro ao consultar ({name}): {e}"


# ---------------------------------------------------------------- IA
SYSTEM = """Você é o assistente de status do homelab "iHome" (mídia da casa) e responde no Telegram ou no WhatsApp em português do Brasil.
Hoje é {hoje}. Use SEMPRE as ferramentas para responder; nunca invente status, datas ou progresso.
- "Disponível" = baixado, importado e (quando precisa) legendado em português; dá para assistir no Jellyfin.
- Se a ferramenta não achar o título, tente de novo com o título original (em inglês) antes de desistir.
- Se não estiver claro se é filme ou série, consulte os dois.
- Responda curto e direto (algumas linhas), pode usar emojis. Inclua o link quando a ferramenta der um.
- Texto simples, sem markdown (nada de **, #, tabelas)."""


def llm(question):
    tools = [{"type": "function", "function": {"name": n, "description": d,
                                                "parameters": {"type": "object", "properties": p, "required": r}}}
             for n, (_, d, p, r) in TOOLS.items()]
    msgs = [{"role": "system", "content": SYSTEM.format(hoje=time.strftime("%d/%m/%Y %H:%M"))},
            {"role": "user", "content": question}]
    for _ in range(6):
        res = http("POST", f"{LLM_URL}/chat/completions", {"Authorization": f"Bearer {LLM_KEY}"},
                   {"model": LLM_MODEL, "messages": msgs, "tools": tools, "temperature": 0.2}, timeout=90)
        m = res["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        if not calls:
            return (m.get("content") or "").strip()
        msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            out = run_tool(name, args) if name in TOOLS else f"ferramenta desconhecida: {name}"
            log.info("IA chamou %s(%s)", name, args)
            msgs.append({"role": "tool", "tool_call_id": c.get("id", name), "name": name, "content": out[:6000]})
    return ""


STOP = set("cade onde esta ta tah ja status situacao como anda andamento do da de dos das o a os as e "
           "um uma me diz fala ai qual quando chega vai chegar sair saiu baixou baixando tem temos".split())
KINDS = {"filme": "filme", "filmes": "filme", "serie": "serie", "series": "serie", "seriado": "serie",
         "temporada": "serie", "episodio": "serie", "livro": "livro", "livros": "livro", "audiolivro": "livro",
         "audiobook": "livro", "musica": "musica", "musicas": "musica", "album": "musica", "disco": "musica",
         "artista": "musica", "banda": "musica"}


def fallback(question):
    """Sem IA: entende o pedido por palavras-chave."""
    words = norm(question).split()
    if any(w in ("fila", "baixando", "downloads", "download") for w in words) and len(words) <= 4:
        return fila_downloads()
    if any(w in ("recentes", "novidades", "chegou", "novos") for w in words) and len(words) <= 4:
        return recentes()
    kind = next((KINDS[w] for w in words if w in KINDS), None)
    season = re.search(r"\b(?:temporada|t|season|s)\s*(\d{1,2})\b", norm(question))
    if season:
        words = re.sub(r"\b(?:temporada|t|season|s)\s*\d{1,2}\b", " ", norm(question)).split()
    while words and (words[0] in STOP or words[0] in KINDS):  # tira só o começo ("cadê o filme ...")
        words.pop(0)
    while words and (words[-1] in STOP or words[-1] in KINDS):
        words.pop()
    title = " ".join(words)
    if not title:
        return "Me diga o nome (ex.: \"cadê o filme Duna\", \"status da série Severance\") ou /fila."
    if kind == "filme":
        return status_filme(title)
    if kind == "serie":
        return status_serie(title, int(season.group(1)) if season else None)
    if kind == "livro":
        return status_livro(title)
    if kind == "musica":
        return status_musica(title)
    parts = []
    for fn in (status_filme, status_serie):
        r = run_tool(fn.__name__, {"titulo": title})
        if not r.startswith("Nenhum"):
            parts.append(r)
    return "\n\n".join(parts) or f"Não achei '{title}' como filme nem como série. Tente /livro ou /musica."


HELP = ("Pergunte o status de qualquer mídia, por exemplo:\n"
        "• cadê o filme Duna parte 2?\n• status da série Severance temporada 2\n• o livro Trono de Vidro já chegou?\n"
        "• tem o álbum Clube da Esquina?\n\nComandos: /fila (o que está baixando), /recentes (o que chegou), "
        "/filme, /serie, /livro, /musica seguidos do nome.\n"
        "Lançamentos das bandas favoritas: /seguindo (lista), /seguir <banda>, /naoseguir <banda>.\n\n"
        "Pedir filme ou série (WhatsApp, grupo Status): \"quero o filme Duna\", \"/pedir Severance temporada 2\" "
        "e responda com o número da opção.")
COMMANDS = {"/fila": lambda a: fila_downloads(), "/recentes": lambda a: recentes(int(a) if a.isdigit() else 3),
            "/filme": lambda a: status_filme(a) if a else "Uso: /filme <nome>",
            "/serie": lambda a: fallback("serie " + a) if a else "Uso: /serie <nome> [temporada N]",
            "/livro": lambda a: status_livro(a) if a else "Uso: /livro <nome ou autor>",
            "/musica": lambda a: status_musica(a) if a else "Uso: /musica <artista ou álbum>",
            "/seguir": lambda a: lancamentos.cmd_follow(a) if a else "Uso: /seguir <banda>",
            "/naoseguir": lambda a: lancamentos.cmd_unfollow(a) if a else "Uso: /naoseguir <banda>",
            "/seguindo": lambda a: lancamentos.cmd_list()}


def answer(text):
    text = text.strip()
    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/ajuda", "/help"):
            return HELP
        if cmd in COMMANDS:
            return COMMANDS[cmd](arg.strip())
        text = arg or cmd[1:]
    if LLM_URL and LLM_KEY and LLM_MODEL:
        try:
            out = llm(text)
            if out:
                return out
        except Exception as e:
            log.warning("IA falhou (%s), respondendo sem IA", e)
    return fallback(text)


# ---------------------------------------------------------------- Telegram
def reply(chat_id, msg_id, text, thread=None):
    for i in range(0, max(len(text), 1), 4000):
        body = {"chat_id": chat_id, "text": text[i:i + 4000] or "…",
                "reply_parameters": {"message_id": msg_id, "allow_sending_without_reply": True},
                "link_preview_options": {"is_disabled": True}}
        if thread:
            body["message_thread_id"] = int(thread)
        tg("sendMessage", body)


def maybe_adopt(chat):
    """Grupo com Tópicos onde o bot é admin vira a central (se ainda não há uma): cria os 5 tópicos."""
    if forum_id() or chat.get("type") != "supergroup" or not chat.get("is_forum"):
        return False
    try:
        me = tg("getChatMember", {"chat_id": chat["id"], "user_id": int(TG_TOKEN.split(":")[0])})["result"]
    except Exception as e:
        log.warning("getChatMember: %s", e)
        return False
    if me.get("status") != "administrator":
        log.info("grupo-fórum '%s' visto, mas o bot ainda não é admin", chat.get("title"))
        return False
    kv_set("forum_id", chat["id"])
    log.info("central adotada: grupo-fórum '%s' (id %s)", chat.get("title"), chat["id"])
    for kind in TOPICS:
        topic_id(kind)
    st = topic_id("status", create=False)
    if st:
        tg("sendMessage", {"chat_id": chat["id"], "message_thread_id": int(st), "text": "Central de status pronta. " + HELP})
    return True


def to_agent(u):
    """Repassa o update ao agente no pve. True = era para o agente (repassado ou avisado do erro)."""
    if not (AGENT_URL and AGENT_TOKEN):
        return False
    cq = u.get("callback_query")
    if cq:
        if not (cq.get("data") or "").startswith("ag:"):
            return False
    else:
        m = u.get("message") or {}
        if not (m.get("is_topic_message") and str(m.get("message_thread_id")) == AGENT_THREAD
                and str(m.get("chat", {}).get("id")) == str(forum_id())
                and str((m.get("from") or {}).get("id")) in AGENT_ADMINS):
            return False
    try:
        http("POST", f"{AGENT_URL}/telegram?token={urllib.parse.quote(AGENT_TOKEN)}", body=u, timeout=15)
    except Exception as e:
        log.warning("agente do homelab: %s", e)
        if cq:
            tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Agente fora do ar"})
        else:
            reply(m["chat"]["id"], m["message_id"], f"⚠️ O agente no pve não respondeu ({e}).", AGENT_THREAD)
    return True


def handle(u):
    if to_agent(u):
        return
    if "my_chat_member" in u:  # bot adicionado/removido de um canal ou grupo: guarda o id
        m = u["my_chat_member"]
        ch, st = m["chat"], m["new_chat_member"]["status"]
        with db() as c:
            c.execute("INSERT INTO chat VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET type=excluded.type,"
                      " title=excluded.title, status=excluded.status, at=excluded.at",
                      (ch["id"], ch.get("type"), ch.get("title") or ch.get("username"), st, time.time()))
        log.info("bot agora é '%s' em %s '%s' (id %s, fórum %s)", st, ch.get("type"), ch.get("title"), ch["id"],
                 ch.get("is_forum", False))
        maybe_adopt(ch)
        return
    m = u.get("channel_post") or u.get("message")
    if not m:
        return
    chat, frm = m["chat"], m.get("from") or {}
    cid = str(chat["id"])
    if m.get("migrate_to_chat_id"):  # grupo comum virou supergrupo (ao ligar Tópicos): id novo
        log.info("grupo '%s' migrou para o id %s", chat.get("title"), m["migrate_to_chat_id"])
        return
    if cid != forum_id() and chat.get("is_forum"):
        maybe_adopt(chat)
    text = m.get("text")
    if not text:
        return
    mentioned = text.startswith("/") or bool(BOT_NAME["username"] and f"@{BOT_NAME['username'].lower()}" in text.lower())
    thread = m.get("message_thread_id") if m.get("is_topic_message") else None
    if "channel_post" in u:
        if cid != CHANNELS["status"]:
            return
    elif chat["type"] == "private":
        if str(frm.get("id")) not in ALLOWED:
            log.info("mensagem privada de usuário não autorizado %s (%s)", frm.get("id"), frm.get("first_name"))
            reply(chat["id"], m["message_id"], f"Não autorizado. Peça para incluir seu id {frm.get('id')}.")
            return
    elif cid == forum_id():
        # no tópico Status responde tudo; nos outros tópicos só comando ou menção
        if str(thread or "") != str(topic_id("status", create=False)) and not mentioned:
            return
    elif cid not in (TG_GROUP, CHANNELS["status"]):
        return
    if BOT_NAME["username"]:
        text = re.sub(rf"@{BOT_NAME['username']}\b", "", text, flags=re.I).strip()
    if not text:
        return
    log.info("pergunta (%s): %s", chat.get("title") or frm.get("first_name"), text)
    try:
        tg("sendChatAction", {"chat_id": chat["id"], "action": "typing", **({"message_thread_id": thread} if thread else {})})
    except Exception:
        pass
    try:
        out = answer(text)
    except Exception as e:
        log.exception("status: %s", e)
        out = f"Deu erro ao consultar: {e}"
    reply(chat["id"], m["message_id"], out, thread)


def worker():
    if not TG_TOKEN:
        return
    init_db()
    try:
        BOT_NAME["username"] = tg("getMe")["result"]["username"]
    except Exception as e:
        log.warning("getMe: %s", e)
    log.info("status: ouvindo o Telegram (IA %s, central %s)", LLM_MODEL if LLM_URL and LLM_KEY else "desligada",
             f"grupo-fórum {forum_id()}" if forum_id() else "ainda sem grupo-fórum")
    offset = int(kv_get("tg_offset", "0"))
    while True:
        try:
            res = tg("getUpdates", {"offset": offset, "timeout": 50,
                                    "allowed_updates": ["message", "channel_post", "my_chat_member", "callback_query"]}, timeout=70)
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                kv_set("tg_offset", offset)
                threading.Thread(target=handle, args=(u,), daemon=True).start()
        except Exception as e:
            log.warning("getUpdates: %s", e)
            time.sleep(10)
