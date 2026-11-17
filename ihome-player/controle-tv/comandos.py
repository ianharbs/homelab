"""Regras locais do assistente: texto transcrito → ações (mesmo formato que o Gemini devolve).

interpreta(texto, ctx) devolve uma lista de ações, ou None quando não reconhece o pedido
(aí ele segue para o Home Assistant e depois para o Gemini).
ctx: {"tocando": dict|None, "filmes": [títulos], "series": [títulos], "apps": [títulos]}
"""
import difflib
import re
import unicodedata

IDIOMAS = {  # palavra falada → códigos que o Kodi costuma mostrar
    "portugues": ("por", "pt", "pob", "pt-br", "ptb", "portuguese", "portugues"),
    "brasileiro": ("por", "pt", "pob", "pt-br", "ptb", "portuguese"),
    "ingles": ("eng", "en", "english", "ingles"),
    "espanhol": ("spa", "es", "spanish", "espanol", "espanhol"),
    "japones": ("jpn", "ja", "japanese", "japones"),
    "frances": ("fre", "fra", "fr", "french", "frances"),
    "italiano": ("ita", "it", "italian", "italiano"),
    "alemao": ("ger", "deu", "de", "german", "alemao"),
    "coreano": ("kor", "ko", "korean", "coreano"),
}
APELIDOS_APP = {  # como as pessoas falam → título do app na TV
    "hbo": "HBO Max", "max": "HBO Max", "rbo": "HBO Max", "hobi": "HBO Max",
    "prime": "Prime Video", "amazon": "Prime Video", "amazon prime": "Prime Video",
    "paramount": "Paramount+", "youtube": "YouTube", "you tube": "YouTube",
    "claro": "Clarovideo", "claro video": "Clarovideo", "navegador": "Internet Browser",
    "loja": "Play Store", "play store": "Play Store", "musica": "Música",
}
NUMEROS = {
    "zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5,
    "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10, "onze": 11, "doze": 12, "treze": 13,
    "catorze": 14, "quatorze": 14, "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18,
    "dezenove": 19, "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50, "sessenta": 60,
    "setenta": 70, "oitenta": 80, "noventa": 90, "cem": 100,
}
VERBO_TOCAR = r"(?:toca|tocar|toque|assistir|assiste|ver|bota|botar|coloca|colocar|poe|por|passa|" \
              r"continua|continuar|continue|abre|abrir|abra|play)"


def norm(t):
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9+ ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _numero(t):
    """Primeiro número no texto (dígitos ou por extenso, inclusive 'vinte e cinco')."""
    m = re.search(r"\d+", t)
    if m:
        return int(m.group())
    total, achou = 0, False
    for p in t.split():
        if p in NUMEROS:
            total += NUMEROS[p]
            achou = True
        elif p == "e" and achou:
            continue
        elif achou:
            break
    return total if achou else None


def _parecido(consulta, opcoes, minimo, nota_tambem=False):
    """Melhor título de `opcoes` para `consulta` (aproximado), ou None."""
    q = norm(consulta)
    q = re.sub(r"^(?:(?:o|a|os|as|um|uma|do|da|de|dos|das|no|na|filme|serie|desenho|app|aplicativo)\s+)+", "", q)
    if not q:
        return (None, 0.0) if nota_tambem else None
    melhor, nota = None, 0.0
    for op in opcoes:
        n = norm(op)
        r = difflib.SequenceMatcher(None, q, n).ratio()
        if len(q) >= 4 and (n.startswith(q) or f" {q}" in f" {n}"):
            r = max(r, 0.9)
        sem_artigo = re.sub(r"^(o|a|os|as|the)\s+", "", n)
        r = max(r, difflib.SequenceMatcher(None, q, sem_artigo).ratio())
        if q == n or q == sem_artigo:
            r = 1.0
        if r > nota:
            melhor, nota = op, r
    if nota < minimo:
        melhor = None
    return (melhor, nota) if nota_tambem else melhor


def _faixa(lista, falado):
    """Índice da faixa (legenda/áudio) do idioma falado, ou None."""
    for palavra, codigos in IDIOMAS.items():
        if palavra in falado:
            for f in lista or []:
                campos = norm(f"{f.get('idioma') or ''} {f.get('nome') or ''}").split()
                if any(c in campos for c in codigos):
                    return f["indice"]
    return None


