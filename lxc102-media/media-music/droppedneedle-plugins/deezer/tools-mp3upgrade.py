"""Troca MP3 da biblioteca pelo FLAC do Deezer, copiando as tags do MP3.

Uso (dentro do container droppedneedle, como uid 1000):
  python3 mp3upgrade.py lista.txt            # simulação: só mostra o casamento
  python3 mp3upgrade.py lista.txt apply      # baixa, troca e manda o MP3 para a lixeira
lista.txt = caminhos relativos a /music, um por linha.
"""
import asyncio, json, logging, os, shutil, sys
from pathlib import Path

sys.path.insert(0, "/app/plugins/.sdk/python")
sys.path.insert(0, "/app/plugins/deezer")
import plugin  # noqa: E402
from mutagen import File as MFile  # noqa: E402  (vendor do plugin)
from mutagen.id3 import ID3  # noqa: E402

ROOT = Path("/music")
TRASH = Path(os.environ.get("TRASH", "/data/lixeira-musica/mp3-trocados-por-flac"))  # precisa ser gravável pelo uid 1000
STAGE = Path("/app/plugins/.data/deezer/mp3upgrade")
MAX_DIFF = 3


class Ctx:
    settings = {"arl": os.environ["ARL"], "musicbrainz_url": "https://api.brainzmash.cc/ws/2"}
    logger = logging.getLogger("mp3upgrade")
    data_dir = "/app/plugins/.data/deezer"


def read_tags(path: Path) -> tuple[dict, float, bytes | None]:
    easy = MFile(path, easy=True)
    tags = {k: v[0] for k, v in (easy.tags or {}).items() if v and k != "length"}
    for key, total in (("tracknumber", "tracktotal"), ("discnumber", "disctotal")):
        if "/" in tags.get(key, ""):
            tags[key], tags[total] = tags[key].split("/", 1)
    cover = None
    try:
        pics = ID3(path).getall("APIC")
        cover = pics[0].data if pics else None
    except Exception:  # noqa: BLE001
        pass
    return tags, float(easy.info.length), cover


async def find(d: plugin.Deezer, tags: dict, length: float, folder_artist: str):
    title = tags.get("title", "")
    artists = [a.split(" feat")[0] for a in (tags.get("artist", ""), tags.get("albumartist", ""), folder_artist) if a]
    cands = {}
    for artist in dict.fromkeys(artists):
        for q in (title, f"{title} {tags.get('album', '')}".strip()):
            data = await d._search_tracks(artist, q, 25)
            for x in data.get("data") or []:
                if not plugin._same_artist(artist, (x.get("artist") or {}).get("name", "")):
                    continue
                diff = abs(int(x.get("duration") or 0) - length)
                score = plugin._track_score(title, x, None, tags.get("album"))
                extra = set(plugin._norm(f"{x.get('title', '')} {x.get('title_version', '')} {(x.get('album') or {}).get('title', '')}").split())
                wanted = set(plugin._norm(f"{title} {tags.get('album', '')}").split())
                if (extra - wanted) & {"redux", "live", "vivo", "rerecorded", "recorded", "taylor", "karaoke"}:
                    continue
                if diff <= MAX_DIFF and score >= 0.95:
                    cands[x["id"]] = ((score, -diff), x)
    if not cands:
        return None
    ranked = [x for _, x in sorted(cands.values(), key=lambda c: c[0], reverse=True)]
    songs = {str(s.get("SNG_ID")): s for s in await d._songs([str(x["id"]) for x in ranked])}
    for x in ranked:
        song = songs.get(str(x["id"]))
        if song and d._pick_format(song) == "FLAC":
            return ("ok", x)
    return ("sem-flac", ranked[0])


async def main() -> None:
    apply = len(sys.argv) > 2 and sys.argv[2] == "apply"
    rels = [l.strip() for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
    d = plugin.Deezer(Ctx())
    report = []
    STAGE.mkdir(parents=True, exist_ok=True)
    for rel in rels:
        src = ROOT / rel
        if not src.exists():
            print("FALTA ", rel); report.append({"path": rel, "result": "missing"}); continue
        tags, length, cover = read_tags(src)
        found = await find(d, tags, length, rel.split("/", 1)[0])
        if not found:
            print("NÃO ACHOU ", rel); report.append({"path": rel, "result": "notfound"}); continue
        status, x = found
        label = f'{x["artist"]["name"]} - {x["title"]} [{x["album"]["title"]}] {x["duration"]}s vs {length:.0f}s'
        if status != "ok":
            print("SEM FLAC  ", rel, "->", label); report.append({"path": rel, "result": "noflac", "deezer": x["id"]}); continue
        if not apply:
            print("CASA      ", rel, "->", label); report.append({"path": rel, "result": "match", "deezer": x["id"]}); continue
        job = plugin._Job(f"up-{x['id']}", STAGE, [])
        entry = {"filename": f"{src.stem}.flac", "id": str(x["id"]), "fmt": "FLAC",
                 "tags": {**tags, "deezer_id": str(x["id"]),
                          "_cover": "" if cover else (plugin.COVER_URL.format(x["album"].get("md5_image")) if x["album"].get("md5_image") else "")},
                 "local": "", "state": "pending", "bytes": 0, "error": ""}
        job.files = [entry]
        try:
            covers = {}
            await d._download(job, entry, covers)
            flac = Path(entry["local"])
            if cover:
                await asyncio.to_thread(plugin._write_tags, flac, "FLAC", {}, cover)
            target = src.with_suffix(".flac")
            if target.exists():
                raise RuntimeError("já existe um .flac com esse nome")
            shutil.move(str(flac), str(target))
            dest = TRASH / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            print("TROCADO   ", rel, "->", label)
            report.append({"path": rel, "result": "replaced", "deezer": x["id"]})
        except Exception as exc:  # noqa: BLE001
            print("ERRO      ", rel, exc)
            report.append({"path": rel, "result": "error", "error": str(exc)})
    out = STAGE / ("report-apply.json" if apply else "report-dry.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    from collections import Counter
    print(Counter(r["result"] for r in report))


asyncio.run(main())
