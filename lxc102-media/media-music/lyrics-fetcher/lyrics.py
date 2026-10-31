#!/usr/bin/env python3
"""Baixa letras da LRCLIB (lrclib.net) para toda a biblioteca, sem tocar nos arquivos de áudio.
Com DEEZER_ARL definido, o que a LRCLIB não tem é buscado no Deezer (pipe.deezer.com, mesma conta do plugin deezer).
Com GENIUS_TOKEN definido, o que nenhum dos dois tem sai do Genius (só texto: busca pela API, letra lida da página).

- Sincronizada → "<faixa>.lrc"; só texto → "<faixa>.txt" (Navidrome: ND_LYRICSPRIORITY com .txt; Jellyfin lê os dois).
- Faixa com letra embutida (gravada pelo library management do DroppedNeedle: LYRICS/SYNCEDLYRICS/USLT/SYLT)
  é pulada; o script só cobre o que o DroppedNeedle não achou.
- Faixas sem letra ficam em STATE_FILE e só são tentadas de novo depois de RETRY_DAYS; instrumentais nunca.
  Faixa "notfound" que ainda não passou pelo Deezer/Genius é tentada de novo na hora, só na fonte que faltou.
- Faixa que só tem .txt é conferida de novo a cada RETRY_DAYS atrás de versão sincronizada (LRCLIB e Deezer);
  achando, grava o .lrc e apaga o .txt.

Variáveis: MUSIC_DIR (/music), STATE_FILE (/state/lyrics-state.json), RETRY_DAYS (30), WORKERS (2), MIN_INTERVAL (1.0 s, sobe sozinho com 429),
           DRY_RUN (0), LIMIT (0 = todas), DEEZER_ARL (vazio = sem Deezer), GENIUS_TOKEN (vazio = sem Genius).
"""
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from threading import Lock

import mutagen

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")
STATE_FILE = os.environ.get("STATE_FILE", "/state/lyrics-state.json")
RETRY_DAYS = float(os.environ.get("RETRY_DAYS", "30"))
WORKERS = int(os.environ.get("WORKERS", "2"))
# A LRCLIB devolve 429 de forma intermitente (servidor cheio, não só por IP): repete após o Retry-After
# e só alarga um pouco o intervalo enquanto os 429 persistem.
MIN_INTERVAL = float(os.environ.get("MIN_INTERVAL", "1.0"))
MAX_INTERVAL = 4.0
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
LIMIT = int(os.environ.get("LIMIT", "0"))
AUDIO_EXT = {".flac", ".mp3", ".m4a", ".ogg", ".opus"}
UA = "homelab-lyrics-fetcher/1.0 (https://github.com/ianharbs/homelab)"
API = "https://lrclib.net/api"
DURATION_TOLERANCE = 2.0
DEEZER_ARL = os.environ.get("DEEZER_ARL", "").strip("\"'")
DEEZER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
DEEZER_TOLERANCE = 3.0  # a API do Deezer arredonda a duração para segundos
GENIUS_TOKEN = os.environ.get("GENIUS_TOKEN", "").strip("\"'")

lock = Lock()
rate_lock = Lock()
deezer_lock = Lock()
deezer_jwt = {"token": "", "at": 0.0}
deezer_next = 0.0
genius_lock = Lock()
genius_next = 0.0
next_call = 0.0
interval = MIN_INTERVAL
stats = {}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def bump(key):
    with lock:
        stats[key] = stats.get(key, 0) + 1


def first(tags, *keys):
    for k in keys:
        v = tags.get(k) if tags else None
        if v:
            v = v[0] if isinstance(v, list) else v
            v = str(v).strip()
            if v:
                return v
    return ""


def read_track(path):
    easy = mutagen.File(path, easy=True)
    if easy is None or easy.info is None:
        return None
    meta = {
        "artist": first(easy.tags, "artist", "albumartist"),
        "isrc": first(easy.tags, "isrc").upper(),
        "title": first(easy.tags, "title"),
        "album": first(easy.tags, "album"),
        "duration": float(getattr(easy.info, "length", 0) or 0),
        "embedded": "",
    }
    raw = mutagen.File(path)
    tags = getattr(raw, "tags", None)
    if tags:
        if hasattr(tags, "getall"):  # ID3
            if tags.getall("SYLT"):
                meta["embedded"] = "synced"
            for frame in tags.getall("USLT"):
                if str(frame.text).strip():
                    meta["embedded"] = str(frame.text)
                    break
        elif path.lower().endswith(".m4a"):
            meta["embedded"] = first(tags, "\xa9lyr")
        else:  # Vorbis (chaves sem diferença de maiúsculas)
            meta["embedded"] = first(tags, "lyrics", "unsyncedlyrics", "syncedlyrics")
    return meta


