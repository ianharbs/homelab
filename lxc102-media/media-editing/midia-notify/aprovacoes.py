"""Grupo ✅ Aprovações no WhatsApp (só o Ian): pedidos pendentes do Seerr e avisos de prazo.

- Seerr (webhook "MEDIA_PENDING") -> mensagem com capa; o Ian RESPONDE à mensagem com "ok" (aprova) ou
  "não" (recusa) e o bot faz isso no Seerr.
- Prazo de convidado vencendo -> o Ian responde "manter" para o título ficar de vez.
- /prazos no grupo lista o que tem prazo.
A ligação mensagem -> pedido/prazo fica no kv (wa_msg_<id da mensagem>).
"""
import json
import re
import threading
import time

from common import E, http, kv_get, kv_set, log, norm
import prazo
import whatsapp

SEERR, SEERR_KEY = E("SEERR_URL", "http://seerr:5055"), E("SEERR_API_KEY", "")
SIM = {"ok", "sim", "aprovar", "aprova", "aprovado", "pode", "s", "👍", "✅", "👌", "❤"}
NAO = {"nao", "não", "recusar", "recusa", "recusado", "n", "👎", "❌"}
MANTER = {"manter", "mantem", "mantém", "fica", "deixa", "📌", "👍", "✅", "❤"}


def _send(text, photo=None):
    """Manda no grupo Aprovações e devolve o id da mensagem (para casar a resposta)."""
    jid = whatsapp.group_of("aprovacoes")
    if not jid:
        log.info("[sem grupo aprovacoes] %s", text)
        return None
    msg = whatsapp.to_wa(text)
    men = whatsapp.mentions_of(text)
    try:
        if photo:
            try:
                data = http("GET", photo, raw=True, timeout=30)
                r = whatsapp.send_image(jid, msg, data=data, mentions=men)
                return (r.get("results") or {}).get("message_id")
            except Exception as e:
                log.warning("aprovações: capa falhou (%s)", e)
        r = whatsapp.send_text(jid, msg, mentions=men)
        return (r.get("results") or {}).get("message_id")
    except Exception as e:
        log.error("aprovações: envio falhou: %s", e)
        return None


def _link(mid, data):
    if mid:
        kv_set(f"wa_msg_{mid}", json.dumps(data))


def avisar(text):
    return _send(text)


def aviso_prazo(r, when):
    mid = _send(f"⏳ <b>{r['title']}</b> (pedido por {r['users']}) sai do catálogo em <b>{when}</b>.\n"
                f"Responda <b>manter</b> para ficar de vez.")
    _link(mid, {"tipo": "prazo", "kind": r["kind"], "arr_id": r["arr_id"], "title": r["title"]})


def on_seerr(p):
    """Webhook do Seerr (notifications/webhook, só MEDIA_PENDING e TEST)."""
    t = p.get("notification_type")
    if t == "TEST_NOTIFICATION":
        _send("🔔 <b>Teste do Seerr</b>\nPedidos que precisam de aprovação vão chegar aqui.")
        return
    if t != "MEDIA_PENDING":
        return
    req = p.get("request") or {}
    media = p.get("media") or {}
    user = req.get("requestedBy_username") or "?"
    tipo = "📺 série" if media.get("media_type") == "tv" else "🎬 filme"
    extra = {e.get("name"): e.get("value") for e in p.get("extra") or [] if isinstance(e, dict)}
    linhas = [f"🙋 {whatsapp.mencao(user)} pediu {tipo}: <b>{p.get('subject', '?')}</b>"]
    seasons = extra.get("Requested Seasons")
    if seasons:
        linhas.append(f"Temporadas: {seasons}")
    if user.lower() not in prazo.SEM_PRAZO:
        dias = prazo.DIAS["series" if media.get("media_type") == "tv" else "movie"]
        linhas.append(f"⏳ Se aprovar, fica {dias} dias no catálogo.")
    linhas.append("\nResponda <b>ok</b> para aprovar ou <b>não</b> para recusar.")
    mid = _send("\n".join(linhas), photo=p.get("image") or None)
    _link(mid, {"tipo": "pedido", "id": req.get("request_id"), "title": p.get("subject")})
    log.info("aprovações: pedido %s de %s (%s)", p.get("subject"), user, req.get("request_id"))


