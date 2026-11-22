"""telegram-agent: o Ian conversa com o Claude Code (no pve) pelo tópico Homelab do grupo iHome.

Fluxo:
  Telegram → statusbot.py do midia-notify (CT 102, único leitor do getUpdates do @iHomeSCbot) →
  POST http://192.168.10.2:8097/telegram?token=AGENT_TOKEN com o update (mensagem do Ian no tópico
  Homelab ou clique num botão "ag:…") → aqui roda `claude -p` em /root/homelab (mesmas memórias e
  settings das sessões no terminal) → resposta no mesmo tópico, respondendo à mensagem.

Conversa: responder (reply) a uma mensagem do agente continua a mesma sessão (--resume). Mensagem
nova sem reply = sessão nova; se for reply a outra mensagem (ex.: alerta do stack-watch), o texto dela
vai junto no pedido. /parar mata o que estiver rodando. Um pedido por vez (os outros entram na fila).

Segurança:
  - só aceita updates vindos do CT 102 (FORWARD_FROM) com AGENT_TOKEN e de ADMIN_IDS (Ian);
  - o Claude roda com --permission-mode auto (o classificador continua valendo) e um hook
    PreToolUse (confirm_hook.py) que, para comandos que mudam algo, pergunta aqui
    (POST 127.0.0.1:8098/confirm) e o agente manda botões ✅/❌ no Telegram; sem resposta em 10 min = não.

Segredos em /etc/telegram-agent.env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_THREAD_ID,
ADMIN_IDS, AGENT_TOKEN (o mesmo vai no secrets.env do midia-notify como AGENT_TOKEN).
"""
import html
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def load_env(path="/etc/telegram-agent.env"):
    cfg = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    cfg.update({k: v for k, v in os.environ.items() if v and k in cfg})
    return cfg


CFG = load_env()
TOKEN = CFG.get("TELEGRAM_TOKEN", "")
CHAT = CFG.get("TELEGRAM_CHAT_ID", "")
THREAD = CFG.get("TELEGRAM_THREAD_ID", "25")
ADMINS = {x.strip() for x in CFG.get("ADMIN_IDS", "").split(",") if x.strip()}
AGENT_TOKEN = CFG.get("AGENT_TOKEN", "")
LISTEN = CFG.get("LISTEN", "192.168.10.2")
PORT = int(CFG.get("PORT", "8097"))
CONFIRM_PORT = int(CFG.get("CONFIRM_PORT", "8098"))
FORWARD_FROM = {x.strip() for x in CFG.get("FORWARD_FROM", "192.168.10.20").split(",") if x.strip()}
WORKDIR = CFG.get("WORKDIR", "/root/homelab")
CLAUDE = CFG.get("CLAUDE_BIN", "/root/.local/bin/claude")
JOB_TIMEOUT = int(CFG.get("JOB_TIMEOUT_MIN", "30")) * 60
CONFIRM_TIMEOUT = int(CFG.get("CONFIRM_TIMEOUT_MIN", "10")) * 60
STATE_FILE = CFG.get("STATE_FILE", "/var/lib/telegram-agent/state.json")
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "confirm_hook.py")

SYSTEM = """Você está atendendo o Ian pelo Telegram (tópico Homelab do grupo iHome), não pelo terminal.
- Responda em português, curto e direto: é uma mensagem de celular. Sem tabelas; use no máximo
  **negrito**, `código`, blocos ``` e listas com "-". Mire em menos de 2.500 caracteres.
- Ele não vê os comandos nem as saídas, só a sua resposta final: diga o que achou, o que fez e o
  que falta, com números concretos.
- Comandos que mudam algo passam por um hook que pergunta ao Ian no Telegram (botões ✅/❌). Se ele
  negar, não tente de outro jeito: diga o que ia fazer e pare. A pergunta mostra o campo
  `description` do Bash: escreva-o SEMPRE em português, como frase para leigo dizendo o que vai
  fazer, em quê e por quê (ex.: "Apagar os logs antigos do Jellyfin no CT 102 para liberar 3 GB"). Consultar, ler logs e
  `docker restart`/`docker start` não pedem confirmação.
- Mudança grande, arriscada ou ambígua: investigue, explique o plano em poucas linhas e PARE,
  esperando ele responder (ele continua a conversa respondendo à sua mensagem).
- Não mande mensagem no Telegram por conta própria: a sua resposta final já vai para lá.
- Valem as memórias e regras de sempre (backups em /data/data/backup/<app>/, editar a config viva
  do HA à mão, commit só dos arquivos da tarefa, etc.)."""

