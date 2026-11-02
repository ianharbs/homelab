# -*- coding: utf-8 -*-
"""Skill "servidor": busca por voz no Music Assistant (via Home Assistant).

"Alexa, peça ao servidor para tocar Faroeste Caboclo de Legião Urbana"
  1. A Amazon manda o texto livre (slot AMAZON.SearchQuery) para cá.
  2. A assinatura e o horário do pedido são verificados (ask-sdk-webservice-support)
     e só o SKILL_ID configurado é aceito.
  3. Busca no MA pelo serviço music_assistant.search do HA e escolhe o melhor resultado.
  4. Responde "Tocando ..." e dispara (sem esperar) o script do HA que descobre a Echo
     chamada (last_called do Alexa Media Player) e manda o MA tocar nela.
"""
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.request
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.utils import is_intent_name, is_request_type
from ask_sdk_webservice_support.webservice_handler import WebserviceSkillHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("busca")


def secret(name, default=""):
    """Valor da variável; se apontar para um arquivo (Docker secret), o conteúdo dele."""
    val = os.environ.get(name, default)
    if val and os.path.isfile(val):
        with open(val, encoding="utf-8") as fh:
            return fh.read().strip()
    return val


HA_URL = os.environ.get("HA_URL", "http://192.168.10.60:8123").rstrip("/")
HA_TOKEN = secret("HA_TOKEN")
MA_ENTRY = os.environ.get("MA_CONFIG_ENTRY_ID", "")
SKILL_ID = os.environ.get("SKILL_ID", "")
PORT = int(os.environ.get("PORT", "5001"))
SCRIPT_TOCAR = os.environ.get("SCRIPT_TOCAR", "script.musica_tocar_na_echo")
SCRIPT_ALEATORIO = os.environ.get("SCRIPT_ALEATORIO", "script.musica_aleatoria")
SCRIPT_CONTROLE = os.environ.get("SCRIPT_CONTROLE", "script.musica_controle")

# Apelidos para nomes que a Alexa em pt-BR transcreve mal: {"o que ela entende": "o que buscar"}
try:
    APELIDOS = {k.lower(): v for k, v in json.loads(os.environ.get("APELIDOS", "{}")).items()}
except ValueError:
    APELIDOS = {}

TIPOS = {  # intent -> tipos aceitos no MA, rádio?
    "TocarIntent": (["artist", "album", "track", "playlist"], False),
    "TocarArtistaIntent": (["artist"], False),
    "TocarAlbumIntent": (["album"], False),
    "TocarMusicaIntent": (["track"], False),
    "TocarPlaylistIntent": (["playlist"], False),
    "TocarRadioIntent": (["artist", "track", "album"], True),
}
CHAVE = {"artist": "artists", "album": "albums", "track": "tracks", "playlist": "playlists"}
PREFERENCIA = {"artist": 0.06, "album": 0.04, "playlist": 0.03, "track": 0.0}  # desempate
NOME_TIPO = {"artist": "", "album": "o álbum ", "track": "", "playlist": "a playlist "}


