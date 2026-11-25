"""print3d-agents: bot do negócio no Telegram + agenda dos agentes.

Comandos (no grupo do negócio, só usuários em ALLOWED_USERS):
  /curar [termo]       Curador: modelos em alta (ou por termo) com licença comercial → Aprovar/Recusar
  /anunciar <texto>    Anunciante: rascunho de anúncio ML/Shopee para um produto descrito
  /pedido <texto>      Pedidos: texto livre ("João, 2 suportes pretos, R$ 90, entrega sexta") → cartão no Vikunja
  /estoque             Estoque: confere Spoolman + InvenTree agora
  /custo               responda a uma mensagem com STL/3MF/G-code, ou mande o arquivo com /custo na legenda
  /relatorio           Gerente: resumo da semana
  /config [chave valor] mostra ou muda a configuração (ex.: /config markup 2.8)
Texto livre no tópico Pedidos vira pedido; no Geral, conversa com a IA.

Agenda (America/Sao_Paulo): estoque 08:00 todo dia; curadoria seg 09:00; relatório seg 08:30;
perguntas/pedidos do ML a cada 5 min (só com credenciais)."""
import datetime as dt
import json
import logging
import os
import threading
import time
import traceback

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agents
import custo
import mercado
import wa
from common import (ALLOWED, LLM_ON, STATE, TG_TOKEN, TOPICS, brl, config, db, edit_buttons, esc, forum_id,
                    kind_of_thread, kv_get, kv_set, log, post, reply, save_config, tg, tg_file, topic_id)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def safe(fn, *a, where=None, chat=None, thread=None):
    try:
        return fn(*a)
    except Exception as e:
        log.error("%s: %s\n%s", where or fn.__name__, e, traceback.format_exc())
        if chat:
            reply(chat, thread, f"⚠️ {esc(where or fn.__name__)} falhou: {esc(e)}")


def bg(fn, *a, **kw):
    threading.Thread(target=safe, args=(fn, *a), kwargs=kw, daemon=True).start()


# ------------------------------------------------------------------ agenda
JOBS = [  # (nome, dias da semana 0=seg ou None, "HH:MM", função)
    ("estoque", None, "08:00", agents.estoque),
    ("snapshot", None, "23:50", agents.snapshot_filamento),
    ("relatorio", {0}, "08:30", agents.relatorio),
    ("curadoria", {0}, "09:00", agents.curar),
]


def scheduler():
    last_ml = 0
    while True:
        now = dt.datetime.now()
        for name, days, hhmm, fn in JOBS:
            key = f"job_{name}"
            if (days is None or now.weekday() in days) and now.strftime("%H:%M") >= hhmm \
                    and kv_get(key) != now.date().isoformat():
                kv_set(key, now.date().isoformat())
                log.info("agenda: %s", name)
                safe(fn, where=name)
        if mercado.ml_on() and time.time() - last_ml > 300:
            last_ml = time.time()
            safe(mercado.perguntas, where="perguntas ML")
            safe(mercado.pedidos_ml, where="pedidos ML")
        time.sleep(30)


# ------------------------------------------------------------------ Telegram
def allowed(user):
    uid = str((user or {}).get("id"))
    return not ALLOWED or uid in ALLOWED or bool(wa.WA_OWNER and uid == wa.WA_OWNER)


def maybe_adopt(chat, user):
    """Primeiro grupo com Tópicos onde um usuário autorizado fala vira a central; cria os tópicos."""
    if forum_id() or not chat.get("is_forum") or not allowed(user):
        return
    kv_set("forum_id", chat["id"])
    log.info("grupo adotado: %s (%s)", chat.get("title"), chat["id"])
    for k in TOPICS:
        topic_id(k)
    post("geral", "👋 Olá! Sou a equipe de agentes do negócio. Cada tópico é um agente:\n"
         "🔎 Curadoria · 📝 Anúncios · 📦 Estoque · 🧾 Pedidos · 📊 Relatórios\nMande /ajuda para ver os comandos.")


