"""iHome · Em breve: filmes e séries que ainda vão chegar ao Jellyfin, para widget da tela inicial.

Dados: GET /embreve do midia-notify (CT 102), mesma regra das seções "em breve" do Jellyfin.
Caminhos para widget:
  plugin://plugin.video.ihome.embreve/?tipo=todos    (filmes e séries, por data)
  plugin://plugin.video.ihome.embreve/?tipo=filmes
  plugin://plugin.video.ihome.embreve/?tipo=series
"""
import json
import sys
import urllib.parse
import urllib.request

import xbmcaddon
import xbmcgui
import xbmcplugin

ADDON = xbmcaddon.Addon()
HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].lstrip("-").isdigit() else -1
PARAMS = dict(urllib.parse.parse_qsl(sys.argv[2][1:] if len(sys.argv) > 2 else ""))


def ddmm(iso):
    try:
        a, m, d = iso.split("-")
        return f"{d}/{m}"
    except ValueError:
        return iso


def carregar():
    url = ADDON.getSetting("url") or "http://192.168.10.20:8095/embreve"
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.load(r)


def quando(x):
    if x["tipo"] == "filme":
        return f"Chega {ddmm(x['data'])} · {x.get('lancamento') or ''}".rstrip(" ·")
    return f"{x.get('episodio', '')} · {ddmm(x['data'])}"


def listar(tipo):
    try:
        d = carregar()
    except Exception as e:  # sem servidor: widget vazio, sem travar a tela inicial
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        return
    itens = (d.get("filmes", []) if tipo in ("todos", "filmes") else []) + \
            (d.get("series", []) if tipo in ("todos", "series") else [])
    itens.sort(key=lambda x: x.get("data") or "")
    for x in itens:
        linha = quando(x)
        li = xbmcgui.ListItem(x.get("titulo") or "?", linha)
        art = {"poster": x.get("poster"), "thumb": x.get("poster"), "fanart": x.get("fanart")}
        li.setArt({k: v for k, v in art.items() if v})
        tag = li.getVideoInfoTag()
        tag.setTitle(x.get("titulo") or "")
        tag.setMediaType("movie" if x["tipo"] == "filme" else "tvshow")
        if x.get("ano"):
            tag.setYear(int(x["ano"]))
        tag.setPremiered(x.get("data") or "")
        tag.setTagLine(linha)
        ep = f"\n{x['nome_episodio']}" if x.get("nome_episodio") else ""
        tag.setPlot(f"[B]{linha}[/B]{ep}\n\n{x.get('sinopse') or ''}")
        li.setProperty("IsPlayable", "false")
        url = "plugin://plugin.video.ihome.embreve/?" + urllib.parse.urlencode(
            {"acao": "info", "titulo": x.get("titulo") or "", "texto": f"{linha}{ep}\n\n{x.get('sinopse') or ''}"})
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
    xbmcplugin.setContent(HANDLE, "movies")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


if PARAMS.get("acao") == "info":
    xbmcgui.Dialog().textviewer(PARAMS.get("titulo", ""), PARAMS.get("texto", ""))
else:
    listar(PARAMS.get("tipo", "todos"))