def _app(t, apps):
    for apelido, titulo in sorted(APELIDOS_APP.items(), key=lambda x: -len(x[0])):
        if re.search(rf"\b{apelido}\b", t) and titulo in apps:
            return titulo
    return _parecido(t, apps, 0.7)


def _tocar(resto, ctx):
    temporada = episodio = None
    m = re.search(r"\btemporada (\w+(?: e \w+)?)", resto)
    if m:
        temporada = _numero(m.group(1))
    m = re.search(r"\bepisodio (\w+(?: e \w+)?)", resto)
    if m:
        episodio = _numero(m.group(1))
    titulo = re.split(r"\b(?:temporada|episodio)\b", resto)[0].strip()
    serie, ns = _parecido(titulo, ctx["series"], 0.72, True)
    filme, nf = _parecido(titulo, ctx["filmes"], 0.72, True)
    quer_filme = re.search(r"\bfilme\b", resto) and filme
    if serie and not quer_filme and (temporada or episodio or not filme or "serie" in resto or ns >= nf):
        return {"tipo": "tocar", "categoria": "serie", "titulo": serie,
                "temporada": temporada, "episodio": episodio}
    if filme:
        return {"tipo": "tocar", "categoria": "filme", "titulo": filme}
    return None


# Comandos de uma palavra que o Whisper costuma errar (às vezes ouve em inglês: "pausar" → "Paul's Up").
# Se nada mais casar e a frase for curta, compara as letras com estes (aproximado).
CURTOS = {
    "pausa": "pausar", "pausar": "pausar", "pause": "pausar", "pausa ai": "pausar",
    "continua": "continuar", "continuar": "continuar", "despausa": "continuar", "play": "continuar",
    "parar": "parar", "para o video": "parar", "stop": "parar",
    "mudo": "mudo", "proximo": "proximo", "proximo episodio": "proximo", "anterior": "anterior",
}


def _curto(t):
    if not t or len(t.split()) > 3 or len(t.replace(" ", "")) < 4:
        return None
    junto = t.replace(" ", "")
    melhor, nota = None, 0.0
    for falado, cmd in CURTOS.items():
        r = difflib.SequenceMatcher(None, junto, falado.replace(" ", "")).ratio()
        if r > nota:
            melhor, nota = cmd, r
    return melhor if nota >= 0.6 else None


def interpreta(texto, ctx):
    acoes = _interpreta(texto, ctx)
    if acoes is None:
        cmd = _curto(norm(texto))
        if cmd == "mudo":
            acoes = [{"tipo": "tv", "comando": "mudo", "valor": None}]
        elif cmd:
            acoes = [{"tipo": "reproducao", "comando": cmd, "valor": None}]
    return acoes