def on_file(msg, chat, thread, pid=None):
    doc = msg.get("document")
    name = doc.get("file_name", "arquivo")
    if not name.lower().endswith((".stl", ".3mf", ".gcode", ".gco")):
        return reply(chat, thread, "Mande um STL, 3MF ou G-code.")
    os.makedirs(f"{STATE}/tmp", exist_ok=True)
    path = f"{STATE}/tmp/{doc['file_unique_id']}-{os.path.basename(name)}"
    tg_file(doc["file_id"], path)
    a = custo.analyze([path])
    os.remove(path)
    if not a["grams"]:
        return reply(chat, thread, "Não consegui ler peso/tempo desse arquivo. " + esc("; ".join(a["notes"])))
    pr = custo.price(a["grams"], a["hours"])
    txt = f"🧮 <b>{esc(name)}</b>\n" + custo.summary(a["grams"], a["hours"], a["estimated"], pr)
    if pid:
        with db() as c:
            c.execute("UPDATE products SET grams=?, hours=?, estimated=?, cost=?, prices=?, updated=? WHERE id=?",
                      (a["grams"], a["hours"], int(a["estimated"]), pr["cost"], json.dumps(pr["prices"]), time.time(), pid))
        txt += "\n✅ Produto atualizado com esses valores."
    reply(chat, thread, txt)


def show_config(chat, thread):
    c = config()
    lines = [f"<code>{k}</code> = {v}" for k, v in c.items() if k != "channels"]
    lines += [f"<code>canal {k}</code> = {v['pct']}% + {brl(v['fixed'])} ({esc(v['name'])})" for k, v in c["channels"].items()]
    reply(chat, thread, "⚙️ <b>Configuração</b>\n" + "\n".join(lines) +
          "\n\nMudar: <code>/config markup 2.8</code> · canal: <code>/config canal ml 13 6</code>")


def set_config(args, chat, thread):
    c = config()
    if args[0] == "canal" and len(args) == 4 and args[1] in c["channels"]:
        c["channels"][args[1]].update(pct=float(args[2].replace(",", ".")), fixed=float(args[3].replace(",", ".")))
    elif len(args) == 2 and args[0] in c and not isinstance(c[args[0]], dict):
        c[args[0]] = type(c[args[0]])(float(args[1].replace(",", ".")))
    else:
        return reply(chat, thread, "Uso: /config chave valor  ou  /config canal ml|shopee|direto pct fixo")
    save_config(c)
    reply(chat, thread, "✅ Salvo.")


def on_message(msg):
    chat, user = msg["chat"], msg.get("from")
    maybe_adopt(chat, user)
    if str(chat["id"]) != str(forum_id()) and chat.get("type") not in ("private", "wa"):
        return
    if not allowed(user):
        return
    thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
    kind = kind_of_thread(thread)
    text = (msg.get("text") or msg.get("caption") or "").strip()
    cid = chat["id"]
    rep = msg.get("reply_to_message") or {}
    rep_pid = kv_get(f"msg_{rep.get('message_id')}") if rep else None

    if msg.get("document"):
        if text.startswith("/custo") or rep_pid or kind in ("anuncios", "curadoria", "geral"):
            return bg(on_file, msg, cid, thread, int(rep_pid) if rep_pid else None, where="custo", chat=cid, thread=thread)
    if not text:
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    if cmd in ("/ajuda", "/help", "/start"):
        return reply(cid, thread, esc(__doc__.split("\n\n")[1]))
    if cmd == "/curar":
        reply(cid, thread, "🔎 Procurando modelos…")
        return bg(agents.curar, arg or None, where="curadoria", chat=cid, thread=thread)
    if cmd == "/anunciar":
        if not arg:
            return reply(cid, thread, "Uso: /anunciar <descrição do produto> (ou o botão 📝 de um produto aprovado)")
        return bg(agents.anunciar, None, arg, where="anúncio", chat=cid, thread=thread)
    if cmd == "/pedido" or (kind == "pedidos" and not text.startswith("/")):
        body = arg if cmd == "/pedido" else text
        if body:
            return bg(agents.pedido, body, where="pedido", chat=cid, thread=thread)
    if cmd == "/estoque":
        return bg(agents.estoque, True, where="estoque", chat=cid, thread=thread)
    if cmd == "/relatorio":
        return bg(agents.relatorio, where="relatório", chat=cid, thread=thread)
    if cmd == "/config":
        return show_config(cid, thread) if not arg else set_config(arg.split(), cid, thread)
    if cmd == "/custo":
        return reply(cid, thread, "Mande o arquivo (STL/3MF/G-code) com /custo na legenda.")
    if not text.startswith("/") and kind == "geral" and LLM_ON:
        return bg(lambda: reply(cid, thread, esc(agents.conversa(text))), where="conversa", chat=cid, thread=thread)


