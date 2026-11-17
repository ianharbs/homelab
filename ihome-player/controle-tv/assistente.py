"""Assistente de voz do controle-tv: microfone 2x → grava → Kodi / TV / Home Assistant.

Fluxo: Whisper local (faster-whisper no CT 102, ~1 s) transcreve → regras locais (comandos.py) para
Kodi/TV → senão o Assist do Home Assistant (casa) → senão o Gemini (perguntas e pedidos complicados,
só texto, com nova tentativa). Sem Whisper, o áudio vai direto ao Gemini (como antes).
Chamado pelo controle-tv.py numa thread própria (ouvir_e_executar). Segredos (root, 600):
  /etc/controle-tv/gemini-key  chave do Google AI Studio (API gratuita)
  /etc/controle-tv/ha-token    token de longa duração do Home Assistant
"""
import array
import base64
import io
import json
import math
import os
import subprocess
import threading
import time
import uuid
import urllib.error
import urllib.request
import wave

import comandos

MIC = os.environ.get("MIC_ALSA", "plughw:CARD=Device,DEV=0")  # microfone do receptor XING WEI
TAXA = 16000
GRAVACOES = "/var/tmp/controle-tv"  # últimas falas gravadas (depuração)
SONS = "/usr/local/share/controle-tv"  # ouvindo.wav / erro.wav (gerados por bipes.py)
USUARIO_SOM = os.environ.get("USUARIO_SOM", "ihomeplayer")  # dono do PipeWire por onde o Kodi toca
GEMINI_MODELOS = os.environ.get("GEMINI_MODELOS", "gemini-3.5-flash-lite,gemini-flash-latest").split(",")
WHISPER_URL = os.environ.get("WHISPER_URL", "http://192.168.10.20:8010")
WHISPER_MODELO = os.environ.get("WHISPER_MODELO", "Systran/faster-whisper-small")
VOCABULARIO = ("Comandos para a TV e o Kodi: legenda em português, legenda em inglês, áudio original, "
               "dublado, volume, mudo, pausa, continua, próximo episódio, temporada, episódio, HDMI, "
               "Kodi, HBO Max, Prime Video, YouTube, Paramount+, Jellyfin, Clarovideo, luz, ar-condicionado.")
GEMINI_CHAVE = os.environ.get("GEMINI_KEY_FILE", "/etc/controle-tv/gemini-key")
HA_URL = os.environ.get("HA_URL", "http://192.168.10.60:8123")
HA_TOKEN = os.environ.get("HA_TOKEN_FILE", "/etc/controle-tv/ha-token")

FALA_MIN = 600       # RMS (0,1 s) que conta como fala; ruído do mic fica em ~200
SILENCIO_MAX = 400   # abaixo disso é silêncio
SILENCIO_FIM = 1.2   # s de silêncio depois da fala encerram a gravação
ESPERA_FALA = 5.0    # s sem fala nenhuma → desiste
GRAVA_MAX = 10.0     # s no máximo
DESCARTA = 0.3       # s iniciais descartados: o mic do controle dá um estalo ao ligar (virava "Até o")

_biblioteca = {"quando": 0, "filmes": {}, "series": {}}
_apps = {"quando": 0, "lista": []}


def _log(*a):
    print("assistente:", *a, flush=True)