LOCK = threading.Lock()
JOBS = queue.Queue()
CURRENT = {"proc": None, "job": None}
PENDING = {}  # id da confirmação → {"event", "ok", "msg_id"}


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- estado
def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (FileNotFoundError, ValueError):
        st = {}
    st.setdefault("msg_session", {})  # message_id do agente → session_id
    return st


STATE = load_state()


def save_state():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    # guarda só as 2000 mensagens mais recentes
    ms = STATE["msg_session"]
    if len(ms) > 2000:
        for k in sorted(ms, key=int)[:-2000]:
            del ms[k]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(STATE, f)
    os.replace(tmp, STATE_FILE)


def remember(msg_id, session):
    if msg_id and session:
        with LOCK:
            STATE["msg_session"][str(msg_id)] = session
            save_state()


# ---------------------------------------------------------------- Telegram
def tg(method, body, timeout=30):
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def md_to_html(s):
    """Markdown simples do Claude → HTML do Telegram."""
    out, pos = [], 0
    for m in re.finditer(r"```[^\n]*\n(.*?)```", s, flags=re.S):
        out.append(inline(s[pos:m.start()]))
        out.append("<pre>" + html.escape(m.group(1).rstrip("\n"), quote=False) + "</pre>")
        pos = m.end()
    out.append(inline(s[pos:]))
    return "".join(out).strip()


def inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", s, flags=re.M)
    s = re.sub(r"^(\s*)[-*]\s+", r"\1• ", s, flags=re.M)
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    return s


def chunks(text, size=3500):
    """Divide em pedaços ≤ size sem quebrar bloco ``` no meio (quando dá)."""
    parts, cur, in_code = [], "", False
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size and cur:
            if in_code:
                cur += "\n```"
            parts.append(cur)
            cur = "```\n" if in_code else ""
        cur += ("\n" if cur and not cur.endswith("\n") else "") + line
        if line.strip().startswith("```"):
            in_code = not in_code
    if cur.strip():
        parts.append(cur)
    return parts or ["(sem resposta)"]