def on_cadastro(p):
    """Pedido de acesso ao Jellyfin (app cadastro, CT 109). evento = pedido | status."""
    esc = whatsapp.html.escape
    if p.get("evento") == "pedido":
        linhas = ["🎬 <b>Pedido de acesso ao Jellyfin</b>", f"Nome: <b>{esc(p.get('nome') or '?')}</b>"]
        if p.get("email"):
            linhas.append(f"E-mail: {esc(p['email'])}")
        if p.get("telefone"):
            linhas.append(f"WhatsApp: {esc(p['telefone'])}")
        if p.get("recado"):
            linhas.append(f"Recado: {esc(p['recado'])}")
        linhas.append(f"<i>#{p.get('id')} · {esc(p.get('criado') or '')} · IP {esc(p.get('ip') or '?')}</i>")
        linhas.append("\nResponda <b>ok</b> para aprovar ou <b>não</b> para rejeitar.")
        mid = _send("\n".join(linhas))
        _link(mid, {"tipo": "cadastro", "nome": p.get("nome"), "aprovar": p.get("aprovar_url"),
                    "rejeitar": p.get("rejeitar_url")})
        log.info("aprovações: pedido de acesso #%s de %s", p.get("id"), p.get("nome"))
    elif p.get("evento") == "status" and p.get("texto"):
        _send(p["texto"])
    elif p.get("evento") == "conta" and p.get("usuario") and p.get("telefone"):
        # em thread: resolve_jid espera entre tentativas e o cadastro só aguarda 20 s pela resposta
        threading.Thread(target=_conta_nova, args=(p,), daemon=True).start()


def _conta_nova(p):
    """Conta criada pelo cadastro: grava o telefone na tabela pessoa e manda o link da Comunidade no privado."""
    usuario, nome = p["usuario"], (p.get("nome") or "").strip() or None
    try:
        r = whatsapp.pessoa_set(usuario, p["telefone"], nome)
        if r.startswith("❌"):
            _send(f"⚠️ {whatsapp.html.escape(nome or usuario)} criou a conta, mas o número não tem WhatsApp: "
                  f"{whatsapp.html.escape(r[2:])}\nCorrija com /pessoa {whatsapp.html.escape(usuario)} &lt;telefone&gt;.")
            return
        time.sleep(3)
        r = whatsapp.convidar(usuario)
        _send(whatsapp.html.escape(r))
        log.info("cadastro: %s", r)
    except Exception as e:
        log.exception("cadastro: convite do WhatsApp para %s: %s", usuario, e)
        _send(f"⚠️ Falhou o convite do WhatsApp para {whatsapp.html.escape(usuario)}: {whatsapp.html.escape(str(e)[:150])}")


def on_reply(replied_to, text):
    """Resposta do Ian a uma mensagem do grupo. Devolve o texto de confirmação, ou None se não era comando."""
    if text.strip().lower() == "/prazos":
        return prazo.listar()
    data = kv_get(f"wa_msg_{replied_to}") if replied_to else None
    if not data:
        return None
    d = json.loads(data)
    w = norm(text).strip() or text.strip()
    w = re.sub(r"[.!]+$", "", w)
    if d["tipo"] == "pedido" and (w in SIM or w in NAO):
        acao = "approve" if w in SIM else "decline"
        try:
            http("POST", f"{SEERR}/api/v1/request/{d['id']}/{acao}", {"X-Api-Key": SEERR_KEY}, {})
        except Exception as e:
            return f"⚠️ O Seerr não aceitou: {e}"
        return f"✅ Aprovado: {d['title']}" if acao == "approve" else f"❌ Recusado: {d['title']}"
    if d["tipo"] == "cadastro" and (w in SIM or w in NAO):
        url = d["aprovar"] if w in SIM else d["rejeitar"]
        try:  # mesmo link assinado do botão (pela URL pública; o CT 109 não aceita conexão da LAN)
            http("POST", url, {"User-Agent": "midia-notify"}, {}, timeout=60, raw=True)
        except Exception as e:
            return f"⚠️ O cadastro não aceitou: {e}"
        return f"✅ Aprovado: {d['nome']} (convite na página do pedido)" if w in SIM else f"❌ Rejeitado: {d['nome']}"
    if d["tipo"] == "prazo" and w in MANTER:
        prazo.manter(d["kind"], d["arr_id"])
        return f"📌 {d['title']} fica no catálogo de vez."
    return None