def bipe(nome):
    """Toca um som curto no PipeWire do usuário do Kodi (mistura com o vídeo; não espera)."""
    try:
        import pwd
        uid = pwd.getpwnam(USUARIO_SOM).pw_uid
        subprocess.Popen(["runuser", "-u", USUARIO_SOM, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                          "pw-play", os.path.join(SONS, f"{nome}.wav")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as ex:
        _log("bipe falhou:", ex)


# ---------------------------------------------------------------- gravação

def grava(cancelar=None, ao_confirmar=None, espera=0.0):
    """Grava do microfone até a fala terminar. Devolve WAV (bytes) ou None se ninguém falou/cancelou.
    cancelar: Event que aborta (2º clique do microfone); ao_confirmar() roda quando passa `espera` s."""
    proc = subprocess.Popen(["arecord", "-q", "-D", MIC, "-f", "S16_LE", "-r", str(TAXA),
                             "-c", "1", "-t", "raw"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pedaco = TAXA // 10 * 2  # 0,1 s
    audio, falou, silencio, inicio = bytearray(), False, 0.0, time.monotonic()
    try:
        while True:
            d = proc.stdout.read(pedaco)
            if not d:
                break
            if cancelar is not None and cancelar.is_set():
                return None
            if ao_confirmar and time.monotonic() - inicio >= espera:
                ao_confirmar()
                ao_confirmar = None
            if time.monotonic() - inicio < DESCARTA:
                continue
            audio += d
            amostras = array.array("h", d)
            rms = math.sqrt(sum(x * x for x in amostras) / max(1, len(amostras)))
            decorrido = time.monotonic() - inicio
            if rms >= FALA_MIN:
                falou, silencio = True, 0.0
            elif rms < SILENCIO_MAX and falou:
                silencio += 0.1
            if falou and silencio >= SILENCIO_FIM:
                break
            if not falou and decorrido >= ESPERA_FALA:
                return None
            if decorrido >= GRAVA_MAX:
                break
    finally:
        proc.kill()
        proc.wait()
    if not falou:
        return None
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAXA)
        w.writeframes(bytes(audio))
    return b.getvalue()


# ---------------------------------------------------------------- contexto

def _atualiza_biblioteca(kodi_rpc):
    if time.time() - _biblioteca["quando"] < 1800:
        return
    f = kodi_rpc("VideoLibrary.GetMovies", {"properties": ["year"]}).get("movies", [])
    s = kodi_rpc("VideoLibrary.GetTVShows", {"properties": ["year"]}).get("tvshows", [])
    _biblioteca["filmes"] = {m["label"]: m["movieid"] for m in f}
    _biblioteca["series"] = {m["label"]: m["tvshowid"] for m in s}
    _biblioteca["quando"] = time.time()
    _log("biblioteca:", len(f), "filmes,", len(s), "séries")


def _atualiza_apps(tv):
    if time.time() - _apps["quando"] < 3600 and _apps["lista"]:
        return
    try:
        _apps["lista"] = tv._rpc("appControl", "getApplicationList", [])[0]
        _apps["quando"] = time.time()
    except Exception as ex:
        _log("lista de apps da TV falhou:", ex)


def _tocando(kodi_rpc):
    """(playerid, descrição do que toca) ou (None, None)."""
    try:
        for p in kodi_rpc("Player.GetActivePlayers"):
            if p.get("type") != "video":
                continue
            pid = p["playerid"]
            item = kodi_rpc("Player.GetItem", {"playerid": pid, "properties": [
                "title", "showtitle", "season", "episode", "year", "cast", "plot"]})["item"]
            prop = kodi_rpc("Player.GetProperties", {"playerid": pid, "properties": [
                "time", "totaltime", "speed", "subtitleenabled", "currentsubtitle", "subtitles",
                "currentaudiostream", "audiostreams"]})
            elenco = [f'{c.get("name")} ({c.get("role")})' for c in item.get("cast", [])[:15]]
            return pid, {
                "titulo": item.get("title"), "serie": item.get("showtitle") or None,
                "temporada": item.get("season"), "episodio": item.get("episode"),
                "ano": item.get("year"), "sinopse": (item.get("plot") or "")[:600],
                "elenco": elenco, "pausado": prop.get("speed") == 0,
                "tempo": prop.get("time"), "duracao": prop.get("totaltime"),
                "legenda_ligada": prop.get("subtitleenabled"),
                "legenda_atual": prop.get("currentsubtitle"),
                "legendas": [{"indice": x["index"], "idioma": x.get("language"), "nome": x.get("name")}
                             for x in prop.get("subtitles", [])],
                "audio_atual": prop.get("currentaudiostream"),
                "audios": [{"indice": x["index"], "idioma": x.get("language"), "nome": x.get("name")}
                           for x in prop.get("audiostreams", [])],
            }
    except Exception as ex:
        _log("info do player falhou:", ex)
    return None, None


# ---------------------------------------------------------------- Gemini

ESQUEMA = {
    "type": "OBJECT",
    "properties": {
        "transcricao": {"type": "STRING", "description": "o que a pessoa falou, literal"},
        "acoes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "tipo": {"type": "STRING", "enum": ["tocar", "reproducao", "tv", "casa"]},
            "titulo": {"type": "STRING", "description": "tocar: título EXATO da lista de filmes/séries"},
            "categoria": {"type": "STRING", "enum": ["filme", "serie"]},
            "temporada": {"type": "INTEGER"},
            "episodio": {"type": "INTEGER"},
            "comando": {"type": "STRING", "enum": [
                "pausar", "continuar", "parar", "avancar", "voltar", "proximo", "anterior",
                "legenda", "legenda_desligar", "audio",
                "volume", "volume_mais", "volume_menos", "mudo", "tirar_mudo",
                "desligar", "ligar", "hdmi", "app", "kodi"]},
            "valor": {"type": "INTEGER", "description": "segundos, volume (0-100), passo do volume, "
                                                         "porta HDMI ou índice da faixa de legenda/áudio"},
            "app": {"type": "STRING", "description": "app: título EXATO da lista de apps da TV"},
            "texto": {"type": "STRING", "description": "casa: o pedido reescrito em português claro "
                                                       "para o Assist do Home Assistant"},
        }, "required": ["tipo"]}},
        "resposta": {"type": "STRING", "description": "frase curta (até 140 caracteres) para mostrar na tela"},
    },
    "required": ["transcricao", "acoes", "resposta"],
}