def _interpreta(texto, ctx):
    t = norm(texto)
    t = re.sub(r"^(por favor|ei|oi|ok|assistente)\s+", "", t)
    t = re.sub(r"\s+por favor$", "", t)
    toc = ctx.get("tocando")
    tv = lambda c, v=None: [{"tipo": "tv", "comando": c, "valor": v}]
    rep = lambda c, v=None: [{"tipo": "reproducao", "comando": c, "valor": v}]

    # --- TV
    if re.search(r"\b(volta|voltar|vai|ir) (pro|para o|ao) kodi\b|\babr\w* o kodi\b|^kodi$", t):
        return tv("kodi")
    if re.search(r"\b(desliga|desligar|desligue)\b.*\b(tv|televisao)\b", t):
        return tv("desligar")
    if re.search(r"\b(liga|ligar|ligue)\b.*\b(tv|televisao)\b", t):
        return tv("ligar")
    if re.search(r"\b(tira|tirar|desativa)\b.*\bmudo\b|\bdesmut|\bvolta o som\b|\bliga o som\b", t):
        return tv("tirar_mudo")
    if re.search(r"\bmudo\b|\bsilencia|\bmuta\b|\bcorta o som\b|\bdesliga o som\b", t):
        return tv("mudo")
    if "volume" in t or re.search(r"\b(aumenta|abaixa|diminui|sobe|baixa)\b.*\bsom\b", t):
        n = _numero(t)
        if re.search(r"\b(aumenta|aumentar|sobe|subir|mais|alto)\b", t):
            return tv("volume_mais", n or 5)
        if re.search(r"\b(abaixa|abaixar|diminui|diminuir|baixa|menos|baixo)\b", t):
            return tv("volume_menos", n or 5)
        if n is not None:
            return tv("volume", n)
    m = re.search(r"\b(hdmi|entrada) (\w+)", t)
    if m and _numero(m.group(2)):
        return tv("hdmi", _numero(m.group(2)))

    # --- legenda / áudio do vídeo
    if "legenda" in t:
        if re.search(r"\b(tira|tirar|desliga|desligar|sem|remove|esconde)\b", t):
            return rep("legenda_desligar")
        if toc:
            i = _faixa(toc.get("legendas"), t)
            if i is not None:
                return rep("legenda", i)
            if re.search(r"\bproxima\b|\boutra\b|\btroca\b", t):
                return rep("legenda_proxima")
            if re.search(r"\b(liga|ligar|coloca|bota|mostra|ativa)\b", t) and not any(p in t for p in IDIOMAS):
                return rep("legenda_ligar")
            if any(p in t for p in IDIOMAS):
                return [{"tipo": "aviso", "texto": "Este vídeo não tem legenda nesse idioma"}]
        return None
    if re.search(r"\baudio\b|\bdublad|\bdublagem\b|\bidioma\b|\blingua\b", t) and toc:
        i = _faixa(toc.get("audios"), t)
        if i is None and re.search(r"\bdublad|\bdublagem\b", t):
            i = _faixa(toc.get("audios"), "portugues")
        if i is None and "original" in t and toc.get("audios"):
            i = toc["audios"][0]["indice"]
        if i is not None:
            return rep("audio", i)
        if re.search(r"\bproximo\b|\boutro\b|\btroca\b", t):
            return rep("audio_proximo")
        if any(p in t for p in IDIOMAS):
            return [{"tipo": "aviso", "texto": "Este vídeo não tem áudio nesse idioma"}]
        return None

    # --- reprodução
    if re.search(r"\b(proximo|seguinte)\b.*\bepisodio\b|\bepisodio seguinte\b|^proximo$", t):
        return rep("proximo")
    if re.search(r"\bepisodio anterior\b|^anterior$", t):
        return rep("anterior")
    m = re.search(r"\b(avanca|avancar|adianta|adiantar|pula|pular|volta|voltar|retrocede|retroceder|"
                  r"atrasa|atrasar)\b(.*)", t)
    if m and toc and not re.search(r"\b(pro|para o|ao) kodi\b", t):
        resto = m.group(2)
        n = _numero(resto)
        if "meio minuto" in resto:
            seg = 30
        elif n is not None and "minuto" in resto:
            seg = n * 60
        elif n is not None:
            seg = n
        elif "minuto" in resto:
            seg = 60
        else:
            seg = 30
        frente = m.group(1).startswith(("avanc", "adiant", "pul"))
        return rep("avancar" if frente else "voltar", seg)
    if re.fullmatch(r"(pausa|pausar|pause|pausa ai|pausa o video|pausa o filme|pausar o video|pausar o filme|"
                    r"para um pouco|espera)", t):
        return rep("pausar")
    if re.fullmatch(r"(continua|continuar|continue|play|despausa|despausar|retoma|retomar|volta a tocar|"
                    r"pode continuar|solta)", t):
        return rep("continuar")
    if re.fullmatch(r"(para|parar|pare|stop|para o video|para o filme|encerra|fecha o video)", t):
        return rep("parar")

    # --- abrir app / tocar título
    m = re.match(rf"{VERBO_TOCAR}\s+(?:o |a |os |as )?(?:app |aplicativo )?(.+)", t)
    if m:
        resto = m.group(1)
        app = _app(resto, ctx["apps"])
        quer_app = re.match(r"(abre|abrir|abra)\b", t) or "app" in t or "aplicativo" in t
        acao = _tocar(resto, ctx)
        if app and (quer_app or not acao):
            return [{"tipo": "tv", "comando": "app", "app": app}]
        if acao:
            return [acao]
    return None
