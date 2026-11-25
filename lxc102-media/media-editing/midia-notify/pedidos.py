"""Pedidos pelo WhatsApp (grupo 🤖 Status): "quero o filme Duna", "/pedir Severance temporada 2".

1. procura no Seerr e responde com até 3 opções (ano, tipo, se já está disponível/pedido);
2. a pessoa RESPONDE à mensagem com o número;
3. o pedido é feito no Seerr em nome dela (telefone -> tabela pessoa -> usuário do Seerr).
Pedido de convidado fica pendente e cai no grupo ✅ Aprovações (webhook do Seerr); Ian/Ana aprovam direto.
Número sem cadastro não pede (o Ian cadastra com /pessoa).
"""
import json
import re
import urllib.parse

from common import E, http, kv_get, kv_set, log
import prazo
import whatsapp

SEERR, SEERR_KEY = E("SEERR_URL", "http://seerr:5055"), E("SEERR_API_KEY", "")
GATILHO = re.compile(r"^\s*(/pedir|pedir|pede|pe[cç]a|quero|queria|adiciona|adicionar|baixa|baixar)\b\s*", re.I)
STATUS = {5: "✅ já disponível", 4: "🟡 parte disponível", 3: "⏬ já está baixando", 2: "⏳ já pedido, aguardando"}


def eh_pedido(text):
    return bool(GATILHO.match(text or ""))


def _seerr(method, path, body=None):
    return http(method, f"{SEERR}/api/v1{path}", {"X-Api-Key": SEERR_KEY}, body, timeout=30)


def _seerr_user(usuario):
    for u in _seerr("GET", "/user?take=200").get("results", []):
        if (u.get("jellyfinUsername") or u.get("username") or "").lower() == usuario.lower():
            return u
    return None


def _quem(p):
    """Usuário (Jellyfin/Seerr) de quem mandou a mensagem, pelo telefone."""
    if p.get("is_from_me"):  # modo antigo: conta do bot = número do Ian
        return whatsapp.pessoa_por_jid(whatsapp.my_jid())
    return whatsapp.pessoa_por_jid(whatsapp._num(p.get("from")))


def pedir(p, text):
    """Primeiro passo: busca e oferece opções. Devolve (texto_da_resposta, dados_para_ligar_à_resposta)."""
    pessoa = _quem(p)
    if not pessoa:
        return ("Seu número ainda não está cadastrado para fazer pedidos. Peça ao Ian para te incluir. 🙂", None)
    q = GATILHO.sub("", text).strip()
    tipo = None
    m = re.match(r"^(o |a )?(filme|s[eé]rie|serie|desenho|anime)\s+", q, re.I)
    if m:
        tipo = "movie" if m.group(2).lower() == "filme" else "tv"
        q = q[m.end():]
    temporada = None
    m = re.search(r"\b(?:temporada|temp\.?|t|season|s)\s*(\d{1,2})\b", q, re.I)
    if m:
        temporada, tipo = int(m.group(1)), "tv"
        q = (q[:m.start()] + q[m.end():]).strip()
    q = re.sub(r"^(o|a|os|as)\s+", "", q.strip(" ?!.,"), flags=re.I)
    if len(q) < 2:
        return ("Me diga o nome, por exemplo: \"quero o filme Duna\" ou \"/pedir Severance temporada 2\".", None)
    # o Seerr recusa espaço como "+" (400): precisa %20
    res = _seerr("GET", f"/search?query={urllib.parse.quote(q, safe='')}&page=1&language=pt-BR")
    ops = [r for r in res.get("results", []) if r.get("mediaType") in ("movie", "tv")
           and (tipo is None or r["mediaType"] == tipo)][:3]
    if not ops:
        return (f"Não achei \"{q}\" no Seerr. Tente com o nome original (em inglês).", None)
    linhas = [f"🔎 Achei isto para *{q}*:"]
    dados = []
    for i, r in enumerate(ops, 1):
        nome = r.get("title") or r.get("name")
        ano = (r.get("releaseDate") or r.get("firstAirDate") or "")[:4]
        st = STATUS.get((r.get("mediaInfo") or {}).get("status"), "")
        linhas.append(f"{i}. {'🎬' if r['mediaType'] == 'movie' else '📺'} {nome}" + (f" ({ano})" if ano else "")
                      + (f" — {st}" if st else ""))
        dados.append({"type": r["mediaType"], "id": r["id"], "nome": nome, "ano": ano,
                      "status": (r.get("mediaInfo") or {}).get("status")})
    alvo = f" (temporada {temporada})" if temporada else ""
    linhas.append(f"\nResponda esta mensagem com o *número* para pedir{alvo}.")
    return ("\n".join(linhas), {"tipo": "busca", "usuario": pessoa["usuario"], "temporada": temporada, "ops": dados})


def escolher(replied_to, text, p):
    """Segundo passo: a pessoa respondeu a uma mensagem de opções com um número. None se não era isso."""
    data = kv_get(f"wa_msg_{replied_to}") if replied_to else None
    if not data:
        return None
    d = json.loads(data)
    if d.get("tipo") != "busca":
        return None
    m = re.fullmatch(r"\s*(\d)\s*[.)]?\s*", text or "")
    if not m:
        return None
    pessoa = _quem(p)
    if not pessoa or pessoa["usuario"] != d["usuario"]:
        return "Só quem fez a busca pode escolher. Faça a sua com \"quero ...\". 🙂"
    n = int(m.group(1))
    if not 1 <= n <= len(d["ops"]):
        return f"Escolha um número de 1 a {len(d['ops'])}."
    op = d["ops"][n - 1]
    if op["status"] in (5,):
        return f"*{op['nome']}* já está disponível no Jellyfin! 🍿"
    if op["status"] in (2, 3):
        return f"*{op['nome']}* já foi pedido e está a caminho. Aviso no grupo quando chegar."
    u = _seerr_user(d["usuario"])
    if not u:
        return "Não achei seu usuário no Seerr. Avise o Ian."
    body = {"mediaType": op["type"], "mediaId": op["id"], "userId": u["id"]}
    if op["type"] == "tv":
        body["seasons"] = [d["temporada"]] if d.get("temporada") else "all"
    try:
        r = _seerr("POST", "/request", body)
    except Exception as e:
        log.warning("pedido %s por %s: %s", op["nome"], d["usuario"], e)
        return f"⚠️ O Seerr não aceitou o pedido: {e}"
    log.info("pedido pelo WhatsApp: %s (%s) por %s, status %s", op["nome"], op["type"], d["usuario"], r.get("status"))
    nome = f"*{op['nome']}*" + (f" ({op['ano']})" if op["ano"] else "")
    if r.get("status") == 2:
        return f"✅ Pedido feito e aprovado: {nome}. Aviso aqui no grupo quando estiver disponível."
    extra = ""
    if d["usuario"].lower() not in prazo.SEM_PRAZO:
        dias = prazo.DIAS["series" if op["type"] == "tv" else "movie"]
        extra = f"\n⏳ Depois de disponível, fica {dias} dias no catálogo."
    return f"📨 Pedido feito: {nome}. Agora é só esperar a aprovação do Ian.{extra}"


def ligar(mid, dados):
    if mid and dados:
        kv_set(f"wa_msg_{mid}", json.dumps(dados))


def tem_numero(text):
    return bool(re.fullmatch(r"\s*\d\s*[.)]?\s*", text or ""))