def send(text, reply_to=None, edit=None, markup=None, raw_html=False):
    """Manda (ou edita) uma mensagem no tópico. Se o HTML for recusado, manda como texto puro."""
    body_html = text if raw_html else md_to_html(text)
    base = {"chat_id": CHAT, "disable_web_page_preview": True}
    if markup:
        base["reply_markup"] = markup
    plain = html.unescape(re.sub(r"<[^>]+>", "", body_html)) if raw_html else text
    for attempt in ("html", "plain"):
        body = dict(base, text=body_html if attempt == "html" else plain)
        if attempt == "html":
            body["parse_mode"] = "HTML"
        try:
            if edit:
                tg("editMessageText", dict(body, message_id=edit))
                return edit
            body["message_thread_id"] = int(THREAD)
            if reply_to:
                body["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            return tg("sendMessage", body)["result"]["message_id"]
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            if "message is not modified" in err:
                return edit
            log("telegram", attempt, e.code, err[:200])
            if attempt == "plain":
                raise
    return None


def typing_until(stop):
    while not stop.wait(5):
        try:
            tg("sendChatAction", {"chat_id": CHAT, "message_thread_id": int(THREAD), "action": "typing"})
        except Exception:
            pass


# ---------------------------------------------------------------- execução
def run_job(job):
    m = job["msg"]
    text = (m.get("text") or m.get("caption") or "").strip()
    rep = m.get("reply_to_message") or {}
    session = STATE["msg_session"].get(str(rep.get("message_id"))) if rep else None
    prompt = text
    if rep and not session:
        quoted = (rep.get("text") or rep.get("caption") or "").strip()
        if quoted:
            prompt = f"(Em resposta a esta mensagem do grupo:)\n<<<\n{quoted[:6000]}\n>>>\n\n{text}"
    placeholder = send("🔍 vendo…" if not session else "🔍 continuando…", reply_to=m["message_id"])
    remember(placeholder, session)
    job_id = uuid.uuid4().hex[:12]
    CURRENT["job"] = {"id": job_id, "reply_to": m["message_id"], "session": session}
    settings = {"hooks": {"PreToolUse": [{
        "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{"type": "command", "command": f"python3 {HOOK}", "timeout": CONFIRM_TIMEOUT + 60}]}]}}
    cmd = [CLAUDE, "-p", prompt, "--output-format", "json", "--permission-mode", "auto",
           "--permission-prompts", "none", "--append-system-prompt", SYSTEM, "--settings", json.dumps(settings)]
    if session:
        cmd += ["--resume", session]
    env = dict(os.environ, TG_AGENT_JOB=job_id, TG_AGENT_CONFIRM_URL=f"http://127.0.0.1:{CONFIRM_PORT}/confirm")
    log("pedido", job_id, "sessão", session or "nova", "-", text[:120])
    stop = threading.Event()
    threading.Thread(target=typing_until, args=(stop,), daemon=True).start()
    t0 = time.time()
    result, new_session, err = "", session, None
    try:
        proc = subprocess.Popen(cmd, cwd=WORKDIR, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        CURRENT["proc"] = proc
        try:
            out, errout = proc.communicate(timeout=JOB_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, errout = proc.communicate()
            err = f"passou de {JOB_TIMEOUT // 60} min e foi interrompido"
        if proc.returncode and proc.returncode < 0 and not err:
            err = "interrompido (/parar)"
        try:
            d = json.loads(out)
            result = d.get("result") or ""
            new_session = d.get("session_id") or session
            if d.get("is_error") and not err:
                err = result or d.get("subtype") or "erro"
            log("fim", job_id, f"{time.time() - t0:.0f}s", f"US$ {d.get('total_cost_usd', 0):.2f}",
                "negados:", len(d.get("permission_denials") or []))
        except ValueError:
            if not err:
                err = (errout or out or "sem saída").strip()[-800:]
    except Exception as e:
        err = str(e)
    finally:
        stop.set()
        CURRENT["proc"] = CURRENT["job"] = None
    if err and not result:
        result = f"❌ {err}"
    elif err:
        result += f"\n\n❌ {err}"
    mins = (time.time() - t0) / 60
    parts = chunks(result)
    if mins >= 1:
        parts[-1] += f"\n\n⏱️ {mins:.0f} min"
    ids = [send(parts[0], edit=placeholder)]
    for p in parts[1:]:
        ids.append(send(p, reply_to=m["message_id"]))
    for i in ids:
        remember(i, new_session)


def worker():
    while True:
        job = JOBS.get()
        try:
            run_job(job)
        except Exception as e:
            log("erro no pedido:", e)
            try:
                send(f"❌ deu erro no agente: {e}", reply_to=job["msg"]["message_id"])
            except Exception:
                pass


def handle_message(m):
    text = (m.get("text") or "").strip()
    if text.split("@")[0] in ("/parar", "/stop"):
        p = CURRENT.get("proc")
        if p and p.poll() is None:
            p.terminate()
            send("⏹️ parando o pedido atual.", reply_to=m["message_id"])
        else:
            send("Nada rodando agora.", reply_to=m["message_id"])
        return
    if not text:
        return
    ahead = JOBS.qsize() + (1 if CURRENT.get("proc") else 0)
    JOBS.put({"msg": m})
    if ahead:
        send(f"⏳ na fila ({ahead} na frente).", reply_to=m["message_id"])


def handle_callback(cq):
    data = cq.get("data") or ""
    ok_txt = "ok"
    if not data.startswith("ag:"):
        return
    _, verdict, cid = data.split(":", 2)
    p = PENDING.get(cid)
    if not p:
        ok_txt = "Essa confirmação já expirou."
    elif verdict == "show":
        try:
            send(p["body"] + p["detail"], edit=p["msg_id"], markup=CONFIRM_KB(cid, shown=True), raw_html=True)
        except Exception as e:
            log("ver comando:", e)
        ok_txt = "Comando mostrado"
    elif p["event"].is_set():
        ok_txt = "Já respondido."
    else:
        p["ok"] = verdict == "ok"
        p["event"].set()
        ok_txt = "Autorizado" if p["ok"] else "Negado"
    try:
        tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": ok_txt})
    except Exception:
        pass


def CONFIRM_KB(cid, shown=False):
    row = [{"text": "✅ Pode", "callback_data": f"ag:ok:{cid}"}, {"text": "❌ Não", "callback_data": f"ag:no:{cid}"}]
    return {"inline_keyboard": [row] if shown else [row, [{"text": "🔎 Ver comando", "callback_data": f"ag:show:{cid}"}]]}


def confirm(req):
    """Chamado pelo hook: pergunta no Telegram e espera o clique."""
    job = CURRENT.get("job") or {}
    if req.get("job") != job.get("id"):
        return False
    cid = uuid.uuid4().hex[:10]
    ev = threading.Event()
    what = req.get("summary") or ""
    if len(what) > 3000:
        what = what[:3000] + "\n…"
    desc = (req.get("description") or "").strip() or f"rodar um comando que {req.get('reason') or 'muda algo'}"
    body = f"⚠️ <b>A IA quer:</b> {html.escape(desc)}"
    if req.get("reason"):
        body += f"\n<i>({html.escape(req['reason'])})</i>"
    detail = "\n<pre>" + html.escape(what) + "</pre>"
    PENDING[cid] = {"event": ev, "ok": False, "body": body, "detail": detail, "msg_id": None}
    msg_id = send(body, reply_to=job.get("reply_to"), markup=CONFIRM_KB(cid), raw_html=True)
    PENDING[cid]["msg_id"] = msg_id
    remember(msg_id, job.get("session"))
    ev.wait(CONFIRM_TIMEOUT)
    ok = PENDING.pop(cid)["ok"]
    verdict = "✅ autorizado" if ok else ("❌ negado" if ev.is_set() else "⌛ sem resposta em "
                                         f"{CONFIRM_TIMEOUT // 60} min — não fiz")
    try:
        send(body + "\n" + verdict, edit=msg_id, markup={"inline_keyboard": []}, raw_html=True)
    except Exception:
        pass
    log("confirmação", req.get("tool"), verdict, "-", what[:100].replace("\n", " "))
    return ok


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj=None):
        data = json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path, _, qs = self.path.partition("?")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.reply(400)
        ip = self.client_address[0]
        if path == "/confirm":
            if ip != "127.0.0.1":
                return self.reply(403)
            return self.reply(200, {"ok": confirm(body)})
        if path == "/telegram":
            token = urllib.parse.parse_qs(qs).get("token", [""])[0]
            if not AGENT_TOKEN or token != AGENT_TOKEN or ip not in FORWARD_FROM:
                log("recusado de", ip)
                return self.reply(403)
            m, cq = body.get("message"), body.get("callback_query")
            frm = str(((m or cq or {}).get("from") or {}).get("id"))
            if frm not in ADMINS:
                return self.reply(403)
            if m:
                if str(m["chat"]["id"]) != CHAT or str(m.get("message_thread_id")) != THREAD:
                    return self.reply(403)
                handle_message(m)
            elif cq:
                handle_callback(cq)
            return self.reply(200)
        return self.reply(404)


def serve(host, port):
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main():
    missing = [k for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "ADMIN_IDS", "AGENT_TOKEN") if not CFG.get(k)]
    if missing:
        raise SystemExit(f"faltando em /etc/telegram-agent.env: {', '.join(missing)}")
    log(f"telegram-agent: {LISTEN}:{PORT} (confirmações 127.0.0.1:{CONFIRM_PORT}), tópico {THREAD}, admins {sorted(ADMINS)}")
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=serve, args=("127.0.0.1", CONFIRM_PORT), daemon=True).start()
    serve(LISTEN, PORT)


if __name__ == "__main__":
    main()
