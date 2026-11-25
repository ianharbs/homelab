"""Infra compartilhada dos agentes (só biblioteca padrão): segredos, HTTP, SQLite, Telegram, IA e clientes
do Spoolman, InvenTree e Vikunja. Segredos em /state/secrets.env (fora do git, lido aqui)."""
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid


def load_secrets(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if not os.environ.get(k.strip()):
                        os.environ[k.strip()] = v.strip()
    except FileNotFoundError:
        pass


load_secrets(os.environ.get("SECRETS_FILE", "/state/secrets.env"))
E = os.environ.get
log = logging.getLogger("print3d")
STATE = E("STATE_DIR", "/state")
DB_PATH = f"{STATE}/agents.db"
LIBRARY = E("LIBRARY_DIR", "/libraries")          # mesma pasta das bibliotecas do Manyfold
TG_TOKEN = E("TELEGRAM_TOKEN", "")
ALLOWED = {x.strip() for x in E("ALLOWED_USERS", "").split(",") if x.strip()}

# Tópicos do grupo do negócio (criados pelo bot quando vira admin num grupo com Tópicos)
TOPICS = {
    "geral": ("💬 Geral", 7322096), "curadoria": ("🔎 Curadoria", 16766590), "anuncios": ("📝 Anúncios", 13338331),
    "estoque": ("📦 Estoque", 9367192), "pedidos": ("🧾 Pedidos", 16749490), "relatorios": ("📊 Relatórios", 16478047),
}

# ---------------------------------------------------------------- config do negócio (mesma lógica da calculadora)
DEFAULT_CONFIG = {
    "watts": 150, "tariff": 0.90, "printer_cost": 5000, "life_hours": 3000, "maint_h": 0.30,
    "labor_rate": 30, "labor_min": 10, "packaging": 1.50, "fail_pct": 10, "markup": 2.5,
    "min_markup": 1.5,            # piso: o agente de preço nunca sugere abaixo de custo × isto
    "filament_kg": 100,           # R$/kg quando o Spoolman não tem carretel com preço
    "g_per_hour": 20,             # velocidade média p/ estimar tempo quando não há fatiamento
    "low_spool_g": 200,           # carretel abaixo disto = comprar
    "channels": {"ml": {"name": "Mercado Livre", "pct": 14, "fixed": 6.5},
                 "shopee": {"name": "Shopee", "pct": 20, "fixed": 4},
                 "direto": {"name": "Venda direta", "pct": 0, "fixed": 0}},
    "curadoria_por_semana": 5,
}


def config():
    c = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(f"{STATE}/config.json") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
    except FileNotFoundError:
        save_config(c)
    except Exception as e:
        log.error("config.json inválido (%s), usando padrões", e)
    return c


def save_config(c):
    tmp = f"{STATE}/config.json.new"
    with open(tmp, "w") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    os.replace(tmp, f"{STATE}/config.json")


# ---------------------------------------------------------------- SQLite
SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS candidates (id INTEGER PRIMARY KEY, source TEXT, ext_id TEXT, name TEXT, url TEXT,
  license TEXT, image TEXT, data TEXT, score REAL, status TEXT DEFAULT 'novo', created REAL, UNIQUE(source, ext_id));
CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, name TEXT, sku TEXT, candidate_id INTEGER,
  folder TEXT, grams REAL, hours REAL, estimated INTEGER, cost REAL, prices TEXT, vikunja_task INTEGER,
  status TEXT DEFAULT 'ideia', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY, product_id INTEGER, kind TEXT, data TEXT,
  status TEXT DEFAULT 'pendente', created REAL);