def on_callback(cb):
    user, msg = cb.get("from"), cb.get("message") or {}
    if not allowed(user):
        return tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Sem permissão"}) if cb.get("id") else None
    if cb.get("id"):  # no WhatsApp não há callback para responder
        tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Ok, trabalhando…"})
    chat, mid = msg.get("chat", {}).get("id"), msg.get("message_id")
    thread = msg.get("message_thread_id")
    agent, action, ident = (cb.get("data", "") + "::").split(":")[:3]
    ident = int(ident) if ident.isdigit() else None

    def run(fn, done_note, *a):
        edit_buttons(chat, mid)
        out = fn(*a)
        if out or done_note:
            reply(chat, thread, out or done_note)

    if agent == "cur" and action == "ok":
        bg(run, agents.aprovar_candidato, None, ident, where="aprovar modelo", chat=chat, thread=thread)
    elif agent == "cur" and action == "no":
        with db() as c:
            c.execute("UPDATE candidates SET status='recusado' WHERE id=?", (ident,))
        edit_buttons(chat, mid, "❌ Recusado — não aparece de novo.")
    elif agent == "anu" and action == "new":
        bg(agents.anunciar, ident, where="anúncio", chat=chat, thread=thread)
    elif agent == "anu" and action == "ok":
        bg(run, agents.aprovar_anuncio, None, ident, where="aprovar anúncio", chat=chat, thread=thread)
    elif agent == "anu" and action == "redo":
        with db() as c:
            d = c.execute("SELECT product_id, data FROM drafts WHERE id=?", (ident,)).fetchone()
            c.execute("UPDATE drafts SET status='refeito' WHERE id=?", (ident,))
        edit_buttons(chat, mid)
        bg(agents.anunciar, d["product_id"], json.loads(d["data"]).get("texto"), where="anúncio", chat=chat, thread=thread)
    elif agent == "anu" and action == "no":
        with db() as c:
            c.execute("UPDATE drafts SET status='descartado' WHERE id=?", (ident,))
        edit_buttons(chat, mid, "🗑 Descartado.")
    elif agent == "mlq" and action == "ok":
        bg(run, mercado.responder, None, ident, where="responder ML", chat=chat, thread=thread)
    elif agent == "mlq" and action == "no":
        edit_buttons(chat, mid, "👍 Ok, você responde no app.")


def telegram_loop():
    if os.environ.get("TELEGRAM_OFF") == "1":  # desde 25/11/2026 tudo pelo WhatsApp
        log.info("Telegram desligado (TELEGRAM_OFF=1)")
        return
    if not TG_TOKEN:
        log.warning("TELEGRAM_TOKEN vazio: bot desligado (agenda roda e manda tudo para o log)")
        return
    me = tg("getMe")["result"]
    log.info("bot @%s ouvindo (IA %s, grupo %s)", me["username"], "ligada" if LLM_ON else "desligada", forum_id() or "ainda não adotado")
    offset = int(kv_get("tg_offset", "0"))
    while True:
        try:
            ups = tg("getUpdates", {"offset": offset, "timeout": 30,
                                    "allowed_updates": ["message", "callback_query", "my_chat_member"]}, timeout=45)["result"]
        except Exception as e:
            log.warning("getUpdates: %s", e)
            time.sleep(10)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            kv_set("tg_offset", offset)
            if "message" in u:
                safe(on_message, u["message"], where="mensagem")
            elif "callback_query" in u:
                safe(on_callback, u["callback_query"], where="botão")
            elif "my_chat_member" in u:
                m = u["my_chat_member"]
                if m["new_chat_member"]["status"] == "administrator":
                    safe(maybe_adopt, m["chat"], m.get("from"), where="adotar grupo")


