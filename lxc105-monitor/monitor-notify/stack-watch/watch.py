"""stack-watch: recebe os alertas do Komodo e manda para o tópico "Homelab" do Telegram
só o que importa: falhas e atualizações que pedem ação manual.

Por que existe: o Komodo manda UM aviso quando a stack fica "unhealthy". Em 09/11/2026 o
netalertx e o dispatcharr_web morreram, o aviso se perdeu entre os "StackAutoUpdated" e eles
ficaram 7 dias parados. Aqui as falhas insistem até resolver.

Entradas:
- alerter "telegram" do Komodo (endpoint Custom) → POST http://stack-watch:8096/komodo
  (aceito só vindo da monitor_net);
- webhook "telegram" do Proxmox (só warning/error) → POST http://192.168.10.50:8096/pve?token=…
  com {"severity","title","message"}; repassado uma vez, com o log cortado.

O que vira mensagem:
- FALHA (repete a cada REPEAT_H h até resolver, avisa quando volta):
  stack/deployment fora de "running" e servidor inacessível, após GRACE_MIN min seguidos
  (o backup das 03:00 deixa o srv-media inacessível por ~2 min todo dia); disco cheio
  (na hora); o próprio Komodo sem responder (checado daqui a cada 5 min).
- FALHA pontual (uma vez): build/procedure/action/repo com erro.
- AÇÃO MANUAL (resumo agrupado, cada item no máximo 1x a cada DEDUPE_D dias):
  imagem nova em stack sem auto-update, Periphery com versão diferente do Core,
  Resource Sync com mudanças pendentes.
- Ignorado: auto-update que deu certo, CPU/RAM alta e o resto.

Segredos em /state/secrets.env (lido pelo script): TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
TELEGRAM_THREAD_ID, WEBHOOK_TOKEN (o do Proxmox; também guardado como secret do webhook no PVE),
KOMODO_KEY/KOMODO_SECRET (só leitura: a cada 5 min confere o estado real e fecha o que já voltou).
Variáveis de ambiente têm precedência.
"""
import html
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_DIR = "/state"
STATE_FILE = os.path.join(STATE_DIR, "state.json")


def load_cfg():
    cfg = {}
    try:
        with open(os.path.join(STATE_DIR, "secrets.env")) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    cfg.update({k: v for k, v in os.environ.items() if v})
    return cfg


CFG = load_cfg()
PORT = int(CFG.get("PORT", "8096"))
KOMODO_URL = CFG.get("KOMODO_URL", "http://komodo-core:9120").rstrip("/")
GRACE_MIN = float(CFG.get("GRACE_MIN", "15"))
REPEAT_H = float(CFG.get("REPEAT_H", "6"))
DIGEST_MIN = float(CFG.get("DIGEST_MIN", "10"))
DEDUPE_D = float(CFG.get("DEDUPE_D", "7"))
IGNORE = {x.strip() for x in CFG.get("IGNORE", "").split(",") if x.strip()}

FAIL_ONCE = {"BuildFailed", "RepoBuildFailed", "ProcedureFailed", "ActionFailed",
             "AwsBuilderTerminationFailed", "Custom"}
LOCK = threading.Lock()


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def esc(s):
    return html.escape(str(s), quote=False)