INSTRUCOES = """Você é o assistente de voz do iHome Player (Kodi ligado numa TV Sony Bravia) de uma casa no Brasil.
Ouça o áudio (português do Brasil) e responda SÓ com o JSON do esquema.

Tipos de ação:
- tocar: abrir um filme ou série da biblioteca do Kodi. "titulo" deve ser copiado EXATAMENTE da lista abaixo
  (os títulos estão em português; traduza/aproxime o que a pessoa disse). Série sem temporada/episódio = continuar
  de onde parou. Se não existir na lista, não crie ação e diga isso na resposta.
- reproducao: controlar o vídeo que está tocando no Kodi. avancar/voltar com "valor" em segundos (padrão 30;
  "um minuto" = 60). legenda/audio com "valor" = índice da faixa escolhida nas listas do vídeo atual.
  proximo/anterior = episódio seguinte/anterior.
- tv: a TV. volume ("valor" 0-100), volume_mais/volume_menos ("valor" = passo, padrão 5), mudo, tirar_mudo,
  desligar, ligar, hdmi ("valor" = porta), app ("app" = título EXATO da lista de apps), kodi (voltar para o Kodi).
- casa: qualquer coisa da casa (luzes, ar-condicionado, tomadas, cenas, sensores, temperatura de cômodo, energia).
  Ponha em "texto" o pedido claro e curto; o Home Assistant executa e responde.
- Perguntas gerais (sobre o vídeo, atores, curiosidades, qualquer assunto): sem ação; responda em "resposta".
Pode haver mais de uma ação. "resposta" é o que aparece na tela: curta, natural, em português.
Se não deu para entender o áudio, sem ação e resposta "Não entendi, pode repetir?".
"""


