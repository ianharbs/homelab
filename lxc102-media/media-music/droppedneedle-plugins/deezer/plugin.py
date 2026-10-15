"""DroppedNeedle plugin: Deezer as indexer + download client, without a proxy.

Search is MusicBrainz-guided. DroppedNeedle only hands a plugin artist, album,
year and track count, so the plugin resolves the target edition itself (same
``select_edition`` policy the host uses) and assembles exactly that tracklist
from Deezer: tracks are matched by ISRC first, then by title + duration, and a
track missing from the main Deezer album is filled from other Deezer albums
(compilations, remasters) by ISRC or track search. That avoids the partial
imports a "whole Deezer album" download causes when Deezer only carries a
different edition. Without a MusicBrainz match it falls back to plain Deezer
album matching.

Download uses the private gateway (ARL cookie): ``song.getListData`` gives the
track token and per-format sizes (so quality and size are known at search
time), ``media.deezer.com/v1/get_url`` gives the stream, which is decrypted
(Blowfish CBC on every third 2048-byte block) and tagged with MusicBrainz ids.
Files land in a per-task staging folder that DroppedNeedle imports and renames.
"""

import asyncio
import difflib
import hashlib
import json
import re
import shutil
import time
import unicodedata
from pathlib import Path

from models.common import ServiceStatus
from repositories.protocols.download_client import (
    DownloadMaterialization,
    DownloadTaskStatus,
    EnqueueRequest,
    MountDiagnosis,
    TaskHandle,
)
from repositories.protocols.indexer import IndexerResult, PluginSearchResult

SOURCE_KEY = "plugin:deezer"
DEFAULT_DOWNLOAD_DIR = "/data/downloads/deezer"
DEFAULT_MB_URL = "https://musicbrainz.org/ws/2"
GW_URL = "https://www.deezer.com/ajax/gw-light.php"
API_URL = "https://api.deezer.com"
MEDIA_URL = "https://media.deezer.com/v1/get_url"
COVER_URL = "https://e-cdns-images.dzcdn.net/images/cover/{}/1000x1000-000000-80-0-0.jpg"
BF_SECRET = b"g4el58wc0zvf9na1"
STATE_FILE = ".deezer-job.json"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

MAX_ALBUM_CANDIDATES = 5
MAX_FILL_LOOKUPS = 40
MIN_ALBUM_SCORE = 0.45
MIN_RELEASE_SCORE = 0.50  # DroppedNeedle's manual-review floor; below it is a guaranteed reject
SCORE_FULL = 0.95  # whole MB tracklist from one Deezer album
SCORE_ASSEMBLED = 0.90  # whole tracklist, some tracks from other Deezer albums
SCORE_PARTIAL = 0.72  # >= 75 % of the tracks: auto-accepted, ranked below complete releases
SCORE_FRAGMENT = 0.60  # fewer: manual review only
TRACK_RETRIES = 2
CACHE_TTL = 1800.0
SESSION_TTL = 1800.0

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_DASH_SUFFIX = re.compile(r"\s+-\s+.*$")
_FORMAT_KEY = {"FLAC": "FILESIZE_FLAC", "MP3_320": "FILESIZE_MP3_320"}
_EXT = {"FLAC": "flac", "MP3_320": "mp3"}


def _safe(value: str) -> str:
    return _UNSAFE.sub("_", str(value or "")).strip(" .") or "_"


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).casefold()
    value = value.replace("&", " and ")
    return " ".join(re.sub(r"[\W_]+", " ", value).split())


def _bare(value: str) -> str:
    """Title without "(Remastered 2016)", "[Ao Vivo]" or " - Live" style suffixes."""
    return _norm(_DASH_SUFFIX.sub("", _BRACKETS.sub("", value or ""))) or _norm(value)


def _same_artist(wanted: str, candidate: str) -> bool:
    """Covers, tributes and karaoke reuse the title; the artist must match too
    (either name containing the other covers "X feat. Y" and "The X" variants)."""
    a, b = _norm(wanted), _norm(candidate)
    return bool(a and b) and (a in b or b in a)


def _album_match(artist: str, album: str, candidate_title: str) -> float:
    try:
        from services.native.acquisition.scoring_core import album_match
    except ImportError:
        return 1.0 if album.lower() in candidate_title.lower() else 0.0
    return float(album_match(artist, album, candidate_title))


def _select_edition(releases: list[dict], track_count: int) -> str | None:
    try:
        from repositories.musicbrainz_base import select_edition
    except ImportError:
        ranked = sorted(
            (abs(sum(int(m.get("track-count") or 0) for m in r.get("media") or []) - track_count), r["id"])
            for r in releases
            if r.get("id")
        )
        return ranked[0][1] if ranked else None
    return select_edition(releases, track_count)


def _credit(credits: list[dict]) -> str:
    return "".join(f"{c.get('name') or (c.get('artist') or {}).get('name', '')}{c.get('joinphrase', '')}" for c in credits or [])


def _title_score(mb_title: str, dz_title: str, dz_version: str = "") -> float:
    full = f"{dz_title} {dz_version}".strip()
    mb, dz = _norm(mb_title), _norm(full)
    if mb in (_norm(dz_title), dz):
        return 1.0
    if _bare(mb_title) == _bare(dz_title):
        return 0.95
    shorter, longer = sorted((mb, dz), key=len)
    if len(shorter) >= 8 and f" {shorter} " in f" {longer} ":
        return 0.9  # "Quinhentos anos de quê?" vs "1992 (Quinhentos Anos de Que?)"
    return difflib.SequenceMatcher(None, _bare(mb_title), _bare(dz_title)).ratio()