def adapt(limited):
    global interval
    with rate_lock:
        interval = min(MAX_INTERVAL, interval + 0.25) if limited else max(MIN_INTERVAL, interval - 0.1)


def throttle(delay=0.0):
    global next_call
    with rate_lock:
        now = time.monotonic()
        next_call = max(next_call, now + delay)
        wait = next_call - now
        next_call += interval
    if wait > 0:
        time.sleep(wait)


def http_json(path, params):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    delay = 0.0
    for attempt in range(20):
        throttle(delay)
        delay = 0.0
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                adapt(False)
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                adapt(False)
                return None
            if e.code == 429:
                delay = float(e.headers.get("Retry-After") or 1) + 0.5
                adapt(True)
                bump("429")
                continue
            if e.code in (500, 502, 503, 504):
                delay = 5.0 * (attempt + 1)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            delay = 5.0 * (attempt + 1)
    raise RuntimeError(f"LRCLIB sem resposta: {path}")


def norm(text):
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\s[-–]\s.*$|[(\[].*?[)\]]|\bfeat\.?\s.*$", " ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def deezer_http(url, body=None, headers=None):
    global deezer_next
    with deezer_lock:  # API pública: 50 pedidos / 5 s por IP; aqui bem abaixo disso
        wait = deezer_next - time.monotonic()
        deezer_next = max(deezer_next, time.monotonic()) + 0.25
    if wait > 0:
        time.sleep(wait)
    h = {"User-Agent": DEEZER_UA}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        h["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=30) as r:
        return json.load(r)


def deezer_token():
    with lock:
        if deezer_jwt["token"] and time.time() - deezer_jwt["at"] < 1800:
            return deezer_jwt["token"]
    got = deezer_http("https://auth.deezer.com/login/arl?jo=p&rto=c&i=c", {}, {"Cookie": f"arl={DEEZER_ARL}"})
    with lock:
        deezer_jwt.update(token=got["jwt"], at=time.time())
    return got["jwt"]


def deezer_track(meta):
    """Acha a faixa no Deezer pelo ISRC da tag; sem ISRC, por busca com artista, título e duração batendo."""
    if meta["isrc"]:
        try:
            t = deezer_http(f"https://api.deezer.com/track/isrc:{urllib.parse.quote(meta['isrc'])}")
            if t.get("id") and abs((t.get("duration") or 0) - meta["duration"]) <= DEEZER_TOLERANCE:
                return t["id"]
        except urllib.error.HTTPError:
            pass
    q = urllib.parse.urlencode({"q": f"{meta['artist']} {meta['title']}", "limit": 25})
    found = deezer_http(f"https://api.deezer.com/search/track?{q}").get("data") or []
    artist, title = norm(meta["artist"]), norm(meta["title"])
    for t in found:
        a = norm((t.get("artist") or {}).get("name", ""))
        if (norm(t.get("title", "")) == title and a and (a in artist or artist in a)
                and abs((t.get("duration") or 0) - meta["duration"]) <= DEEZER_TOLERANCE):
            return t["id"]
    return None


def deezer_lookup(meta):
    """Devolve um registro no formato da LRCLIB (syncedLyrics/plainLyrics) ou None."""
    sng_id = deezer_track(meta)
    if not sng_id:
        return None
    query = ("query SynchronizedTrackLyrics($trackId: String!) { track(trackId: $trackId) "
             "{ lyrics { text synchronizedLines { lrcTimestamp line } } } }")
    for attempt in range(2):
        try:
            got = deezer_http("https://pipe.deezer.com/api",
                              {"query": query, "variables": {"trackId": str(sng_id)}},
                              {"Authorization": f"Bearer {deezer_token()}"})
            break
        except urllib.error.HTTPError as e:
            if e.code != 401 or attempt:
                raise
            with lock:
                deezer_jwt["token"] = ""
    lyrics = (((got.get("data") or {}).get("track") or {}).get("lyrics")) or {}
    lines = lyrics.get("synchronizedLines") or []
    synced = "\n".join(f"{l['lrcTimestamp']} {l['line']}" for l in lines if l.get("lrcTimestamp"))
    plain = (lyrics.get("text") or "").replace("\r\n", "\n")
    if not synced and not plain.strip():
        return None
    return {"syncedLyrics": synced, "plainLyrics": plain}


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class GeniusLyrics(HTMLParser):
    """Texto dos <div data-lyrics-container="true"> da página do Genius, sem os blocos marcados para não copiar."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0  # profundidade dentro de um container de letra (0 = fora)
        self.skip = 0  # profundidade dentro de um bloco data-exclude-from-selection
        self.parts = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "br":
            if self.depth and not self.skip:
                self.parts.append("\n")
            return
        if tag in VOID_TAGS:  # sem tag de fechamento: não mexe na profundidade
            return
        if self.depth:
            self.depth += 1
            if self.skip or a.get("data-exclude-from-selection") == "true":
                self.skip += 1
        elif tag == "div" and a.get("data-lyrics-container") == "true":
            self.depth = 1
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.depth and tag not in VOID_TAGS:
            self.depth -= 1
            if self.skip:
                self.skip -= 1

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.parts.append(data)


def genius_http(url, api=True):
    global genius_next
    for attempt in range(4):
        with genius_lock:  # o Genius não publica limite; um pedido por segundo
            wait = genius_next - time.monotonic()
            genius_next = max(genius_next, time.monotonic()) + 1.0
        if wait > 0:
            time.sleep(wait)
        h = {"User-Agent": DEEZER_UA}
        if api:
            h["Authorization"] = f"Bearer {GENIUS_TOKEN}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
            return json.loads(body) if api else body
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
            time.sleep(5.0 * (attempt + 1))


def genius_lookup(meta):
    """Só texto: acha a música pela API (artista e título batendo) e lê a letra da página."""
    q = urllib.parse.urlencode({"q": f"{meta['artist']} {meta['title']}"})
    hits = ((genius_http(f"https://api.genius.com/search?{q}").get("response") or {}).get("hits")) or []
    artist, title = norm(meta["artist"]), norm(meta["title"])
    for h in hits:
        s = h.get("result") or {}
        names = [norm((s.get("primary_artist") or {}).get("name", "")), norm(s.get("artist_names", ""))]
        if (h.get("type") == "song" and s.get("lyrics_state") == "complete" and not s.get("instrumental")
                and norm(s.get("title", "")) == title and any(a and (a in artist or artist in a) for a in names)):
            break
    else:
        return None
    parser = GeniusLyrics()
    parser.feed(genius_http(s["url"], api=False))
    lines = [l.strip() for l in html.unescape("".join(parser.parts)).split("\n")]
    # tira os cabeçalhos de seção ([Refrão], [Verso 1: ...]) e junta linhas em branco seguidas
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(l for l in lines if not re.fullmatch(r"\[.*\]", l))).strip()
    return {"syncedLyrics": "", "plainLyrics": text} if text else None


def has_lyrics(rec):
    return bool(rec and (rec.get("syncedLyrics") or rec.get("plainLyrics")))


def lookup(meta):
    params = {"artist_name": meta["artist"], "track_name": meta["title"], "duration": round(meta["duration"])}
    if meta["album"]:
        params["album_name"] = meta["album"]
    rec = http_json("get", params)
    if rec and (rec.get("syncedLyrics") or rec.get("instrumental")):
        return rec
    # o "get" devolve um único registro, às vezes só texto, mesmo havendo outro sincronizado da mesma faixa:
    # busca por artista + título e prefere sincronizada com a duração mais próxima (±2 s)
    results = http_json("search", {"artist_name": meta["artist"], "track_name": meta["title"]}) or []
    near = [r for r in results if abs((r.get("duration") or 0) - meta["duration"]) <= DURATION_TOLERANCE
            and (has_lyrics(r) or r.get("instrumental"))]
    near.sort(key=lambda r: (not r.get("syncedLyrics"), abs(r["duration"] - meta["duration"])))
    best = near[0] if near else None
    if best and best.get("syncedLyrics"):
        return best
    return rec if has_lyrics(rec) else best


def write(path, text):
    if DRY_RUN:
        return True
    tmp = path + ".part"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
        os.replace(tmp, path)
        return True
    except OSError as e:
        log(f"ERRO gravando {path}: {e}")
        bump("erro de escrita")
        return False


def process(path, state):
    try:
        _process(path, state)
    except Exception as e:  # uma faixa problemática não derruba a passada
        log(f"ERRO {path}: {e!r}")
        bump("erro")


def upgrade_plain(path, base, state):
    """Faixa só com .txt: procura versão sincronizada; achando, troca o .txt pelo .lrc."""
    prev = state.get(path)
    if prev and prev["status"] == "plain" and time.time() - prev["ts"] < RETRY_DAYS * 86400:
        bump("só texto (conferida antes)")
        return
    try:
        meta = read_track(path)
    except Exception as e:
        log(f"ERRO lendo {path}: {e}")
        bump("erro")
        return
    if not meta or not meta["artist"] or not meta["title"] or meta["duration"] <= 0:
        bump("sem tags")
        return
    try:
        rec = lookup(meta)
        synced = (rec or {}).get("syncedLyrics") or ""
        if not synced and DEEZER_ARL:
            synced = (deezer_lookup(meta) or {}).get("syncedLyrics") or ""
    except Exception as e:
        log(f"ERRO procurando sincronizada {meta['artist']} - {meta['title']}: {e}")
        bump("erro")
        return
    if not synced:
        with lock:
            state[path] = {"status": "plain", "ts": time.time()}
        bump("só texto (sem sincronizada)")
        return
    if write(base + ".lrc", synced):
        if not DRY_RUN:
            os.remove(base + ".txt")
        with lock:
            state.pop(path, None)
        bump("texto → sincronizada (.lrc)")


def _process(path, state):
    base = os.path.splitext(path)[0]
    if os.path.exists(base + ".lrc"):
        bump("já tinha")
        return
    if os.path.exists(base + ".txt"):
        upgrade_plain(path, base, state)
        return
    prev = state.get(path)
    retry = bool(prev and prev["status"] == "notfound" and time.time() - prev["ts"] < RETRY_DAYS * 86400)
    deezer_pending = bool(DEEZER_ARL and prev and prev["status"] == "notfound" and not prev.get("deezer"))
    genius_pending = bool(GENIUS_TOKEN and prev and prev["status"] == "notfound" and not prev.get("genius"))
    if prev and not (deezer_pending or genius_pending) and (prev["status"] == "instrumental" or retry):
        bump("pulada (sem letra antes)")
        return
    try:
        meta = read_track(path)
    except Exception as e:  # arquivo corrompido ou formato estranho
        log(f"ERRO lendo {path}: {e}")
        bump("erro")
        return
    if not meta or not meta["artist"] or not meta["title"] or meta["duration"] <= 0:
        bump("sem tags")
        return
    if meta["embedded"]:
        bump("já tem na tag")
        return
    try:
        # fonte nova ligada: só ela é consultada; o resto espera o RETRY_DAYS normal
        rec = None if retry else lookup(meta)
    except Exception as e:
        log(f"ERRO LRCLIB {meta['artist']} - {meta['title']}: {e}")
        bump("erro")
        return
    source = ""
    if DEEZER_ARL and (deezer_pending or not retry) and not has_lyrics(rec) and not (rec and rec.get("instrumental")):
        try:
            rec = deezer_lookup(meta)
            source = " Deezer" if has_lyrics(rec) else ""
        except Exception as e:
            log(f"ERRO Deezer {meta['artist']} - {meta['title']}: {e}")
            bump("erro")
            return
    if GENIUS_TOKEN and (genius_pending or not retry) and not has_lyrics(rec) and not (rec and rec.get("instrumental")):
        try:
            rec = genius_lookup(meta)
            source = " Genius" if has_lyrics(rec) else ""
        except Exception as e:
            log(f"ERRO Genius {meta['artist']} - {meta['title']}: {e}")
            bump("erro")
            return
    with lock:
        if rec and rec.get("instrumental"):
            state[path] = {"status": "instrumental", "ts": time.time()}
        elif not has_lyrics(rec):
            # um retry só de fonte nova mantém o ts antigo: as outras fontes continuam no prazo de RETRY_DAYS
            ts = prev["ts"] if retry else time.time()
            state[path] = {"status": "notfound", "ts": ts, "deezer": bool(DEEZER_ARL), "genius": bool(GENIUS_TOKEN)}
        else:
            state.pop(path, None)
    if rec and rec.get("instrumental"):
        bump("instrumental")
    elif not has_lyrics(rec):
        bump("não encontrada")
    elif rec.get("syncedLyrics"):
        if write(base + ".lrc", rec["syncedLyrics"]):
            bump("sincronizada (.lrc)" + source)
    elif write(base + ".txt", rec["plainLyrics"]):
        bump("só texto (.txt)" + source)


def save_state(state):
    if DRY_RUN:
        return
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with lock:
        data = json.dumps(state)
    with open(STATE_FILE + ".part", "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(STATE_FILE + ".part", STATE_FILE)


def main():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    files = []
    for root, dirs, names in os.walk(MUSIC_DIR):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        files += [os.path.join(root, n) for n in sorted(names) if os.path.splitext(n)[1].lower() in AUDIO_EXT]
    if LIMIT:
        files = files[:LIMIT]
    log(f"{len(files)} faixas em {MUSIC_DIR}{' (DRY_RUN)' if DRY_RUN else ''}")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, _ in enumerate(pool.map(lambda p: process(p, state), files), 1):
            if i % 250 == 0:
                log(f"{i}/{len(files)} intervalo={interval:.1f}s {stats}")
                save_state(state)
    save_state(state)
    log(f"fim em {time.time() - t0:.0f}s: {stats}")


if __name__ == "__main__":
    sys.exit(main())