def _gemini(contexto, wav=None, texto=None, prazo=20.0):
    """Manda o áudio (sem Whisper) ou o texto já transcrito. Tenta os modelos em ordem até o prazo."""
    chave = open(GEMINI_CHAVE).read().strip()
    fala = ({"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}}
            if wav else {"text": f"A pessoa falou (transcrição automática, pode ter erros): {texto!r}"})
    corpo = json.dumps({
        "systemInstruction": {"parts": [{"text": INSTRUCOES}]},
        "contents": [{"role": "user", "parts": [
            {"text": "Contexto agora:\n" + json.dumps(contexto, ensure_ascii=False)}, fala]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": ESQUEMA,
                             "temperature": 0.2},
    }).encode()
    fim, erro = time.monotonic() + prazo, None
    for modelo in GEMINI_MODELOS * 2:  # cada modelo até duas vezes
        resta = fim - time.monotonic()
        if resta < 2:
            break
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{modelo.strip()}:generateContent",
            data=corpo, headers={"Content-Type": "application/json", "x-goog-api-key": chave})
        try:
            with urllib.request.urlopen(req, timeout=resta) as r:
                d = json.loads(r.read())
            return json.loads(d["candidates"][0]["content"]["parts"][0]["text"])
        except urllib.error.HTTPError as ex:
            erro = f"{modelo} HTTP {ex.code}"
            if ex.code not in (429, 500, 503, 504):
                break
        except Exception as ex:
            erro = f"{modelo} {ex}"
        _log("Gemini falhou, tentando de novo:", erro)
    raise RuntimeError(erro or "sem tempo")


def transcreve(wav):
    """Texto falado, pelo Whisper do CT 102 (API estilo OpenAI). Exceção se o serviço não responder."""
    limite = uuid.uuid4().hex
    campos = {"model": WHISPER_MODELO, "language": "pt", "prompt": VOCABULARIO, "response_format": "json"}
    corpo = b"".join(f'--{limite}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                     for k, v in campos.items())
    corpo += (f'--{limite}\r\nContent-Disposition: form-data; name="file"; filename="fala.wav"\r\n'
              f'Content-Type: audio/wav\r\n\r\n').encode() + wav + f"\r\n--{limite}--\r\n".encode()
    req = urllib.request.Request(f"{WHISPER_URL}/v1/audio/transcriptions", data=corpo,
                                 headers={"Content-Type": f"multipart/form-data; boundary={limite}"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read()).get("text", "").strip()


def _silencio(segundos=0.5):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAXA)
        w.writeframes(b"\0\0" * int(TAXA * segundos))
    return b.getvalue()


def aquece():
    """Carrega o modelo do Whisper (o 1º pedido demora ~2 s a mais)."""
    try:
        transcreve(_silencio())
        _log("Whisper pronto")
    except Exception as ex:
        _log("Whisper indisponível:", ex)


# ---------------------------------------------------------------- execução

def _home_assistant(texto):
    """(resposta, entendeu?) do Assist do Home Assistant."""
    token = open(HA_TOKEN).read().strip()
    req = urllib.request.Request(
        f"{HA_URL}/api/conversation/process",
        data=json.dumps({"text": texto, "language": "pt-BR"}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())["response"]
    fala = ((d.get("speech") or {}).get("plain") or {}).get("speech") or ""
    entendeu = d.get("response_type") != "error" or (d.get("data") or {}).get("code") != "no_intent_match"
    return fala, entendeu


def _episodio(kodi_rpc, tvshowid, temporada, episodio):
    eps = kodi_rpc("VideoLibrary.GetEpisodes", {
        "tvshowid": tvshowid, "properties": ["season", "episode", "playcount", "resume"],
        "sort": {"method": "episode"}}).get("episodes", [])
    eps.sort(key=lambda x: (x["season"] == 0, x["season"], x["episode"]))
    if temporada:
        escolha = [x for x in eps if x["season"] == temporada and (not episodio or x["episode"] == episodio)]
        return escolha[0] if escolha else None
    andamento = [x for x in eps if (x.get("resume") or {}).get("position", 0) > 0]
    if andamento:
        return andamento[0]
    nao_vistos = [x for x in eps if not x.get("playcount") and x["season"] > 0]
    return nao_vistos[0] if nao_vistos else (eps[0] if eps else None)


def _tocar(a, kodi_rpc):
    titulo = a.get("titulo") or ""
    series, filmes = _biblioteca["series"], _biblioteca["filmes"]
    eh_serie = a.get("categoria") == "serie" or (titulo in series and titulo not in filmes)
    if eh_serie and titulo in series:
        ep = _episodio(kodi_rpc, series[titulo], a.get("temporada"), a.get("episodio"))
        if not ep:
            return f"Não achei esse episódio de {titulo}"
        kodi_rpc("Player.Open", {"item": {"episodeid": ep["episodeid"]}, "options": {"resume": True}})
        return f'{titulo} T{ep["season"]}E{ep["episode"]}'
    if titulo in filmes:
        kodi_rpc("Player.Open", {"item": {"movieid": filmes[titulo]}, "options": {"resume": True}})
        return titulo
    return f"Não achei {titulo} na biblioteca"


def _reproducao(a, kodi_rpc, pid):
    c, v = a.get("comando"), a.get("valor")
    if pid is None:
        return "Nada tocando no Kodi"
    if c in ("pausar", "continuar"):
        kodi_rpc("Player.PlayPause", {"playerid": pid, "play": c == "continuar"})
    elif c == "parar":
        kodi_rpc("Player.Stop", {"playerid": pid})
    elif c in ("avancar", "voltar"):
        s = abs(v or 30) * (1 if c == "avancar" else -1)
        kodi_rpc("Player.Seek", {"playerid": pid, "value": {"seconds": s}})
    elif c in ("proximo", "anterior"):
        kodi_rpc("Player.GoTo", {"playerid": pid, "to": "next" if c == "proximo" else "previous"})
    elif c == "legenda" and v is not None:
        kodi_rpc("Player.SetSubtitle", {"playerid": pid, "subtitle": v, "enable": True})
    elif c == "legenda_desligar":
        kodi_rpc("Player.SetSubtitle", {"playerid": pid, "subtitle": "off"})
    elif c == "legenda_ligar":
        kodi_rpc("Player.SetSubtitle", {"playerid": pid, "subtitle": "on"})
    elif c == "legenda_proxima":
        kodi_rpc("Player.SetSubtitle", {"playerid": pid, "subtitle": "next", "enable": True})
    elif c == "audio" and v is not None:
        kodi_rpc("Player.SetAudioStream", {"playerid": pid, "stream": v})
    elif c == "audio_proximo":
        kodi_rpc("Player.SetAudioStream", {"playerid": pid, "stream": "next"})
    return None


CONFIRMA = {"pausar": "Pausado", "continuar": "Continuando", "parar": "Parado",
            "proximo": "Próximo episódio", "anterior": "Episódio anterior",
            "legenda_desligar": "Legenda desligada", "legenda_ligar": "Legenda ligada",
            "legenda_proxima": "Próxima legenda", "audio_proximo": "Próximo áudio",
            "volume_mais": "Volume +{v}", "volume_menos": "Volume −{v}", "volume": "Volume {v}",
            "mudo": "Mudo", "tirar_mudo": "Som ligado", "desligar": "Desligando a TV",
            "ligar": "Ligando a TV", "hdmi": "TV na HDMI {v}", "kodi": "De volta ao Kodi",
            "avancar": "+{v} s", "voltar": "−{v} s"}


def _confirmacao(a, tocando):
    c, v = a.get("comando"), a.get("valor")
    if c in ("legenda", "audio") and tocando:
        for f in tocando.get("legendas" if c == "legenda" else "audios") or []:
            if f["indice"] == v:
                return f'{"Legenda" if c == "legenda" else "Áudio"}: {f.get("idioma") or ""} {f.get("nome") or ""}'.strip()
    if c == "app":
        return f'Abrindo {a.get("app")}'
    return CONFIRMA.get(c, "").format(v=v if v is not None else "") or None


def _tv(a, tv, volta_ao_kodi):
    c, v = a.get("comando"), a.get("valor")
    if c == "volume" and v is not None:
        tv._rpc("audio", "setAudioVolume", [{"target": "speaker", "volume": str(max(0, min(100, v)))}])
    elif c in ("volume_mais", "volume_menos"):
        passo = abs(v or 5)
        tv._rpc("audio", "setAudioVolume", [{"target": "speaker",
                                             "volume": f'{"+" if c == "volume_mais" else "-"}{passo}'}])
    elif c in ("mudo", "tirar_mudo"):
        tv._rpc("audio", "setAudioMute", [{"status": c == "mudo"}])
    elif c in ("desligar", "ligar"):
        tv._rpc("system", "setPowerStatus", [{"status": c == "ligar"}])
    elif c == "hdmi" and v:
        tv.entrada(v)
    elif c == "kodi":
        volta_ao_kodi()
    elif c == "app":
        nome = (a.get("app") or "").lower()
        for app in _apps["lista"]:
            if app["title"].lower() == nome:
                if app["title"] == "Kodi":
                    volta_ao_kodi()
                else:
                    tv._rpc("appControl", "setActiveApp", [{"uri": app["uri"]}])
                return None
        return f'App "{a.get("app")}" não encontrado na TV'
    return None


def ouvir_e_executar(kodi_rpc, kodi_aviso, tv, modo_tv, volta_ao_kodi, cancelar=None, espera=0.0):
    """Fluxo completo. modo_tv: bool (o controle está na TV); volta_ao_kodi(): põe a TV na HDMI do Kodi."""
    wav = grava(cancelar, lambda: bipe("ouvindo"), espera)
    if cancelar is not None and cancelar.is_set():
        _log("cancelado (microfone duas vezes)")
        return
    if not wav:
        bipe("erro")
        kodi_aviso("🎤 Assistente", "Não ouvi nada", 2500)
        _log("sem fala")
        return
    t0 = time.monotonic()
    try:
        _atualiza_biblioteca(kodi_rpc)
    except Exception as ex:
        _log("biblioteca falhou:", ex)
    _atualiza_apps(tv)
    pid, tocando = _tocando(kodi_rpc)
    apps = [a["title"] for a in _apps["lista"]]

    try:  # guarda as últimas 10 falas para depurar o reconhecimento
        os.makedirs(GRAVACOES, exist_ok=True)
        velhas = sorted(os.listdir(GRAVACOES))
        for v in velhas[:-9]:
            os.remove(os.path.join(GRAVACOES, v))
        with open(os.path.join(GRAVACOES, time.strftime("%Y%m%d-%H%M%S.wav")), "wb") as f:
            f.write(wav)
    except OSError as ex:
        _log("não guardou a gravação:", ex)
    texto, origem, r = None, "", None
    try:
        texto = transcreve(wav)
    except Exception as ex:
        _log("Whisper falhou, vou mandar o áudio ao Gemini:", ex)
    t_voz = time.monotonic() - t0

    if texto is not None:
        if not texto:
            bipe("erro")
            kodi_aviso("🎤 Assistente", "Não entendi, pode repetir?", 3000)
            _log(f"({t_voz:.1f}s) Whisper não reconheceu fala")
            return
        acoes = comandos.interpreta(texto, {"tocando": tocando, "filmes": list(_biblioteca["filmes"]),
                                            "series": list(_biblioteca["series"]), "apps": apps})
        if acoes is not None:
            r, origem = {"transcricao": texto, "acoes": acoes, "resposta": ""}, "regras"
        else:
            try:
                fala, entendeu = _home_assistant(texto)
                if entendeu:
                    r, origem = {"transcricao": texto, "acoes": [], "resposta": fala}, "Home Assistant"
            except Exception as ex:
                _log("Home Assistant falhou:", ex)

    if r is None:  # pergunta, pedido complicado, ou Whisper fora do ar
        kodi_aviso("🎤 " + (texto or "Assistente")[:60], "Pensando…", 8000)
        contexto = {
            "agora": time.strftime("%d/%m/%Y %H:%M"),
            "controle_esta_em": "TV" if modo_tv else "Kodi",
            "tocando_no_kodi": tocando,
            "filmes": sorted(_biblioteca["filmes"]),
            "series": sorted(_biblioteca["series"]),
            "apps_da_tv": apps,
        }
        try:
            r = _gemini(contexto, wav=None if texto else wav, texto=texto)
            origem = "Gemini"
            if texto:
                r["transcricao"] = texto
        except Exception as ex:
            _log("Gemini falhou:", ex)
            bipe("erro")
            kodi_aviso("🎤 " + (texto or "Assistente")[:60], "O Gemini não respondeu, tente de novo", 4000)
            return

    _log(f"({t_voz:.1f}s voz, {time.monotonic() - t0:.1f}s total, {origem}) ouviu: "
         f"{r.get('transcricao')!r} → {r.get('acoes')} {r.get('resposta')!r}")
    mensagens = []
    tocou = False
    for a in r.get("acoes") or []:
        try:
            tipo = a.get("tipo")
            if tipo == "tocar":
                mensagens.append(_tocar(a, kodi_rpc))
                tocou = True
            elif tipo == "reproducao":
                mensagens.append(_reproducao(a, kodi_rpc, pid) or _confirmacao(a, tocando))
            elif tipo == "tv":
                mensagens.append(_tv(a, tv, volta_ao_kodi) or _confirmacao(a, tocando))
            elif tipo == "aviso":
                mensagens.append(a.get("texto"))
            elif tipo == "casa":
                resp, _ = _home_assistant(a.get("texto") or r.get("transcricao") or "")
                _log("Home Assistant:", resp)
                mensagens.append(resp)
        except Exception as ex:
            _log("ação falhou:", a, ex)
            mensagens.append("Não consegui fazer isso")
    if tocou and modo_tv:
        volta_ao_kodi()
    extras = [m for m in mensagens if m]
    resposta = " · ".join(extras) if extras else (r.get("resposta") or "Ok")
    if not r.get("acoes") and "entendi" in resposta.lower():
        bipe("erro")
    kodi_aviso("🎤 " + (r.get("transcricao") or "Assistente")[:60], resposta[:220], 6000)


threading.Thread(target=aquece, daemon=True).start()
