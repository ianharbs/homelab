"""stack-watch: alerta no ntfy quando um container das stacks do Komodo fica fora do ar.

Por que existe: o Komodo só manda UM aviso (WARNING) quando a stack fica "unhealthy".
Em 09/11/2026 o netalertx e o dispatcharr_web morreram, o aviso se perdeu entre os
"StackAutoUpdated" e ficaram 7 dias parados. Aqui o alerta insiste até resolver.

Regras:
- a cada CHECK_MIN minutos lê ListStacks/ListStackServices/ListServers do Komodo;
- problema = container que não está "running", running mas "(unhealthy)", servidor
  inacessível ou o próprio Komodo sem responder;
- só alerta depois de GRACE_MIN minutos seguidos (evita o auto-update das 04:00);
- repete a cada REPEAT_H horas enquanto continuar; avisa quando volta;
- IGNORE = "stack/serviço" ou "stack" separados por vírgula (ex.: apps-tasks/vikunja).

Segredos em /state/secrets.env (lido pelo script): KOMODO_KEY, KOMODO_SECRET, NTFY_TOKEN.
Variáveis de ambiente têm precedência sobre o arquivo.
"""
import json
import os
import time
import urllib.request

STATE_DIR = "/state"
STATE_FILE = os.path.join(STATE_DIR, "state.json")


def load_secrets():
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


CFG = load_secrets()
KOMODO_URL = CFG.get("KOMODO_URL", "http://192.168.10.50:9120").rstrip("/")
NTFY_URL = CFG.get("NTFY_URL", "http://ntfy:8080").rstrip("/")
NTFY_TOPIC = CFG.get("NTFY_TOPIC", "homelab")
CHECK_MIN = float(CFG.get("CHECK_MIN", "5"))
GRACE_MIN = float(CFG.get("GRACE_MIN", "15"))
REPEAT_H = float(CFG.get("REPEAT_H", "6"))
IGNORE = {x.strip() for x in CFG.get("IGNORE", "").split(",") if x.strip()}


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def komodo(typ, params=None):
    req = urllib.request.Request(
        f"{KOMODO_URL}/read/{typ}", data=json.dumps(params or {}).encode(),
        headers={"X-Api-Key": CFG["KOMODO_KEY"], "X-Api-Secret": CFG["KOMODO_SECRET"],
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ntfy(title, body, priority, tags):
    # Publica pelo JSON (POST na raiz) para o título com acento não quebrar no header.
    payload = {"topic": NTFY_TOPIC, "title": title, "message": body, "priority": priority,
               "tags": tags.split(",")}
    req = urllib.request.Request(
        NTFY_URL, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {CFG['NTFY_TOKEN']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def ignored(stack, service=None):
    return stack in IGNORE or (service and f"{stack}/{service}" in IGNORE)


def current_problems():
    """Devolve {chave: descrição} dos problemas vistos agora."""
    probs = {}
    servers = {s["id"]: s for s in komodo("ListServers")}
    down_servers = set()
    for sid, s in servers.items():
        state = (s.get("info") or {}).get("state")
        if state and state not in ("Ok", "Disabled"):
            down_servers.add(sid)
            probs[f"server:{s['name']}"] = f"servidor {s['name']}: {state}"
    for st in komodo("ListStacks"):
        name, info = st["name"], st.get("info") or {}
        if ignored(name) or info.get("server_id") in down_servers:
            continue
        for sv in komodo("ListStackServices", {"stack": st["id"]}):
            svc = sv.get("service")
            if ignored(name, svc):
                continue
            c = sv.get("container") or {}
            state, status = c.get("state"), c.get("status") or ""
            cname = c.get("name") or svc
            if not c:
                probs[f"{name}/{svc}"] = f"{name}/{svc}: sem container"
            elif state != "running":
                probs[f"{name}/{svc}"] = f"{name}/{cname} ({info.get('server_name')}): {status or state}"
            elif "unhealthy" in status:
                probs[f"{name}/{svc}"] = f"{name}/{cname} ({info.get('server_name')}): {status}"
    return probs


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def fmt_age(sec):
    h, m = divmod(int(sec // 60), 60)
    d, h = divmod(h, 24)
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}min"


def cycle(state):
    now = time.time()
    try:
        probs = current_problems()
    except Exception as e:  # Komodo fora: vira um problema também
        log("erro lendo o Komodo:", e)
        probs = {"komodo": f"Komodo não responde ({type(e).__name__}: {e})"}
        # mantém os problemas já conhecidos para não "resolvê-los" sem ter olhado
        probs.update({k: v["desc"] for k, v in state.items() if k != "komodo"})

    # novos / persistentes
    for k, desc in probs.items():
        p = state.setdefault(k, {"first": now, "last_alert": 0})
        p["desc"] = desc

    due = [k for k, p in state.items() if k in probs and now - p["first"] >= GRACE_MIN * 60
           and (not p["last_alert"] or now - p["last_alert"] >= REPEAT_H * 3600)]
    resolved = [k for k in list(state) if k not in probs]

    if due:
        repeat = all(state[k]["last_alert"] for k in due)
        lines = [f"• {state[k]['desc']} — há {fmt_age(now - state[k]['first'])}" for k in sorted(due)]
        title = ("Ainda fora do ar" if repeat else "Fora do ar") + f": {len(due)} container(s)"
        if len(due) == 1:
            title = ("Ainda fora do ar: " if repeat else "Fora do ar: ") + due[0]
        body = "\n".join(lines) + f"\n\nRepito a cada {REPEAT_H:g} h até voltar."
        try:
            ntfy(title, body, 4, "rotating_light")
            for k in due:
                state[k]["last_alert"] = now
            log("alerta:", title)
        except Exception as e:
            log("erro enviando ntfy:", e)

    alerted_resolved = [k for k in resolved if state[k]["last_alert"]]
    if alerted_resolved:
        lines = [f"• {state[k]['desc'].split(':')[0]} — ficou {fmt_age(now - state[k]['first'])} fora"
                 for k in sorted(alerted_resolved)]
        try:
            ntfy("Voltou: " + (alerted_resolved[0] if len(alerted_resolved) == 1 else f"{len(alerted_resolved)} container(s)"),
                 "\n".join(lines), 3, "white_check_mark")
            log("resolvido:", alerted_resolved)
        except Exception as e:
            log("erro enviando ntfy:", e)
            return  # tenta de novo no próximo ciclo
    for k in resolved:
        state.pop(k, None)
    save_state(state)


def main():
    global CFG
    while True:  # sem segredos: espera (em vez de reiniciar em loop) e relê o arquivo
        missing = [k for k in ("KOMODO_KEY", "KOMODO_SECRET", "NTFY_TOKEN") if not CFG.get(k)]
        if not missing:
            break
        log(f"faltando em secrets.env: {', '.join(missing)} — tento de novo em 5 min")
        time.sleep(300)
        CFG = load_secrets()
    log(f"stack-watch: a cada {CHECK_MIN:g} min, carência {GRACE_MIN:g} min, repete {REPEAT_H:g} h, ignora {sorted(IGNORE)}")
    if os.environ.get("TEST_NTFY"):
        ntfy("stack-watch: teste", "Se chegou, o alerta de containers fora do ar está funcionando.", 3, "test_tube")
    state = load_state()
    while True:
        cycle(state)
        time.sleep(CHECK_MIN * 60)


if __name__ == "__main__":
    main()