def telegram(text):
    payload = {"chat_id": CFG["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if CFG.get("TELEGRAM_THREAD_ID"):
        payload["message_thread_id"] = int(CFG["TELEGRAM_THREAD_ID"])
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{CFG['TELEGRAM_TOKEN']}/sendMessage",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


# ---------------------------------------------------------------- estado
def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (FileNotFoundError, ValueError):
        st = {}
    st.setdefault("problems", {})   # chave -> {first, last_alert, desc, grace}
    st.setdefault("digest", [])     # itens de ação manual esperando o resumo
    st.setdefault("sent", {})       # chave de dedupe -> ts do último envio
    st.setdefault("once", [])       # falhas pontuais a enviar
    return st


STATE = load_state()


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(STATE, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def fmt_size(used_gb, total_gb):
    if total_gb < 1024:
        return f"{used_gb:.0f} de {total_gb:.0f} GB"
    return f"{used_gb / 1024:.1f} de {total_gb / 1024:.1f} TB"


# título de cada tipo de problema: (novo, repetido, resolvido)
KINDS = {
    "disk": ("💾 <b>Disco quase cheio</b>", "💾 <b>Disco ainda quase cheio</b>", "✅ <b>Disco normalizado</b>"),
    "down": ("🚨 <b>Fora do ar</b>", "🚨 <b>Ainda fora do ar</b>", "✅ <b>Voltou</b>"),
}


def kind(key):
    return "disk" if key.startswith("disk:") else "down"


def open_problem(key, desc, grace=True):
    p = STATE["problems"].get(key)
    if p:
        p["desc"] = desc
    else:
        STATE["problems"][key] = {"first": time.time(), "last_alert": 0, "desc": desc, "grace": grace}
        log("aberto:", key, "-", desc)


def close_problem(key):
    p = STATE["problems"].get(key)
    if p:
        p["resolved"] = time.time()
        log("resolvido:", key)


def add_digest(dedupe_key, line):
    last = STATE["sent"].get(dedupe_key, 0)
    if time.time() - last < DEDUPE_D * 86400:
        return
    if any(d["key"] == dedupe_key for d in STATE["digest"]):
        return
    STATE["digest"].append({"key": dedupe_key, "line": line, "ts": time.time()})
    log("ação manual:", line)


# ---------------------------------------------------------------- alertas do Komodo
def handle_alert(alert):
    data = alert.get("data") or {}
    typ, d = data.get("type"), data.get("data") or {}
    name = d.get("name") or "?"
    where = f" ({d['server_name']})" if d.get("server_name") else ""
    resolved = bool(alert.get("resolved"))
    if name in IGNORE:
        return

    if typ in ("StackStateChange", "ContainerStateChange"):
        key = f"stack:{name}"
        if d.get("to") == "running":
            close_problem(key)
        elif d.get("to") in ("stopped", "down") and d.get("from") in ("stopped", "down"):
            pass
        else:
            open_problem(key, f"<b>{esc(name)}</b>{esc(where)}: {esc(d.get('from'))} → <b>{esc(d.get('to'))}</b>")
    elif typ == "ServerUnreachable":
        key = f"server:{name}"
        if resolved:
            close_problem(key)
        else:
            err = (d.get("err") or {}).get("error", "")
            open_problem(key, f"servidor <b>{esc(name)}</b> inacessível ({esc(err)})")
    elif typ == "ServerDisk":
        key = f"disk:{name}:{d.get('path')}"
        if resolved:
            close_problem(key)
        else:
            used, total = d.get("used_gb", 0), d.get("total_gb", 1) or 1
            open_problem(key, f"disco <b>{esc(d.get('path'))}</b> em {esc(name)}: "
                              f"{used / total:.0%} ({fmt_size(used, total)})", grace=False)
    elif typ in FAIL_ONCE:
        if not resolved:
            msg = d.get("message") or d.get("err") or d.get("version") or ""
            STATE["once"].append(f"❌ <b>{esc(typ)}</b>: {esc(name)}{esc(where)} {esc(json.dumps(msg, ensure_ascii=False)[:300]) if msg else ''}")
    elif typ in ("StackImageUpdateAvailable", "DeploymentImageUpdateAvailable"):
        svc = d.get("service")
        img = d.get("image", "")
        add_digest(f"img:{name}:{svc}:{img}",
                   f"⬆️ imagem nova: <b>{esc(name)}</b>{('/' + esc(svc)) if svc else ''} — <code>{esc(img)}</code>")
    elif typ == "ServerVersionMismatch":
        if not resolved:
            add_digest(f"ver:{name}:{d.get('server_version')}:{d.get('core_version')}",
                       f"🔧 Periphery do <b>{esc(name)}</b> em {esc(d.get('server_version'))}, Core em {esc(d.get('core_version'))} — atualizar o Periphery")
    elif typ == "ResourceSyncPendingUpdates":
        if not resolved:
            add_digest(f"sync:{name}:{time.strftime('%Y-%m-%d')}",
                       f"🔁 Resource Sync <b>{esc(name)}</b> com mudanças pendentes — revisar e executar no Komodo")
    elif typ == "Test":
        STATE["once"].append("🧪 Teste do alerter do Komodo: chegou certinho.")
    # StackAutoUpdated, DeploymentAutoUpdated, ServerCpu, ServerMem, ... → ignorados
    save_state()


def handle_pve(n):
    """Notificação do Proxmox (webhook "telegram", matcher só warning/error). O vzdump manda o
    log inteiro, que estoura os 4096 caracteres do Telegram: fica só o fim."""
    sev = (n.get("severity") or "").lower()
    icon = {"error": "❌", "warning": "⚠️"}.get(sev, "ℹ️")
    msg = (n.get("message") or "").strip()
    if len(msg) > 2500:
        msg = "…\n" + msg[-2500:]
    STATE["once"].append(f"{icon} <b>Proxmox: {esc(n.get('title') or '')}</b>\n<pre>{esc(msg)}</pre>")
    log("proxmox:", sev, n.get("title"))
    save_state()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code):
        self.send_response(code)
        self.end_headers()

    def do_POST(self):
        path, _, qs = self.path.partition("?")
        params = urllib.parse.parse_qs(qs)
        internal = not self.client_address[0].startswith("192.168.")  # veio pela monitor_net
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.reply(400)
        if path == "/komodo":
            if not internal:  # a porta publicada é só para o Proxmox
                return self.reply(403)
            with LOCK:
                handle_alert(body)
            return self.reply(200)
        if path == "/pve":
            if not CFG.get("WEBHOOK_TOKEN") or params.get("token", [""])[0] != CFG["WEBHOOK_TOKEN"]:
                return self.reply(403)
            with LOCK:
                handle_pve(body)
            return self.reply(200)
        return self.reply(404)

    def do_GET(self):
        if self.client_address[0].startswith("192.168."):
            return self.reply(403)
        with LOCK:
            out = json.dumps({k: STATE[k] for k in ("problems", "digest")}, ensure_ascii=False, indent=1).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(out)


# ---------------------------------------------------------------- envio periódico
def fmt_age(sec):
    h, m = divmod(int(sec // 60), 60)
    d, h = divmod(h, 24)
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}min"


def komodo_read(typ):
    req = urllib.request.Request(
        KOMODO_URL + "/read", data=json.dumps({"type": typ, "params": {}}).encode(),
        headers={"Content-Type": "application/json", "X-Api-Key": CFG["KOMODO_KEY"],
                 "X-Api-Secret": CFG["KOMODO_SECRET"]})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def reconcile():
    """Fecha problema que o Komodo já dá como resolvido. Se o alerta de volta ("→ running" ou
    ServerUnreachable resolvido) se perder — como em 20/11/2026, quando 8 stacks do srv-media
    voltaram sem aviso e ficaram "fora do ar" aqui por 2,5 dias —, o estado real corrige."""
    if not (CFG.get("KOMODO_KEY") and CFG.get("KOMODO_SECRET")):
        return
    ok = set()
    for typ in ("ListStacks", "ListDeployments"):
        ok |= {f"stack:{x['name']}" for x in komodo_read(typ) if (x.get("info") or {}).get("state") == "running"}
    ok |= {f"server:{x['name']}" for x in komodo_read("ListServers") if (x.get("info") or {}).get("state") == "Ok"}
    for k, p in STATE["problems"].items():
        if k in ok and not p.get("resolved"):
            log("estado real ok:", k)
            close_problem(k)


def check_komodo():
    try:
        with urllib.request.urlopen(KOMODO_URL + "/", timeout=15) as r:
            r.read(64)
        close_problem("komodo")
    except Exception as e:
        open_problem("komodo", f"<b>Komodo</b> não responde ({esc(type(e).__name__)}) — sem ele não chega nenhum alerta")
        return
    try:
        reconcile()
    except Exception as e:
        log("reconciliação falhou:", e)


def tick():
    now = time.time()
    probs = STATE["problems"]

    # resolvidos: avisa só se já tinha alertado
    back = [k for k, p in probs.items() if p.get("resolved") and p["last_alert"]]
    for kd, (_, _, head_ok) in KINDS.items():
        ks = sorted(k for k in back if kind(k) == kd)
        if not ks:
            continue
        if kd == "disk":
            lines = [f"• {probs[k]['desc'].split(':')[0]} — ficou cheio por {fmt_age(probs[k]['resolved'] - probs[k]['first'])}"
                     for k in ks]
        else:
            lines = [f"• {probs[k]['desc'].split(':')[0]} — ficou {fmt_age(probs[k]['resolved'] - probs[k]['first'])} fora"
                     for k in ks]
        telegram(head_ok + "\n" + "\n".join(lines))
    for k in [k for k, p in probs.items() if p.get("resolved")]:
        del probs[k]

    due = [k for k, p in probs.items()
           if (not p.get("grace", True) or now - p["first"] >= GRACE_MIN * 60)
           and (not p["last_alert"] or now - p["last_alert"] >= REPEAT_H * 3600)]
    for kd, (head_new, head_rep, _) in KINDS.items():
        ks = sorted(k for k in due if kind(k) == kd)
        if not ks:
            continue
        repeat = all(probs[k]["last_alert"] for k in ks)
        lines = [f"• {probs[k]['desc']} — há {fmt_age(now - probs[k]['first'])}" for k in ks]
        head = head_rep if repeat else head_new
        telegram(f"{head}\n" + "\n".join(lines) + f"\n\n<i>Repito a cada {REPEAT_H:g} h até resolver.</i>")
        for k in ks:
            probs[k]["last_alert"] = now

    while STATE["once"]:
        telegram(STATE["once"][0])
        STATE["once"].pop(0)

    dg = STATE["digest"]
    if dg and now - min(d["ts"] for d in dg) >= DIGEST_MIN * 60:
        telegram("🛠️ <b>Precisa de ação manual</b>\n" + "\n".join(d["line"] for d in dg))
        for d in dg:
            STATE["sent"][d["key"]] = now
        STATE["digest"] = []
        STATE["sent"] = {k: v for k, v in STATE["sent"].items() if now - v < DEDUPE_D * 86400}
    save_state()


def loop():
    last_komodo = 0
    while True:
        try:
            with LOCK:
                if time.time() - last_komodo >= 300:
                    check_komodo()
                    last_komodo = time.time()
                tick()
        except Exception as e:  # Telegram fora etc.: tenta no próximo minuto
            log("erro no envio:", e)
        time.sleep(60)


def main():
    global CFG
    while True:  # sem segredos: espera e relê (em vez de reiniciar em loop)
        missing = [k for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID") if not CFG.get(k)]
        if not missing:
            break
        log(f"faltando em secrets.env: {', '.join(missing)} — tento de novo em 5 min")
        time.sleep(300)
        CFG = load_cfg()
    log(f"stack-watch: porta {PORT}, carência {GRACE_MIN:g} min, repete {REPEAT_H:g} h, "
        f"resumo {DIGEST_MIN:g} min, dedupe {DEDUPE_D:g} d, ignora {sorted(IGNORE)}")
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