# ---------------------------------------------------------------- WhatsApp (desde 25/11/2026)
def wa_event(ev):
    """Evento do GOWA -> mesmo formato do Telegram (on_message / on_callback)."""
    p = ev.get("payload") or {}
    jid = p.get("chat_id") or ""
    if p.get("is_from_me"):
        return
    kind = wa.kind_of_group(jid)
    privado_do_ian = jid.endswith("@s.whatsapp.net") and wa.num(p.get("from")) == wa.WA_OWNER
    if not kind and not privado_do_ian:
        return  # grupos do iHome e o resto: não é com a gente
    user = {"id": wa.num(p.get("from")), "first_name": p.get("sender_display_name") or p.get("from_name")}
    chat = {"id": jid, "type": "wa"}
    if ev.get("event") == "message.reaction":
        data = wa.botao(p.get("reacted_message_id"), p.get("reaction") or "")
        if data:
            on_callback({"id": None, "from": user, "data": data,
                         "message": {"chat": chat, "message_id": p.get("reacted_message_id"), "message_thread_id": kind}})
        return
    text = (p.get("body") or "").strip()
    rid = p.get("replied_to_id")
    if rid and text.isdigit():
        data = wa.botao(rid, text)
        if data:
            return on_callback({"id": None, "from": user, "data": data,
                                "message": {"chat": chat, "message_id": rid, "message_thread_id": kind}})
    msg = {"chat": chat, "from": user, "message_id": p.get("id"), "text": text,
           "is_topic_message": bool(kind), "message_thread_id": kind or "geral"}
    if rid:
        msg["reply_to_message"] = {"message_id": rid}
    doc = p.get("document") or {}
    if doc:
        name = doc.get("filename") or doc.get("file_name") or doc.get("title") or "arquivo"
        msg["document"] = {"file_name": name, "file_id": f"wa:{p.get('id')}|{jid}", "file_unique_id": p.get("id")}
        msg["caption"] = doc.get("caption") or text
    on_message(msg)


class WAHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.split("?")[0] != "/whatsapp" or not wa.check_sig(raw, self.headers.get("X-Hub-Signature-256")):
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        try:
            ev = json.loads(raw or b"{}")
        except ValueError:
            return
        if ev.get("event") in ("message", "message.reaction"):
            threading.Thread(target=safe, args=(wa_event, ev), kwargs={"where": "whatsapp"}, daemon=True).start()


def whatsapp_server():
    if not wa.ON:
        return
    def criar_aos_poucos():  # um grupo a cada 10 min até ter os 6 (limite do WhatsApp p/ número novo)
        while True:
            try:
                g = wa.create_groups(max_new=1)
                if len(g) == len(TOPICS):
                    log.info("whatsapp: grupos prontos %s", g)
                    return
            except Exception as e:
                log.warning("whatsapp: criar grupos (tento de novo em 10 min): %s", e)
            time.sleep(600)
    threading.Thread(target=criar_aos_poucos, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("WA_PORT", "8099"))), WAHandler).serve_forever()


if __name__ == "__main__":
    os.makedirs(STATE, exist_ok=True)
    db().close()
    config()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=whatsapp_server, daemon=True).start()
    telegram_loop()
    while True:      # sem Telegram: só a agenda
        time.sleep(3600)