# ---------------------------------------------------------------- Home Assistant
def ha(path, payload, timeout=6):
    req = urllib.request.Request(
        HA_URL + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + HA_TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def ma_search(texto, tipos):
    data = {"name": texto, "media_type": tipos, "limit": 8}
    if MA_ENTRY:
        data["config_entry_id"] = MA_ENTRY
    r = ha("/api/services/music_assistant/search?return_response", data)
    return (r or {}).get("service_response") or {}


def tocar(item, radio):
    """Dispara o script do HA sem esperar (script.turn_on retorna na hora)."""
    variaveis = {"media_id": item["uri"], "media_type": item["tipo"], "radio_mode": radio}
    ha("/api/services/script/turn_on", {"entity_id": SCRIPT_TOCAR, "variables": variaveis})


# ---------------------------------------------------------------- busca
def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"_automatic$", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parecido(a, b):
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def nota(q, item, tipo):
    nome = norm(item.get("name"))
    artistas = " ".join(norm(a.get("name")) for a in item.get("artists") or [])
    candidatos = [nome]
    if artistas:
        candidatos += [f"{nome} {artistas}", f"{nome} do {artistas}", f"{nome} de {artistas}",
                       f"{artistas} {nome}"]
    n = max(parecido(q, c) for c in candidatos)
    if q == nome:
        n += 0.2
    elif nome.startswith(q) or q.startswith(nome):
        n += 0.05
    if (item.get("uri") or "").startswith("library://"):
        n += 0.02  # prefere o que está na biblioteca (Navidrome) ao resultado bruto do provider
    return n + PREFERENCIA.get(tipo, 0)


def limpa(texto):
    """Tira o "no servidor" / "do servidor" que a pessoa pode falar no fim."""
    return re.sub(r"\s+(no|do|pelo|lá no)\s+(meu\s+)?servidor\s*$", "", texto.strip(), flags=re.I)


_ARTISTAS = {"lista": [], "quando": 0.0}
_ARTISTAS_LOCK = threading.Lock()


def artistas_biblioteca():
    """Todos os artistas da biblioteca do MA (cache de 1 h), para achar nomes mal ouvidos."""
    with _ARTISTAS_LOCK:
        if _ARTISTAS["lista"] and time.time() - _ARTISTAS["quando"] < 3600:
            return _ARTISTAS["lista"]
        lista, offset = [], 0
        while offset < 20000:
            data = {"media_type": "artist", "limit": 500, "offset": offset, "order_by": "name"}
            if MA_ENTRY:
                data["config_entry_id"] = MA_ENTRY
            r = ha("/api/services/music_assistant/get_library?return_response", data, timeout=20)
            itens = ((r or {}).get("service_response") or {}).get("items") or []
            lista += [{"name": i.get("name"), "uri": i.get("uri"), "n": norm(i.get("name"))} for i in itens if i.get("uri")]
            if len(itens) < 500:
                break
            offset += 500
        _ARTISTAS.update(lista=lista, quando=time.time())
        log.info("cache de artistas: %d", len(lista))
        return lista


CONECTOR = re.compile(r"\s+(?:de|do|da|dos|das)\s+")


def consultas(q):
    """'faroeste caboclo de legiao urbana' -> a frase inteira, sem o conector, e só o nome."""
    out = [q]
    for m in CONECTOR.finditer(q):
        esq, dir_ = q[:m.start()], q[m.end():]
        if esq and dir_:
            out += [f"{esq} {dir_}", esq]
    return list(dict.fromkeys(out))[:5]


NUM_EN = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
NUM_PT = ("zero um dois tres quatro cinco seis sete oito nove dez onze doze treze quatorze quinze dezesseis dezessete "
          "dezoito dezenove vinte").split()


def variantes(q):
    """A Alexa escreve números com algarismos ("o álbum ten" -> "10"): tenta também por extenso."""
    out = [q]
    if re.search(r"\b\d{1,2}\b", q):
        for nomes in (NUM_EN, NUM_PT):
            out.append(re.sub(r"\b(\d{1,2})\b",
                              lambda m: nomes[int(m.group(1))] if int(m.group(1)) < len(nomes) else m.group(1), q))
    return list(dict.fromkeys(out))


def melhor(texto, tipos):
    texto = limpa(texto)
    q0 = norm(APELIDOS.get(texto.lower().strip(), texto))
    vistos, achados = {}, []
    for q in variantes(q0):
        for consulta in consultas(q):
            res = ma_search(consulta, tipos)
            for tipo in tipos:
                for item in res.get(CHAVE[tipo]) or []:
                    n = nota(q, item, tipo)
                    uri = item.get("uri")
                    if uri in vistos and vistos[uri] >= n:
                        continue
                    vistos[uri] = n
                    achados.append((n, tipo, item))
            if achados and max(a[0] for a in achados) >= 0.9:
                break  # já tem um resultado muito bom; não gasta mais buscas
        if achados and max(a[0] for a in achados) >= 0.9:
            break
    q = q0
    if "artist" in tipos and (not achados or max(a[0] for a in achados) < 0.7):
        # Reserva: nome de artista parecido na biblioteca ("perl jem" -> Pearl Jam)
        try:
            n_art, art = max(((parecido(q, a["n"]), a) for a in artistas_biblioteca()), key=lambda x: x[0],
                             default=(0, None))
        except Exception:
            log.exception("falha ao carregar artistas")
            n_art, art = 0, None
        if art and n_art >= 0.7:
            achados.append((n_art + 0.01, "artist", {"name": art["name"], "uri": art["uri"]}))
    if not achados:
        log.info("busca %r tipos=%s -> nada", texto, tipos)
        return None
    achados.sort(key=lambda x: x[0], reverse=True)
    n, tipo, item = achados[0]
    log.info("busca %r tipos=%s -> %s %r (%.2f) %s", texto, tipos, tipo, item.get("name"), n, item.get("uri"))
    if n < 0.45:
        return None
    artistas = ", ".join(a.get("name") for a in item.get("artists") or [] if a.get("name"))
    return {"uri": item["uri"], "tipo": tipo, "nome": re.sub(r"_automatic$", "", item.get("name") or ""),
            "artistas": artistas}


def fala_tocando(item, radio):
    nome = item["nome"]
    if radio:
        return f"Tocando uma rádio a partir de {nome}."
    if item["tipo"] in ("track", "album") and item["artistas"]:
        return f"Tocando {NOME_TIPO[item['tipo']]}{nome}, de {item['artistas']}."
    return f"Tocando {NOME_TIPO[item['tipo']]}{nome}."


# ---------------------------------------------------------------- skill
sb = SkillBuilder()
sb.skill_id = SKILL_ID or None

PERGUNTA = "O que você quer ouvir? Pode falar uma banda, um álbum, uma música ou uma playlist."


@sb.request_handler(can_handle_func=is_request_type("LaunchRequest"))
def launch(h):
    return h.response_builder.speak(PERGUNTA).ask("O que você quer ouvir?").response


def _busca_intent(h):
    nome = h.request_envelope.request.intent.name
    return nome in TIPOS


@sb.request_handler(can_handle_func=_busca_intent)
def tocar_intent(h):
    intent = h.request_envelope.request.intent
    tipos, radio = TIPOS[intent.name]
    slot = (intent.slots or {}).get("busca")
    texto = (slot.value if slot else "") or ""
    if not texto.strip():
        return h.response_builder.speak(PERGUNTA).ask("O que você quer ouvir?").response
    try:
        item = melhor(texto, tipos)
    except Exception:
        log.exception("falha na busca %r", texto)
        return h.response_builder.speak("Não consegui falar com o Home Assistant agora.").set_should_end_session(True).response
    if not item:
        return (h.response_builder.speak(f"Não achei {texto} na sua biblioteca. O que mais você quer ouvir?")
                .ask("O que você quer ouvir?").response)
    try:
        tocar(item, radio)
    except Exception:
        log.exception("falha ao disparar o script")
        return h.response_builder.speak("Achei, mas não consegui mandar tocar.").set_should_end_session(True).response
    return h.response_builder.speak(fala_tocando(item, radio)).set_should_end_session(True).response


@sb.request_handler(can_handle_func=is_intent_name("AleatorioIntent"))
def aleatorio(h):
    try:
        ha("/api/services/script/turn_on", {"entity_id": SCRIPT_ALEATORIO})
    except Exception:
        log.exception("falha no aleatório")
        return h.response_builder.speak("Não consegui falar com o Home Assistant agora.").set_should_end_session(True).response
    return h.response_builder.speak("Tocando músicas aleatórias da sua biblioteca.").set_should_end_session(True).response


CONTROLES = {  # intent -> (ação do script do HA, fala)
    "ProximaIntent": ("next", "Próxima."),
    "AnteriorIntent": ("previous", "Voltando."),
    "PausarIntent": ("pause", "Pausado."),
    "ContinuarIntent": ("play", "Continuando."),
}


def controlar(h, acao, fala):
    """Próxima/anterior/pausa no player do MA da Echo chamada (script do HA acha qual)."""
    try:
        ha("/api/services/script/turn_on", {"entity_id": SCRIPT_CONTROLE, "variables": {"acao": acao}})
    except Exception:
        log.exception("falha no controle %s", acao)
        return h.response_builder.speak("Não consegui falar com o Home Assistant agora.").set_should_end_session(True).response
    return h.response_builder.speak(fala).set_should_end_session(True).response


@sb.request_handler(can_handle_func=lambda h: is_request_type("IntentRequest")(h)
                    and h.request_envelope.request.intent.name in CONTROLES)
def controle_intent(h):
    acao, fala = CONTROLES[h.request_envelope.request.intent.name]
    return controlar(h, acao, fala)


@sb.request_handler(can_handle_func=is_intent_name("AMAZON.HelpIntent"))
def ajuda(h):
    fala = ("Diga, por exemplo: tocar Legião Urbana, tocar o álbum Ten, tocar a música Faroeste Caboclo, "
            "tocar a playlist Noite de Festa, tocar rádio de Pearl Jam, ou tocar qualquer coisa. O que você quer ouvir?")
    return h.response_builder.speak(fala).ask("O que você quer ouvir?").response


@sb.request_handler(can_handle_func=lambda h: is_intent_name("AMAZON.StopIntent")(h)
                    or is_intent_name("AMAZON.CancelIntent")(h) or is_intent_name("AMAZON.NavigateHomeIntent")(h))
def parar(h):
    # "peça ao servidor para parar" (pedido único) = pausar a música; dentro da conversa = só sair
    sessao = h.request_envelope.session
    if is_intent_name("AMAZON.StopIntent")(h) and sessao is not None and sessao.new:
        return controlar(h, "pause", "Pausado.")
    return h.response_builder.speak("Tudo bem.").set_should_end_session(True).response


@sb.request_handler(can_handle_func=is_intent_name("AMAZON.FallbackIntent"))
def fallback(h):
    return h.response_builder.speak("Não entendi. " + PERGUNTA).ask("O que você quer ouvir?").response


@sb.request_handler(can_handle_func=is_request_type("SessionEndedRequest"))
def fim(h):
    return h.response_builder.response


@sb.exception_handler(can_handle_func=lambda h, e: True)
def erro(h, e):
    log.exception("erro no skill: %s", e)
    return h.response_builder.speak("Deu um erro aqui. Tente de novo.").set_should_end_session(True).response


webservice = WebserviceSkillHandler(skill=sb.create(), verify_signature=True, verify_timestamp=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, '{"ok": true}')
        self._send(404, '{"error": "not found"}')

    def do_POST(self):
        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho > 100_000:
            return self._send(413, '{"error": "too large"}')
        corpo = self.rfile.read(tamanho).decode("utf-8")
        cabecalhos = {k: v for k, v in self.headers.items()}
        try:
            resposta = webservice.verify_request_and_dispatch(cabecalhos, corpo)
        except Exception as e:  # assinatura/horário/skill_id inválidos
            log.warning("pedido recusado: %s", e)
            return self._send(400, '{"error": "bad request"}')
        self._send(200, json.dumps(resposta) if not isinstance(resposta, str) else resposta)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    if not HA_TOKEN:
        log.warning("HA_TOKEN vazio: a busca não vai funcionar")
    if not SKILL_ID:
        log.warning("SKILL_ID vazio: aceitando qualquer skill assinado pela Amazon")
    if HA_TOKEN:
        threading.Thread(target=lambda: artistas_biblioteca(), daemon=True).start()
    log.info("ouvindo em :%d", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
