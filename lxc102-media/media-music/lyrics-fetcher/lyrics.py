#!/usr/bin/env python3
"""Baixa letras da LRCLIB (lrclib.net) para toda a biblioteca, sem tocar nos arquivos de áudio.

- Sincronizada → "<faixa>.lrc"; só texto → "<faixa>.txt" (Navidrome: ND_LYRICSPRIORITY com .txt; Jellyfin lê os dois).
- Letra embutida no arquivo (LYRICS / USLT) é exportada para o sidecar sem consultar a internet.
- Faixas sem letra ficam em STATE_FILE e só são tentadas de novo depois de RETRY_DAYS; instrumentais nunca.

Variáveis: MUSIC_DIR (/music), STATE_FILE (/state/lyrics-state.json), RETRY_DAYS (30), WORKERS (2), MIN_INTERVAL (1.0 s, sobe sozinho com 429),
           DRY_RUN (0), LIMIT (0 = todas).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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

lock = Lock()
rate_lock = Lock()
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
        "title": first(easy.tags, "title"),
        "album": first(easy.tags, "album"),
        "duration": float(getattr(easy.info, "length", 0) or 0),
        "embedded": "",
    }
    raw = mutagen.File(path)
    tags = getattr(raw, "tags", None)
    if tags:
        if hasattr(tags, "getall"):  # ID3
            for frame in tags.getall("USLT"):
                if str(frame.text).strip():
                    meta["embedded"] = str(frame.text)
                    break
        elif path.lower().endswith(".m4a"):
            meta["embedded"] = first(tags, "\xa9lyr")
        else:  # Vorbis (chaves sem diferença de maiúsculas)
            meta["embedded"] = first(tags, "lyrics", "unsyncedlyrics")
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


def has_lyrics(rec):
    return bool(rec and (rec.get("syncedLyrics") or rec.get("plainLyrics")))


def lookup(meta):
    params = {"artist_name": meta["artist"], "track_name": meta["title"], "duration": round(meta["duration"])}
    if meta["album"]:
        params["album_name"] = meta["album"]
    rec = http_json("get", params)
    if rec and (has_lyrics(rec) or rec.get("instrumental")):
        return rec
    # sem casar o álbum: busca por artista + título e fica com a duração mais próxima (±2 s)
    results = http_json("search", {"artist_name": meta["artist"], "track_name": meta["title"]}) or []
    near = [r for r in results if abs((r.get("duration") or 0) - meta["duration"]) <= DURATION_TOLERANCE
            and (has_lyrics(r) or r.get("instrumental"))]
    near.sort(key=lambda r: (not r.get("syncedLyrics"), abs(r["duration"] - meta["duration"])))
    return near[0] if near else None


def is_synced(text):
    return any(line.startswith("[") and line[1:3].isdigit() for line in text.splitlines()[:30])


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


def _process(path, state):
    base = os.path.splitext(path)[0]
    if os.path.exists(base + ".lrc") or os.path.exists(base + ".txt"):
        bump("já tinha")
        return
    prev = state.get(path)
    if prev and (prev["status"] == "instrumental" or time.time() - prev["ts"] < RETRY_DAYS * 86400):
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
        ext = ".lrc" if is_synced(meta["embedded"]) else ".txt"
        if write(base + ext, meta["embedded"]):
            bump("exportada da tag")
        return
    try:
        rec = lookup(meta)
    except Exception as e:
        log(f"ERRO LRCLIB {meta['artist']} - {meta['title']}: {e}")
        bump("erro")
        return
    with lock:
        if rec and rec.get("instrumental"):
            state[path] = {"status": "instrumental", "ts": time.time()}
        elif not has_lyrics(rec):
            state[path] = {"status": "notfound", "ts": time.time()}
        else:
            state.pop(path, None)
    if rec and rec.get("instrumental"):
        bump("instrumental")
    elif not has_lyrics(rec):
        bump("não encontrada")
    elif rec.get("syncedLyrics"):
        if write(base + ".lrc", rec["syncedLyrics"]):
            bump("sincronizada (.lrc)")
    elif write(base + ".txt", rec["plainLyrics"]):
        bump("só texto (.txt)")


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