def _blowfish_key(sng_id: str) -> bytes:
    digest = hashlib.md5(str(sng_id).encode()).hexdigest().encode()  # noqa: S324 - Deezer's scheme
    return bytes(digest[i] ^ digest[i + 16] ^ BF_SECRET[i] for i in range(16))


class _Throttle:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


class _Job:
    """One enqueued task: its staging folder, per-track state and worker."""

    def __init__(self, key: str, directory: Path, files: list[dict]) -> None:
        self.key = key
        self.directory = directory
        self.files = files  # {filename, id, fmt, tags, local, state, bytes, error}
        self.worker: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.worker is not None and not self.worker.done()

    def save(self) -> None:
        path = self.directory / STATE_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": self.key, "files": self.files}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, directory: Path) -> "_Job | None":
        try:
            data = json.loads((directory / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return cls(str(data.get("key") or directory.name), directory, list(data.get("files") or []))


class Deezer:
    def __init__(self, context) -> None:  # noqa: ANN001 - PluginContext
        self._ctx = context
        self._http = context.http
        self._log = context.logger
        self._jobs: dict[str, _Job] = {}
        self._sem: asyncio.Semaphore | None = None
        self._sem_size = 0
        self._session: dict | None = None
        self._session_lock: asyncio.Lock | None = None
        self._api_throttle: _Throttle | None = None
        self._mb_throttle: _Throttle | None = None
        self._cache: dict[tuple, tuple[float, object]] = {}

    # -- settings --------------------------------------------------------------

    def _setting(self, key: str, default: str = "") -> str:
        return str(self._ctx.settings.get(key) or default).strip()

    @property
    def _arl(self) -> str:
        return self._setting("arl").strip("\"'")

    @property
    def _download_dir(self) -> Path:
        return Path(self._setting("download_dir", DEFAULT_DOWNLOAD_DIR))

    @property
    def _formats(self) -> list[str]:
        if self._setting("quality", "flac").lower().startswith("mp3"):
            return ["MP3_320"]
        fallback = self._setting("allow_fallback", "não").lower() in ("sim", "s", "yes", "y", "true", "1")
        return ["FLAC", "MP3_320"] if fallback else ["FLAC"]

    @property
    def _mb_url(self) -> str:
        return self._setting("musicbrainz_url", DEFAULT_MB_URL).rstrip("/")

    def _semaphore(self) -> asyncio.Semaphore:
        try:
            size = max(1, min(6, int(self._setting("max_parallel", "3"))))
        except ValueError:
            size = 3
        if self._sem is None or size != self._sem_size:
            self._sem, self._sem_size = asyncio.Semaphore(size), size
        return self._sem

    def _cached(self, key: tuple) -> object | None:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        return None

    def _store(self, key: tuple, value: object) -> object:
        if len(self._cache) > 500:
            self._cache.clear()
        self._cache[key] = (time.monotonic(), value)
        return value

    # -- deezer gateway (ARL) ------------------------------------------------

    def _cookie(self, sid: str = "") -> dict[str, str]:
        cookie = f"arl={self._arl}" + (f"; sid={sid}" if sid else "")
        return {"Cookie": cookie, "User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"}

    async def _gw_raw(self, method: str, body: dict | None, token: str, sid: str) -> tuple[dict, str]:
        resp = await self._http.post(
            GW_URL,
            params={"method": method, "input": "3", "api_version": "1.0", "api_token": token},
            json=body or {},
            headers=self._cookie(sid),
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json(), resp.cookies.get("sid") or sid

    async def _login(self, force: bool = False) -> dict:
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if not force and self._session and time.monotonic() - self._session["at"] < SESSION_TTL:
                return self._session
            data, sid = await self._gw_raw("deezer.getUserData", None, "", "")
            results = data.get("results") or {}
            user = results.get("USER") or {}
            if not user.get("USER_ID"):
                self._session = None
                raise RuntimeError("ARL inválido ou expirado (Deezer respondeu como visitante)")
            options = user.get("OPTIONS") or {}
            self._session = {
                "at": time.monotonic(),
                "sid": sid,
                "token": results.get("checkForm", ""),
                "license": options.get("license_token", ""),
                "lossless": bool(options.get("web_lossless") or options.get("mobile_lossless")),
                "offer": results.get("OFFER_NAME") or "",
                "country": results.get("COUNTRY") or "",
            }
            return self._session

    async def _gw(self, method: str, body: dict | None = None) -> dict:
        for attempt in range(2):
            session = await self._login(force=attempt > 0)
            data, _ = await self._gw_raw(method, body, session["token"], session["sid"])
            error = data.get("error")
            if error and any(k in json.dumps(error) for k in ("VALID_TOKEN_REQUIRED", "invalid api token", "NEED_USER_AUTH")):
                continue
            if error and not data.get("results"):
                raise RuntimeError(f"{method}: {error}")
            return data.get("results") or {}
        raise RuntimeError(f"{method}: sessão do Deezer recusada")

    async def _api(self, path: str, **params: object) -> dict:
        """Public API (search, UPC/ISRC lookups) - 50 requests / 5 s per IP."""
        if self._api_throttle is None:
            self._api_throttle = _Throttle(0.12)
        for attempt in range(3):
            await self._api_throttle.wait()
            resp = await self._http.get(f"{API_URL}/{path}", params=params or None, headers={"User-Agent": UA}, timeout=20.0)
            resp.raise_for_status()
            data = resp.json()
            error = data.get("error") if isinstance(data, dict) else None
            if not error:
                return data
            if error.get("code") == 4:  # quota
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if error.get("code") == 800:  # no data (unknown UPC/ISRC)
                return {}
            raise RuntimeError(f"api.deezer.com/{path}: {error.get('message')}")
        return {}

    async def _album_tracks(self, album_id: str) -> list[dict]:
        key = ("album", str(album_id))
        cached = self._cached(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        results = await self._gw("song.getListByAlbum", {"alb_id": str(album_id), "nb": 500, "start": 0})
        return self._store(key, list(results.get("data") or []))  # type: ignore[return-value]

    async def _songs(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for start in range(0, len(ids), 100):
            results = await self._gw("song.getListData", {"sng_ids": [str(i) for i in ids[start:start + 100]]})
            out.extend(results.get("data") or [])
        return out

    def _pick_format(self, song: dict) -> str | None:
        for fmt in self._formats:
            if int(song.get(_FORMAT_KEY[fmt]) or 0) > 0:
                return fmt
        return None

    # -- musicbrainz target ----------------------------------------------------

    async def _mb(self, path: str, **params: object) -> dict:
        if self._mb_throttle is None:
            self._mb_throttle = _Throttle(1.05 if "musicbrainz.org" in self._mb_url else 0.15)
        for attempt in range(3):
            await self._mb_throttle.wait()
            resp = await self._http.get(f"{self._mb_url}/{path}", params={**params, "fmt": "json"}, timeout=20.0)
            if resp.status_code in (429, 503):
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"MusicBrainz {path}: limite de requisições")

    async def _target(self, artist: str, album: str, track_count: int | None) -> dict | None:
        """The MB edition DroppedNeedle will import against: release group by
        title + artist, then ``select_edition`` on the track count."""
        key = ("target", _norm(artist), _norm(album), track_count)
        cached = self._cached(key)
        if cached is not None:
            return cached or None  # type: ignore[return-value]
        from repositories.musicbrainz_base import escape_lucene_phrase as esc

        found = await self._mb(
            "release-group", query=f'releasegroup:"{esc(album)}" AND artist:"{esc(artist)}"', limit=10
        )
        groups = []
        for rg in found.get("release-groups") or []:
            rg_artist = _credit(rg.get("artist-credit") or [])
            if not _same_artist(artist, rg_artist):
                continue
            match = _album_match(artist, album, f"{rg_artist} - {rg.get('title', '')}")
            if match >= 0.8 or _bare(rg.get("title", "")) == _bare(album):
                groups.append((match, rg))
        groups.sort(key=lambda item: item[0], reverse=True)
        target = None
        for _, rg in groups[:3]:
            detail = await self._mb(f"release-group/{rg['id']}", inc="artist-credits+releases+media")
            releases = detail.get("releases") or []
            release_id = _select_edition(releases, track_count or 0) if releases else None
            if not release_id:
                continue
            release = await self._mb(f"release/{release_id}", inc="recordings+isrcs+artist-credits")
            tracks = []
            media = release.get("media") or []
            for medium in media:
                disc = int(medium.get("position") or 1)
                for tr in medium.get("tracks") or []:
                    rec = tr.get("recording") or {}
                    tracks.append({
                        "disc": disc,
                        "pos": int(tr.get("position") or 0),
                        "title": tr.get("title") or rec.get("title") or "",
                        "length": int(tr.get("length") or rec.get("length") or 0) // 1000,
                        "isrcs": list(rec.get("isrcs") or []),
                        "recording": rec.get("id", ""),
                        "release_track": tr.get("id", ""),
                        "artist": _credit(tr.get("artist-credit") or rec.get("artist-credit") or []),
                    })
            if not tracks:
                continue
            if track_count and len(tracks) != track_count:
                # A sibling release group may carry the exact count (single vs EP of the same name).
                target = target or self._target_dict(rg, detail, release, tracks)
                continue
            target = self._target_dict(rg, detail, release, tracks)
            break
        self._store(key, target or {})
        return target

    @staticmethod
    def _target_dict(rg: dict, detail: dict, release: dict, tracks: list[dict]) -> dict:
        credits = release.get("artist-credit") or detail.get("artist-credit") or rg.get("artist-credit") or []
        date = release.get("date") or detail.get("first-release-date") or ""
        return {
            "rg": rg["id"],
            "release": release.get("id", ""),
            "title": release.get("title") or rg.get("title") or "",
            "artist": _credit(credits),
            "artist_id": ((credits[0].get("artist") or {}).get("id", "") if credits else ""),
            "date": date,
            "year": (detail.get("first-release-date") or date or "")[:4],
            "discs": max((t["disc"] for t in tracks), default=1),
            "barcodes": sorted({
                r.get("barcode") for r in detail.get("releases") or [] if r.get("barcode")
            } | ({release["barcode"]} if release.get("barcode") else set())),
            "tracks": tracks,
        }

    # -- matching ------------------------------------------------------------

    @staticmethod
    def _match_track(mb: dict, songs: list[dict], used: set[str], same_album: bool = False) -> dict | None:
        """``same_album``: the songs come from a Deezer album of the wanted title, so a
        looser title (spelling drift) is trusted at the same position or exact length."""
        best, best_score = None, 0.0
        for song in songs:
            sid = str(song.get("SNG_ID", ""))
            if not sid or sid in used:
                continue
            duration = int(song.get("DURATION") or 0)
            delta = abs(duration - mb["length"]) if mb["length"] and duration else None
            # DroppedNeedle's import drops a file whose length is off by more than
            # max(15 s, 10 %) from MusicBrainz - picking it here only yields a partial import.
            fits = delta is None or delta <= max(15.0, 0.10 * mb["length"])
            if song.get("ISRC") and song["ISRC"] in mb["isrcs"] and fits:
                return song
            score = _title_score(mb["title"], song.get("SNG_TITLE", ""), song.get("VERSION", ""))
            same_slot = same_album and (
                int(song.get("DISK_NUMBER") or 1), int(song.get("TRACK_NUMBER") or 0)
            ) == (mb["disc"], mb["pos"])
            if score >= 0.95:
                ok = fits
            elif score >= 0.85:
                ok = (delta is not None and delta <= (6 if same_album else 4)) or (same_slot and fits)
            elif score >= 0.6 and same_album:
                ok = delta is not None and delta <= 3  # spelling drift: "Ypê" vs "Ipê"
            else:
                ok = False
            if ok and score > best_score:
                best, best_score = song, score
        return best

    def _assign(self, target: dict, songs: list[dict], plan: dict[int, dict], used: set[str],
                same_album: bool = False) -> int:
        added = 0
        for index, mb in enumerate(target["tracks"]):
            if index in plan:
                continue
            song = self._match_track(mb, songs, used, same_album)
            if song is None:
                continue
            fmt = self._pick_format(song)
            if fmt is None and song.get("FALLBACK"):
                fallback = song["FALLBACK"]
                if self._pick_format(fallback):
                    song, fmt = {**song, **fallback}, self._pick_format(fallback)
            if fmt is None:
                continue
            used.add(str(song["SNG_ID"]))
            plan[index] = {**song, "_fmt": fmt}
            added += 1
        return added

    async def _deezer_albums(self, artist: str, album: str, barcodes: list[str]) -> list[dict]:
        """Candidate Deezer albums: exact UPC hits first, then the album search."""
        seen: dict[str, dict] = {}
        for barcode in barcodes[:4]:
            for upc in dict.fromkeys((barcode, barcode.lstrip("0"), barcode.zfill(13))):
                if not upc:
                    continue
                data = await self._api(f"album/upc:{upc}")
                if data.get("id"):
                    seen.setdefault(str(data["id"]), {**data, "_upc": True})
                    break
        search = await self._api("search/album", q=f'artist:"{artist}" album:"{album}"', limit=15)
        if not search.get("data"):
            search = await self._api("search/album", q=f"{artist} {album}", limit=15)
        ranked = []
        for item in search.get("data") or []:
            name = (item.get("artist") or {}).get("name", "")
            if not _same_artist(artist, name):
                continue
            score = _album_match(artist, album, f"{name} - {item.get('title', '')}")
            if score >= MIN_ALBUM_SCORE:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        for _, item in ranked:
            seen.setdefault(str(item["id"]), item)
        return list(seen.values())[:MAX_ALBUM_CANDIDATES + 2]

    async def _fill(self, artist: str, target: dict, plan: dict[int, dict], used: set[str], deadline: float) -> None:
        """Missing tracks: ISRC lookup, then a track search scoped to the artist."""
        lookups = 0
        found: list[tuple[int, str]] = []
        for index, mb in enumerate(target["tracks"]):
            if index in plan or lookups >= MAX_FILL_LOOKUPS or time.monotonic() > deadline:
                continue
            hit = None
            for isrc in mb["isrcs"][:2]:
                lookups += 1
                data = await self._api(f"track/isrc:{isrc}")
                if data.get("id") and str(data["id"]) not in used:
                    hit = str(data["id"])
                    break
            if hit is None:
                lookups += 1
                data = await self._api("search/track", q=f'artist:"{artist}" track:"{_bare(mb["title"])}"', limit=10)
                candidates = [
                    {"SNG_ID": str(t["id"]), "SNG_TITLE": t.get("title", ""), "VERSION": t.get("title_version", ""),
                     "DURATION": t.get("duration", 0), "ISRC": ""}
                    for t in data.get("data") or []
                    if _same_artist(artist, (t.get("artist") or {}).get("name", ""))
                ]
                song = self._match_track(mb, candidates, used)
                hit = str(song["SNG_ID"]) if song else None
            if hit:
                used.add(hit)
                found.append((index, hit))
        if not found:
            return
        details = {str(s.get("SNG_ID")): s for s in await self._songs([sid for _, sid in found])}
        for index, sid in found:
            song = details.get(sid)
            fmt = self._pick_format(song) if song else None
            if song and fmt:
                plan[index] = {**song, "_fmt": fmt}

    # -- releases ------------------------------------------------------------

    def _tags(self, target: dict, index: int, song: dict) -> dict:
        mb = target["tracks"][index]
        tags = {
            "title": mb["title"],
            "artist": mb["artist"] or target["artist"],
            "album": target["title"],
            "albumartist": target["artist"],
            "tracknumber": str(mb["pos"]),
            "tracktotal": str(sum(1 for t in target["tracks"] if t["disc"] == mb["disc"])),
            "discnumber": str(mb["disc"]),
            "disctotal": str(target["discs"]),
            "date": target["date"],
            "isrc": song.get("ISRC") or (mb["isrcs"][0] if mb["isrcs"] else ""),
            "musicbrainz_albumid": target["release"],
            "musicbrainz_releasegroupid": target["rg"],
            "musicbrainz_trackid": mb["recording"],
            "musicbrainz_releasetrackid": mb["release_track"],
            "musicbrainz_albumartistid": target["artist_id"],
            "deezer_id": str(song.get("SNG_ID", "")),
        }
        if song.get("ALB_PICTURE"):
            tags["_cover"] = COVER_URL.format(song["ALB_PICTURE"])
        return {k: v for k, v in tags.items() if v}

    def _filename(self, artist: str, album: str, year: str, disc: int, pos: int, discs: int, title: str, fmt: str) -> str:
        number = f"{disc}-{pos:02d}" if discs > 1 else f"{pos:02d}"
        folder = f"{_safe(album)} ({year})" if year else _safe(album)
        return f"{_safe(artist)}/{folder}/{number}. {_safe(title)}.{_EXT[fmt]}"

    def _release(self, title: str, files: dict[str, dict], score: float) -> PluginSearchResult:
        fmts = {f["fmt"] for f in files.values()}
        return PluginSearchResult(
            title=title,
            size_bytes=sum(int(f.pop("size", 0)) for f in files.values()),
            score=max(0.0, min(1.0, score)),
            quality_tier="lossless" if fmts == {"FLAC"} else "mp3_320",
            files=[],  # folder mode: per-file plugin candidates fail to decode in DroppedNeedle 2.15
            payload=json.dumps({"v": 1, "files": files}, ensure_ascii=False),
        )

    async def _guided(self, artist: str, target: dict, timeout: float) -> list[IndexerResult]:
        deadline = time.monotonic() + max(5.0, timeout * 0.7)
        albums = await self._deezer_albums(target["artist"] or artist, target["title"], target["barcodes"])
        loaded: list[tuple[dict, list[dict]]] = []
        for album in albums:
            if time.monotonic() > deadline:
                break
            try:
                loaded.append((album, await self._album_tracks(str(album["id"]))))
            except Exception as exc:  # noqa: BLE001 - one bad album drops itself only
                self._log.warning("deezer: álbum %s falhou: %s", album.get("id"), exc)
        total = len(target["tracks"])

        def same_album(album: dict) -> bool:
            return bool(album.get("_upc")) or _bare(album.get("title", "")) == _bare(target["title"])

        # Base album = the one covering most of the MB tracklist (UPC hit breaks ties).
        def coverage(entry: tuple[dict, list[dict]]) -> tuple:
            trial: dict[int, dict] = {}
            self._assign(target, entry[1], trial, set(), same_album(entry[0]))
            return (len(trial), bool(entry[0].get("_upc")), -abs(len(entry[1]) - total))

        loaded.sort(key=coverage, reverse=True)
        plan: dict[int, dict] = {}
        used: set[str] = set()
        sources = 0
        for album, songs in loaded:
            if len(plan) == total:
                break
            if self._assign(target, songs, plan, used, same_album(album)):
                sources += 1
        if len(plan) < total:
            before = len(plan)
            await self._fill(target["artist"] or artist, target, plan, used, deadline)
            sources += 1 if len(plan) > before else 0
        if not plan:
            return []
        files = {}
        for index, song in sorted(plan.items()):
            mb = target["tracks"][index]
            name = self._filename(target["artist"] or artist, target["title"], target["year"], mb["disc"], mb["pos"],
                                  target["discs"], mb["title"], song["_fmt"])
            files[name] = {"id": str(song["SNG_ID"]), "fmt": song["_fmt"],
                           "size": int(song.get(_FORMAT_KEY[song["_fmt"]]) or 0),
                           "tags": self._tags(target, index, song)}
        score = SCORE_FULL if sources <= 1 else SCORE_ASSEMBLED
        if len(plan) < total:
            score = SCORE_PARTIAL if len(plan) >= 0.75 * total else SCORE_FRAGMENT
            self._log.info("deezer: %s - %s: %d/%d faixas no Deezer", target["artist"], target["title"], len(plan), total)
        title = f"{target['artist'] or artist} - {target['title']}" + (f" ({target['year']})" if target["year"] else "")
        return [IndexerResult(source=SOURCE_KEY, plugin=self._release(title, files, score))]

    async def _unguided(self, artist: str, album: str, year: int | None, track_count: int | None) -> list[IndexerResult]:
        """No MusicBrainz match: whole Deezer albums ranked by title and track count."""
        out = []
        for item in (await self._deezer_albums(artist, album, []))[:MAX_ALBUM_CANDIDATES]:
            name = (item.get("artist") or {}).get("name", "") or artist
            score = _album_match(artist, album, f"{name} - {item.get('title', '')}")
            songs = await self._album_tracks(str(item["id"]))
            if not songs:
                continue
            release_year = str(songs[0].get("PHYSICAL_RELEASE_DATE") or "")[:4]
            if year and release_year.isdigit() and abs(int(release_year) - int(year)) > 1:
                score *= 0.9
            if track_count and len(songs) != track_count:
                score *= 0.65 if abs(len(songs) - track_count) <= 2 else 0.45
            files = {}
            discs = max(int(s.get("DISK_NUMBER") or 1) for s in songs)
            for song in songs:
                fmt = self._pick_format(song)
                if fmt is None:
                    continue
                disc, pos = int(song.get("DISK_NUMBER") or 1), int(song.get("TRACK_NUMBER") or 0)
                fname = self._filename(name, item.get("title", ""), release_year, disc, pos, discs, song.get("SNG_TITLE", ""), fmt)
                files[fname] = {
                    "id": str(song["SNG_ID"]), "fmt": fmt, "size": int(song.get(_FORMAT_KEY[fmt]) or 0),
                    "tags": {k: v for k, v in {
                        "title": song.get("SNG_TITLE", ""), "artist": song.get("ART_NAME", "") or name,
                        "album": item.get("title", ""), "albumartist": name, "tracknumber": str(pos),
                        "discnumber": str(disc), "date": str(song.get("PHYSICAL_RELEASE_DATE") or ""),
                        "isrc": song.get("ISRC", ""), "deezer_id": str(song["SNG_ID"]),
                        "_cover": COVER_URL.format(song["ALB_PICTURE"]) if song.get("ALB_PICTURE") else "",
                    }.items() if v},
                }
            if len(files) < len(songs):
                score *= 0.8
            if files and score >= MIN_RELEASE_SCORE:
                title = f"{name} - {item.get('title', '')}" + (f" ({release_year})" if release_year else "")
                out.append(IndexerResult(source=SOURCE_KEY, plugin=self._release(title, files, score)))
        return out

    # -- common --------------------------------------------------------------

    def is_configured(self) -> bool:
        return len(self._arl) > 100

    async def health_check(self) -> ServiceStatus:
        if not self.is_configured():
            return ServiceStatus(status="error", message="ARL do Deezer não configurado")
        try:
            session = await self._login(force=True)
        except Exception as exc:  # noqa: BLE001
            return ServiceStatus(status="error", message=f"Deezer: {exc}")
        if "FLAC" in self._formats[:1] and not session["lossless"]:
            return ServiceStatus(status="error", message=f"A conta ({session['offer']}) não tem FLAC")
        return ServiceStatus(status="ok", version=f"{session['offer']} ({session['country']})")

    # -- indexer -------------------------------------------------------------

    @property
    def indexer_name(self) -> str:
        return SOURCE_KEY

    async def search_album(
        self,
        artist_name: str,
        album_title: str,
        year: int | None = None,
        track_count: int | None = None,
        *,
        timeout: float = 30.0,
    ) -> list[IndexerResult]:
        if not self.is_configured():
            return []
        target = None
        try:
            target = await self._target(artist_name, album_title, track_count)
        except Exception as exc:  # noqa: BLE001 - MB down: Deezer-only matching still works
            self._log.warning("deezer: MusicBrainz falhou para %s - %s: %s", artist_name, album_title, exc)
        if target and (not track_count or len(target["tracks"]) == track_count):
            results = await self._guided(artist_name, target, timeout)
            if results:
                return results
        if track_count == 1:
            return await self._search_single(artist_name, album_title)
        return await self._unguided(artist_name, album_title, year, track_count)

    async def _search_single(self, artist_name: str, title: str) -> list[IndexerResult]:
        data = await self._api("search/track", q=f'artist:"{artist_name}" track:"{title}"', limit=15)
        ranked = []
        for t in data.get("data") or []:
            name = (t.get("artist") or {}).get("name", "")
            if not _same_artist(artist_name, name):
                continue
            score = _album_match(artist_name, title, f"{name} - {t.get('title', '')}")
            if score >= MIN_RELEASE_SCORE:
                ranked.append((score, t))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return await self._track_releases(artist_name, ranked[:3], title)

    async def _track_releases(self, artist_name: str, ranked: list[tuple[float, dict]], name: str | None) -> list[IndexerResult]:
        if not ranked:
            return []
        details = {str(s.get("SNG_ID")): s for s in await self._songs([str(t["id"]) for _, t in ranked])}
        out = []
        for score, t in ranked:
            song = details.get(str(t["id"]))
            fmt = self._pick_format(song) if song else None
            if not fmt:
                continue
            artist = song.get("ART_NAME") or artist_name
            album, year = song.get("ALB_TITLE", ""), str(song.get("PHYSICAL_RELEASE_DATE") or "")[:4]
            disc, pos = int(song.get("DISK_NUMBER") or 1), int(song.get("TRACK_NUMBER") or 1)
            fname = self._filename(artist, album, year, disc, pos, 1, song.get("SNG_TITLE", ""), fmt)
            files = {fname: {
                "id": str(song["SNG_ID"]), "fmt": fmt, "size": int(song.get(_FORMAT_KEY[fmt]) or 0),
                "tags": {k: v for k, v in {
                    "title": song.get("SNG_TITLE", ""), "artist": artist, "album": album, "albumartist": artist,
                    "tracknumber": str(pos), "discnumber": str(disc),
                    "date": str(song.get("PHYSICAL_RELEASE_DATE") or ""), "isrc": song.get("ISRC", ""),
                    "deezer_id": str(song["SNG_ID"]),
                    "_cover": COVER_URL.format(song["ALB_PICTURE"]) if song.get("ALB_PICTURE") else "",
                }.items() if v},
            }}
            # Named after the requested single, not the album the track sits on:
            # DroppedNeedle drops releases whose title doesn't match the wanted one.
            title = f"{artist} - {name}" if name else f"{artist} - {album}" + (f" ({year})" if year else "")
            out.append(IndexerResult(source=SOURCE_KEY, plugin=self._release(title, files, score)))
        return out

    async def search_track(
        self,
        artist_name: str,
        track_title: str,
        album_title: str | None = None,
        duration_seconds: int | None = None,
        *,
        timeout: float = 30.0,
    ) -> list[IndexerResult]:
        if not self.is_configured():
            return []
        data = await self._api("search/track", q=f'artist:"{artist_name}" track:"{track_title}"', limit=15)
        ranked = []
        for t in data.get("data") or []:
            name = (t.get("artist") or {}).get("name", "")
            if not _same_artist(artist_name, name):
                continue
            score = _album_match(artist_name, track_title, f"{name} - {t.get('title', '')}")
            if duration_seconds and t.get("duration") and abs(int(t["duration"]) - duration_seconds) > 5:
                score *= 0.8
            if album_title and _bare((t.get("album") or {}).get("title", "")) == _bare(album_title):
                score = min(1.0, score + 0.05)
            if score >= MIN_ALBUM_SCORE:
                ranked.append((score, t))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return await self._track_releases(artist_name, ranked[:5], None)

    # -- download client -----------------------------------------------------

    @property
    def client_name(self) -> str:
        return SOURCE_KEY

    def _job_dir(self, key: str) -> Path:
        return self._download_dir / _safe(key)

    def _job(self, handle: TaskHandle) -> _Job | None:
        key = handle.job_name
        if not key:
            return None
        job = self._jobs.get(key)
        if job is None:
            job = _Job.load(self._job_dir(key))
            if job is None:
                return None
            self._jobs[key] = job
        if not job.running and any(f["state"] in ("pending", "downloading") for f in job.files):
            # DroppedNeedle restarted mid-download: resume the unfinished tracks.
            job.worker = asyncio.create_task(self._run(job))
        return job

    async def enqueue(self, request: EnqueueRequest) -> TaskHandle:
        try:
            mapping = json.loads(request.payload or "{}").get("files", {})
        except ValueError as exc:
            raise RuntimeError("payload inválido") from exc
        if not mapping:
            raise RuntimeError("payload sem faixas")
        # job_name is per candidate (droppedneedle-<task>-<n>): a failover to the next
        # candidate must not reuse, and mix files into, the previous candidate's folder.
        key = request.job_name or f"deezer-{request.task_id}"
        directory = self._job_dir(key)
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        job = _Job(key, directory, [
            {"filename": name, "id": spec["id"], "fmt": spec["fmt"], "tags": spec.get("tags") or {},
             "local": "", "state": "pending", "bytes": 0, "error": ""}
            for name, spec in mapping.items()
        ])
        await asyncio.to_thread(job.save)
        self._jobs[key] = job
        job.worker = asyncio.create_task(self._run(job))
        return TaskHandle(source=SOURCE_KEY, filenames=list(mapping), job_name=key, plugin_token=request.payload)

    async def _run(self, job: _Job) -> None:
        covers: dict[str, bytes | None] = {}

        async def one(entry: dict) -> None:
            async with self._semaphore():
                for attempt in range(TRACK_RETRIES + 1):
                    try:
                        await self._download(job, entry, covers)
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - recorded per track
                        entry["error"] = str(exc)[:300]
                        self._log.warning("deezer %s tentativa %d falhou: %s", entry["id"], attempt + 1, exc)
                        await asyncio.sleep(3 * (attempt + 1))
                entry["state"] = "failed"
                await asyncio.to_thread(job.save)

        await asyncio.gather(*(one(e) for e in job.files if e["state"] in ("pending", "downloading")))

    async def _stream_url(self, sng_id: str, wanted: str) -> tuple[str, str]:
        songs = await self._songs([sng_id])
        if not songs or not songs[0].get("TRACK_TOKEN"):
            raise RuntimeError("faixa indisponível no Deezer")
        song = songs[0]
        formats = [wanted] + [f for f in self._formats if f != wanted]
        session = await self._login()
        resp = await self._http.post(
            MEDIA_URL,
            json={
                "license_token": session["license"],
                "media": [{"type": "FULL", "formats": [{"cipher": "BF_CBC_STRIPE", "format": f} for f in formats]}],
                "track_tokens": [song["TRACK_TOKEN"]],
            },
            headers={"User-Agent": UA},
            timeout=20.0,
        )
        resp.raise_for_status()
        data = (resp.json().get("data") or [{}])[0]
        if data.get("errors"):
            raise RuntimeError(f"get_url: {data['errors'][0].get('message', data['errors'])}")
        media = (data.get("media") or [{}])[0]
        sources = media.get("sources") or []
        if not sources:
            raise RuntimeError("get_url sem fontes")
        return sources[0]["url"], media.get("format") or wanted

    async def _download(self, job: _Job, entry: dict, covers: dict[str, bytes | None]) -> None:
        from cryptography.hazmat.decrepit.ciphers.algorithms import Blowfish
        from cryptography.hazmat.primitives.ciphers import Cipher, modes

        entry.update(state="downloading", bytes=0)
        url, fmt = await self._stream_url(entry["id"], entry["fmt"])
        stem = Path(entry["filename"].rsplit("/", 1)[-1]).stem
        part = job.directory / f"{stem}.part"
        algorithm, iv = Blowfish(_blowfish_key(entry["id"])), bytes(range(8))
        block = 0
        pending = b""
        async with self._http.stream("GET", url, headers={"User-Agent": UA}, timeout=120.0) as resp:
            resp.raise_for_status()
            with part.open("wb") as fh:
                async for chunk in resp.aiter_bytes(256 * 1024):
                    pending += chunk
                    entry["bytes"] += len(chunk)
                    usable = len(pending) - len(pending) % 2048
                    out = bytearray()
                    for start in range(0, usable, 2048):
                        piece = pending[start:start + 2048]
                        if block % 3 == 0:
                            dec = Cipher(algorithm, modes.CBC(iv)).decryptor()
                            piece = dec.update(piece) + dec.finalize()
                        out += piece
                        block += 1
                    fh.write(out)
                    pending = pending[usable:]
                fh.write(pending)  # trailing partial block is never encrypted
        with part.open("rb") as fh:
            magic = fh.read(4)
        if entry["bytes"] < 64 * 1024 or (fmt == "FLAC" and magic != b"fLaC"):
            part.unlink(missing_ok=True)
            raise RuntimeError(f"arquivo inválido ({entry['bytes']} bytes, {magic!r})")
        final = job.directory / f"{stem}.{_EXT.get(fmt, 'flac')}"
        cover_url = entry["tags"].get("_cover", "")
        if cover_url and cover_url not in covers:
            covers[cover_url] = None
            try:
                got = await self._http.get(cover_url, headers={"User-Agent": UA}, timeout=20.0)
                if got.status_code == 200 and got.headers.get("content-type", "").startswith("image/"):
                    covers[cover_url] = got.content
            except Exception as exc:  # noqa: BLE001 - cover is optional
                self._log.info("deezer: capa indisponível: %s", exc)
        await asyncio.to_thread(_write_tags, part, fmt, entry["tags"], covers.get(cover_url))
        part.replace(final)
        entry.update(state="done", local=str(final), fmt=fmt, error="")
        await asyncio.to_thread(job.save)

    def _status_of(self, job: _Job) -> str:
        states = [f["state"] for f in job.files]
        if any(s in ("pending", "downloading") for s in states):
            return "downloading" if any(s != "pending" for s in states) else "queued"
        if all(s == "done" for s in states):
            return "completed"
        return "partial" if any(s == "done" for s in states) else "failed"

    async def get_status(self, handle: TaskHandle) -> DownloadTaskStatus:
        job = self._job(handle)
        if job is None:
            return DownloadTaskStatus(task_id=handle.job_name, status="failed", error="tarefa desconhecida")
        done = [f for f in job.files if f["state"] == "done"]
        failed = [f for f in job.files if f["state"] == "failed"]
        total = len(job.files)
        downloaded = sum(int(f["bytes"]) for f in job.files)
        return DownloadTaskStatus(
            task_id=job.key,
            status=self._status_of(job),
            files_total=total,
            files_completed=len(done),
            files_failed=len(failed),
            bytes_total=downloaded,
            bytes_downloaded=downloaded,
            progress_percent=round(100.0 * (len(done) + len(failed)) / total, 1) if total else 0.0,
            error="; ".join(f["error"] for f in failed)[:500] or None,
            succeeded_filenames=[f["filename"] for f in done],
            has_active_transfer=job.running,
            matched_transfers=total,
        )

    async def abort(self, handle: TaskHandle) -> bool:
        job = self._job(handle)
        if job is None:
            return True
        if job.worker is not None and not job.worker.done():
            job.worker.cancel()
            try:
                await job.worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for entry in job.files:
            if entry["state"] in ("pending", "downloading"):
                entry["state"] = "failed"
                entry["error"] = "cancelado"
        for part in job.directory.glob("*.part"):
            part.unlink(missing_ok=True)
        await asyncio.to_thread(job.save)
        return True

    async def inspect_materialization(self, handle: TaskHandle) -> DownloadMaterialization:
        root = self._download_dir
        healthy = root.is_dir()
        job = self._job(handle)
        if job is None:
            return DownloadMaterialization(state="missing", mount_root=str(root), mount_healthy=healthy)
        status = self._status_of(job)
        state = "active" if status in ("queued", "downloading") else "failed" if status == "failed" else "completed"
        return DownloadMaterialization(
            state=state,
            mount_root=str(root),
            workspace_path=str(job.directory),
            # No per-file evidence: DroppedNeedle 2.15 only fingerprints Soulseek files,
            # so listing them here strands cleanup in "materialization_fingerprint_missing".
            file_paths=[],
            mount_healthy=healthy,
        )

    async def discard_client_artifacts(self, handle: TaskHandle) -> bool:
        job = self._job(handle)
        if job is None:
            return True
        if job.running:
            return False
        self._jobs.pop(job.key, None)
        if job.directory.parent == self._download_dir:
            await asyncio.to_thread(shutil.rmtree, job.directory, True)
        return True

    async def list_completed_files(self, handle: TaskHandle) -> list[Path]:
        job = self._job(handle)
        if job is None:
            return []
        return [Path(f["local"]) for f in job.files if f["state"] == "done" and f["local"]]

    async def get_file_path(
        self,
        handle: TaskHandle,
        remote_filename: str,
        size: int | None = None,
    ) -> Path | None:
        job = self._job(handle)
        if job is None:
            return None
        for entry in job.files:
            if entry["filename"] == remote_filename and entry["state"] == "done" and entry["local"]:
                path = Path(entry["local"])
                return path if path.is_file() else None
        return None

    async def diagnose_downloads_mount(self) -> MountDiagnosis:
        return MountDiagnosis(supported=False)


def _write_tags(path: Path, fmt: str, tags: dict, cover: bytes | None) -> None:
    tags = {k: v for k, v in tags.items() if not k.startswith("_")}
    if fmt == "FLAC":
        from mutagen.flac import FLAC, Picture

        audio = FLAC(path)
        for key, value in tags.items():
            audio[key.upper()] = [str(value)]
        if cover:
            picture = Picture()
            picture.type, picture.mime, picture.desc, picture.data = 3, "image/jpeg", "Cover", cover
            audio.clear_pictures()
            audio.add_picture(picture)
        audio.save()
        return
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import APIC, ID3, TXXX, ID3NoHeaderError

    try:
        audio = EasyID3(path)
    except ID3NoHeaderError:
        audio = EasyID3()
    for key, value in tags.items():
        if key in ("tracktotal", "disctotal", "deezer_id"):
            continue
        if key == "tracknumber" and tags.get("tracktotal"):
            value = f"{value}/{tags['tracktotal']}"
        if key == "discnumber" and tags.get("disctotal"):
            value = f"{value}/{tags['disctotal']}"
        if key in EasyID3.valid_keys:
            audio[key] = str(value)
    audio.save(path)
    id3 = ID3(path)
    if tags.get("deezer_id"):
        id3.add(TXXX(encoding=3, desc="DEEZER_ID", text=[tags["deezer_id"]]))
    if cover:
        id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    id3.save(path)