CREATE TABLE IF NOT EXISTS seen_alerts (k TEXT PRIMARY KEY, ts REAL);
"""


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def kv_get(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def kv_set(k, v):
    with db() as c:
        c.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ---------------------------------------------------------------- HTTP
def http(method, url, headers=None, body=None, timeout=30, raw=False, form=None):
    data, headers = None, dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers.setdefault("User-Agent", "print3d-agents/1.0")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        content = r.read()
    if raw:
        return content
    return json.loads(content) if content.strip() else None


def download(url, path, max_mb=80):
    req = urllib.request.Request(url, headers={"User-Agent": "print3d-agents/1.0"})
    size = 0
    with urllib.request.urlopen(req, timeout=120) as r, open(path + ".part", "wb") as f:
        while chunk := r.read(1 << 16):
            size += len(chunk)
            if size > max_mb << 20:
                raise ValueError(f"arquivo maior que {max_mb} MB")
            f.write(chunk)
    os.replace(path + ".part", path)
    return size


def esc(s):
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slug(s, n=60):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s[:n].strip("-") or "modelo"


def brl(v):
    return ("R$ {:,.2f}".format(v or 0)).replace(",", "X").replace(".", ",").replace("X", ".")


# ---------------------------------------------------------------- Telegram
def tg(method, body=None, timeout=40):
    return http("POST", f"https://api.telegram.org/bot{TG_TOKEN}/{method}", body=body or {}, timeout=timeout)


def forum_id():
    return E("TELEGRAM_FORUM_ID", "") or kv_get("forum_id", "")


def topic_id(kind):
    tid = kv_get(f"topic_{kind}", "")
    if tid or not forum_id():
        return tid
    name, color = TOPICS[kind]
    try:
        tid = str(tg("createForumTopic", {"chat_id": forum_id(), "name": name, "icon_color": color})
                  ["result"]["message_thread_id"])
        kv_set(f"topic_{kind}", tid)
        log.info("tópico %s criado (%s)", name, tid)
    except Exception as e:
        log.error("não consegui criar o tópico %s (bot precisa ser admin com 'Gerenciar tópicos'): %s", name, e)
    return tid


def kind_of_thread(thread):
    if thread in TOPICS:  # WhatsApp: o "tópico" é o próprio tipo do grupo
        return thread
    for k in TOPICS:
        if thread and kv_get(f"topic_{k}", "") == str(thread):
            return k
    return "geral"


def buttons(rows):
    """rows = [[(texto, callback_data), ...], ...]"""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def post(kind, text, photo=None, kb=None, reply_to=None):
    """Mensagem na central `kind`. Sem Telegram/WhatsApp configurado, vai para o log (o resto funciona igual)."""
    import wa
    if wa.ON and wa.group_of(kind):
        try:
            return wa.send(wa.group_of(kind), text, photo=photo, kb=kb, reply_to=reply_to)
        except Exception as e:
            log.error("whatsapp (%s): %s", kind, e)
            return None
    chat, thread = forum_id(), topic_id(kind) if forum_id() else None
    if not (TG_TOKEN and chat) or E("TELEGRAM_OFF") == "1":
        log.info("[telegram desligado → %s]\n%s", kind, re.sub(r"<[^>]+>", "", text))
        return None
    msg = {"chat_id": chat, "parse_mode": "HTML"}
    if thread:
        msg["message_thread_id"] = int(thread)
    if kb:
        msg["reply_markup"] = kb
    if reply_to:
        msg["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    try:
        if photo:
            try:
                return tg("sendPhoto", {**msg, "photo": photo, "caption": text[:1024]})["result"]
            except Exception as e:
                log.warning("sendPhoto falhou (%s), mandando texto", e)
        parts, r = split(text), None
        for i, part in enumerate(parts):
            m = {**msg, "text": part, "link_preview_options": {"is_disabled": True}}
            if i < len(parts) - 1:
                m.pop("reply_markup", None)   # botões só na última parte
            r = tg("sendMessage", m)
        return r["result"]
    except Exception as e:
        log.error("telegram (%s): %s", kind, e)
        return None


def split(text, n=3900):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    return parts + [cur] if cur.strip() else parts or [text]


def is_wa(chat):
    return isinstance(chat, str) and "@" in chat


def reply(chat, thread, text, kb=None):
    if is_wa(chat):
        import wa
        try:
            return wa.send(chat, text, kb=kb)
        except Exception as e:
            log.error("reply (whatsapp): %s", e)
            return None
    if not TG_TOKEN:
        log.info("[resposta] %s", text)
        return None
    msg = {"chat_id": chat, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    if thread:
        msg["message_thread_id"] = int(thread)
    if kb:
        msg["reply_markup"] = kb
    try:
        return tg("sendMessage", msg)["result"]
    except Exception as e:
        log.error("reply: %s", e)


def edit_buttons(chat, message_id, note=None):
    """Tira os botões de uma mensagem já respondida (e acrescenta uma nota)."""
    if is_wa(chat):  # WhatsApp: "botões" são opções; apaga o mapa p/ não responder 2x e responde com a nota
        import wa
        kv_set(f"wa_btn_{message_id}", "")
        if note:
            try:
                wa.send_text(chat, wa.to_wa(note), reply_to=message_id)
            except Exception as e:
                log.warning("edit_buttons (whatsapp): %s", e)
        return
    try:
        tg("editMessageReplyMarkup", {"chat_id": chat, "message_id": message_id, "reply_markup": {"inline_keyboard": []}})
        if note:
            tg("sendMessage", {"chat_id": chat, "text": note, "parse_mode": "HTML",
                               "reply_parameters": {"message_id": message_id, "allow_sending_without_reply": True}})
    except Exception as e:
        log.warning("edit_buttons: %s", e)


def tg_file(file_id, path):
    if str(file_id).startswith("wa:"):  # anexo do WhatsApp: "wa:<id da mensagem>|<chat>"
        import wa
        mid, chat = file_id[3:].split("|", 1)
        return wa.baixar(mid, chat, path)
    info = tg("getFile", {"file_id": file_id})["result"]
    return download(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}", path, max_mb=20)


# ---------------------------------------------------------------- IA (API compatível com OpenAI; padrão Gemini grátis)
LLM_URL, LLM_KEY, LLM_MODEL = E("LLM_URL", "").rstrip("/"), E("LLM_API_KEY", ""), E("LLM_MODEL", "")
LLM_ON = bool(LLM_URL and LLM_KEY and LLM_MODEL)
# Modelo melhor p/ textos (anúncios, curadoria): o flash-lite em modo JSON tira os acentos. Raciocínio baixo = ~4 s.
LLM_WRITER = E("LLM_MODEL_WRITER", "gemini-3.5-flash")
SYSTEM = ("Você é parte da equipe de agentes de um pequeno negócio brasileiro de impressão 3D (FDM, impressora "
          "Anycubic Kobra X Combo, multicolor com ACE, PLA/PETG). Vende no Mercado Livre e na Shopee. "
          "Responda em português do Brasil, direto e prático.")


def llm(prompt, json_out=False, temperature=0.4, system=SYSTEM, tries=3, writer=False):
    if not LLM_ON:
        raise RuntimeError("IA não configurada (LLM_URL/LLM_API_KEY/LLM_MODEL)")
    body = {"model": LLM_WRITER if writer and LLM_WRITER else LLM_MODEL, "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    if writer and LLM_WRITER:
        body["reasoning_effort"] = E("LLM_REASONING", "low")
    if json_out:
        body["response_format"] = {"type": "json_object"}
    for i in range(tries):
        try:
            res = http("POST", f"{LLM_URL}/chat/completions", {"Authorization": f"Bearer {LLM_KEY}"}, body, timeout=120)
            out = res["choices"][0]["message"]["content"] or ""
            return parse_json(out) if json_out else out.strip()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and i < tries - 1:
                time.sleep(15 * (i + 1))
                continue
            raise
        except ValueError:
            if i < tries - 1:
                continue
            raise


def parse_json(s):
    s = s.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"[\[{].*[\]}]", s, re.S)
        if m:
            return json.loads(m.group(0))
        raise ValueError("IA não devolveu JSON")


# ---------------------------------------------------------------- Spoolman
def spoolman(path):
    return http("GET", E("SPOOLMAN_URL", "http://spoolman:8000").rstrip("/") + "/api/v1" + path, timeout=15)


def spool_price_kg(s):
    f = s.get("filament") or {}
    price = s.get("price") if s.get("price") is not None else f.get("price")
    weight = s.get("initial_weight") or f.get("weight")
    return price / weight * 1000 if price and weight else None


def avg_filament_kg(material=None):
    """R$/kg médio dos carretéis ativos (do material pedido, se houver)."""
    try:
        vals = [p for s in spoolman("/spool?allow_archived=false")
                if (p := spool_price_kg(s)) and (not material or (s.get("filament") or {}).get("material", "").upper() == material.upper())]
        if vals:
            return sum(vals) / len(vals)
    except Exception as e:
        log.warning("spoolman: %s", e)
    return config()["filament_kg"]


# ---------------------------------------------------------------- InvenTree
def inventree(method, path, body=None):
    base = E("INVENTREE_URL", "http://192.168.10.200:8090").rstrip("/")
    return http(method, base + "/api" + path, {"Authorization": "Token " + E("INVENTREE_TOKEN", ""),
                                               "Accept": "application/json"}, body, timeout=30)


# ---------------------------------------------------------------- Vikunja
VMAP = json.loads(E("VIKUNJA_MAP", "{}") or "{}")


def vikunja(method, path, body=None):
    return http(method, E("VIKUNJA_URL", "http://192.168.10.30:3456").rstrip("/") + "/api/v1" + path,
                {"Authorization": "Bearer " + E("VIKUNJA_TOKEN", "")}, body, timeout=30)


def vk_task(project, title, html, labels=(), bucket=None, due=None):
    """Cria tarefa num projeto (pedidos/compras/ideias), com etiquetas e coluna do kanban opcionais."""
    pid = VMAP["projects"][project]
    body = {"title": title[:250], "description": html}
    if due:
        body["due_date"] = due
    t = vikunja("PUT", f"/projects/{pid}/tasks", body)
    for name in labels:
        lid = VMAP.get("labels", {}).get(name)
        if lid:
            try:
                vikunja("PUT", f"/tasks/{t['id']}/labels", {"label_id": lid})
            except Exception as e:
                log.warning("label %s: %s", name, e)
    if bucket and project == "pedidos":
        bid = VMAP.get("buckets", {}).get(bucket)
        view = VMAP.get("kanban", {}).get(str(pid)) or VMAP.get("kanban", {}).get(pid)
        if bid and view:
            try:
                vikunja("POST", f"/projects/{pid}/views/{view}/buckets/{bid}/tasks", {"task_id": t["id"], "bucket_id": bid,
                                                                                    "project_view_id": view})
            except Exception as e:
                log.warning("mover p/ coluna %s: %s", bucket, e)
    return t


def vk_open_titles(project):
    pid = VMAP["projects"][project]
    out, page = set(), 1
    while True:
        r = vikunja("GET", f"/projects/{pid}/tasks?filter=done%3Dfalse&per_page=50&page={page}") or []
        out |= {t["title"] for t in r}
        if len(r) < 50:
            return out
        page += 1


def vk_link(task_id):
    return f"{E('VIKUNJA_PUBLIC', 'https://tasks.ihome.dev.br').rstrip('/')}/tasks/{task_id}"
