"""legenda-revisor: revisa as legendas traduzidas (EN -> PT-BR) com o Claude e corrige os erros.

Fluxo:
  - a cada POLL_SECONDS lê o banco do Lingarr (só leitura) e enfileira as traduções concluídas novas;
  - `revisar <pasta|arquivo>` enfileira o acervo antigo (prioridade menor);
  - o worker pega um arquivo por vez: alinha EN x PT bloco a bloco, manda pro Claude (Sonnet 5.5)
    em pedaços de CHUNK_BLOCKS blocos e recebe SÓ as correções (JSON com schema);
  - cada correção é validada (tags, nº de linhas, travessão de diálogo) antes de ser aplicada;
  - o .srt original vai para BACKUP_DIR antes de ser reescrito; relatório em /state/relatorios.

Orçamento: o custo de cada chamada é calculado pelo `usage` da resposta e somado por mês no SQLite.
Antes de cada chamada estima o custo; se passar de MONTHLY_BUDGET_USD para tudo. O acervo antigo só
usa o que sobra acima da reserva NEW_RESERVE_USD (guardada para as traduções novas).

Comandos: servico | revisar <caminho...> [--forcar] | testar <arquivo.pt-BR.srt> | status | desfazer <arquivo>
"""
import datetime as dt
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time

import anthropic


def load_secrets(path):
    """Segredos ficam fora do git, em /state/secrets.env (variável de ambiente tem prioridade)."""
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
MODEL = E("MODEL", "claude-sonnet-5-5")
EFFORT = E("EFFORT", "low")  # low | medium | high  (mais alto = mais caro)
BUDGET = float(E("MONTHLY_BUDGET_USD", "10"))
RESERVE = float(E("NEW_RESERVE_USD", "4"))  # acervo antigo para quando o gasto do mês chega em BUDGET-RESERVE
POLL = int(E("POLL_SECONDS", "120"))
CHUNK = int(E("CHUNK_BLOCKS", "400"))
CONTEXT = int(E("CONTEXT_BLOCKS", "15"))  # blocos anteriores mandados só como contexto
DB_PATH = E("DB_PATH", "/state/revisor.db")
LINGARR_DB = E("LINGARR_DB", "/lingarr/local.db")
BACKUP_DIR = E("BACKUP_DIR", "/data/backup/legendas")
REPORT_DIR = E("REPORT_DIR", "/state/relatorios")
MEDIA_ROOT = E("MEDIA_ROOT", "/data")
DRY_RUN = E("DRY_RUN", "0") == "1"

# US$ por milhão de tokens: entrada, saída, escrita de cache, leitura de cache.
PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 0.20),
}
PRICE_UNKNOWN = (5.0, 25.0, 6.25, 0.50)  # modelo de fallback desconhecido: conta pelo preço de Opus (pessimista)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("revisor")

# ---------------------------------------------------------------- SRT

TS = re.compile(r"(\d+:\d+:\d+[,.]\d+)\s*-->\s*(\d+:\d+:\d+[,.]\d+)")
TAG = re.compile(r"<[^>]+>|\{\\[^}]*\}")


def read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue


def parse_srt(text):
    """-> lista de blocos {num, ts, start, lines}. Tolera BOM, \\r\\n e numeração faltando."""
    blocks = []
    for chunk in re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()):
        lines = chunk.split("\n")
        i = next((k for k, l in enumerate(lines) if TS.search(l)), None)
        if i is None:
            continue
        num = lines[i - 1].strip() if i > 0 else str(len(blocks) + 1)
        m = TS.search(lines[i])
        blocks.append({"num": num, "ts": lines[i].strip(), "start": m.group(1).replace(".", ","),
                       "lines": lines[i + 1:]})
    return blocks


def write_srt(blocks, orig):
    """Remonta o SRT mantendo o estilo do original (\\r\\n ou \\n, BOM, final do arquivo)."""
    newline = "\r\n" if "\r\n" in orig else "\n"
    body = "\n\n".join("\n".join([b["num"], b["ts"], *b["lines"]]) for b in blocks)
    tail = orig[len(orig.rstrip()):].replace("\r\n", "\n") or "\n"
    bom = "\ufeff" if orig.startswith("\ufeff") else ""
    o = orig.lstrip("\ufeff")
    head = o[:len(o) - len(o.lstrip())].replace("\r\n", "\n")
    return bom + (head + body + tail).replace("\n", newline)


def align(en, pt):
    """Pareia cada bloco PT com o EN de mesmo início (ou mesma posição). Devolve (pares, % casado)."""
    by_start = {b["start"]: b for b in en}
    pairs, hit = [], 0
    same_len = len(en) == len(pt)
    for i, b in enumerate(pt):
        e = by_start.get(b["start"]) or (en[i] if same_len else None)
        hit += e is not None
        pairs.append((e, b))
    return pairs, (hit / len(pt) if pt else 0)


def en_sibling(pt_path):
    base = re.sub(r"\.(pt-BR|pt_BR|pt-br|pob|pt|por)(\.(forced|hi|sdh|cc))?\.srt$", "", pt_path)
    if base == pt_path:
        return None
    for suf in (".en.srt", ".eng.srt", ".en.hi.srt", ".en.sdh.srt", ".en.cc.srt", ".en.forced.srt"):
        if os.path.exists(base + suf):
            return base + suf
    return None


def media_label(path):
    """'Série (1995) - S03E21 - Título' a partir do nome do arquivo (usado como contexto)."""
    name = os.path.basename(path)
    name = re.sub(r"\.(pt-BR|pt_BR|pob|pt|por)(\.\w+)?\.srt$", "", name)
    return name


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# ---------------------------------------------------------------- estado

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript("""
        create table if not exists files(
          path text primary key, en_path text, source text, priority int,
          status text, pt_sha text, added_at text, done_at text,
          cost real default 0, n_fixes int default 0, note text);
        create table if not exists calls(
          id integer primary key, at text, month text, path text, model text,
          input int, output int, cache_write int, cache_read int, cost real);
        create table if not exists fixes(
          id integer primary key, at text, path text, block text,
          en text, before text, after text, tipo text, motivo text, applied int);
        create table if not exists meta(k text primary key, v text);
    """)
    return con


def meta(con, k, v=None):
    if v is None:
        r = con.execute("select v from meta where k=?", (k,)).fetchone()
        return r["v"] if r else None
    con.execute("insert or replace into meta values(?,?)", (k, str(v)))
    con.commit()


def month():
    return dt.date.today().strftime("%Y-%m")


def spent(con):
    return con.execute("select coalesce(sum(cost),0) s from calls where month=?", (month(),)).fetchone()["s"]


def enqueue(con, path, source, priority, force=False):
    en = en_sibling(path)
    if not en:
        log.info("sem legenda EN ao lado, ignorando: %s", path)
        return False
    row = con.execute("select status, pt_sha from files where path=?", (path,)).fetchone()
    cur = sha(path)
    if row and not force and row["status"] in ("feito", "pendente") and (row["status"] == "pendente" or row["pt_sha"] == cur):
        return False
    now = dt.datetime.now().isoformat(timespec="seconds")
    con.execute("""insert into files(path,en_path,source,priority,status,pt_sha,added_at)
                   values(?,?,?,?, 'pendente', ?, ?)
                   on conflict(path) do update set en_path=excluded.en_path, source=excluded.source,
                     priority=min(files.priority, excluded.priority), status='pendente', added_at=excluded.added_at""",
                (path, en, source, priority, cur, now))
    con.commit()
    return True


# ---------------------------------------------------------------- Lingarr

def poll_lingarr(con):
    """Traduções concluídas (status 2) com id maior que o último visto. 1ª execução começa do máximo atual
    (o acervo antigo entra pelo comando `revisar`, para não gastar o orçamento de uma vez)."""
    try:
        lg = sqlite3.connect(f"file:{LINGARR_DB}?mode=ro&immutable=1", uri=True, timeout=5)
        rows = lg.execute("select id, translated_subtitle from translation_requests "
                          "where status=2 and translated_subtitle is not null order by id").fetchall()
        lg.close()
    except sqlite3.Error as e:
        log.warning("não consegui ler o banco do Lingarr (%s), tento de novo depois", e)
        return
    last = meta(con, "lingarr_last_id")
    if last is None:
        meta(con, "lingarr_last_id", max((r[0] for r in rows), default=0))
        log.info("1ª execução: começando depois da tradução %s do Lingarr", meta(con, "lingarr_last_id"))
        return
    last = int(last)
    for rid, path in rows:
        if rid <= last:
            continue
        if os.path.exists(path) and enqueue(con, path, "lingarr", 0, force=True):
            log.info("na fila (Lingarr #%s): %s", rid, path)
        last = rid
    meta(con, "lingarr_last_id", last)


# ---------------------------------------------------------------- Claude

SYSTEM = """Você é um revisor sênior de legendas de filmes e séries traduzidas do inglês para o português do Brasil (padrão das legendas de streaming brasileiras). A tradução foi feita por um modelo automático; seu trabalho é encontrar e corrigir os ERROS dela.

Você recebe o nome do título/episódio e uma lista de blocos no formato:
[id] EN: texto original
     PT: tradução atual
Quebras de linha dentro do bloco aparecem como " ⏎ ". Blocos marcados como CONTEXTO servem só para entender a cena; não os corrija.

Corrija quando houver:
- sentido errado ou perdido (falso cognato, frase mal entendida, negação invertida, sujeito trocado);
- tradução literal que não faz sentido no contexto da cena (expressões idiomáticas, gírias, piadas, trocadilhos, frases de desafio como "Watch me" = "Duvida?");
- bloco não traduzido ou traduzido só pela metade;
- gênero/número errados (ex.: personagem feminina tratada no masculino), concordância ou gramática quebrada;
- inconsistência com o resto da legenda (nome próprio traduzido em um lugar e não em outro, tratamento tu/você/senhor mudando sem motivo, termo recorrente com traduções diferentes);
- nomes próprios, marcas e lugares que não deveriam ter sido traduzidos.

NÃO corrija:
- escolhas de estilo, sinônimos ou frases que já estão corretas e naturais: se uma pessoa razoável aceitaria a tradução, deixe como está;
- pontuação ou maiúsculas sem impacto no sentido;
- erros que já vêm do texto em inglês.

Ao corrigir, devolva o texto PT COMPLETO do bloco já corrigido:
- mantenha as mesmas tags e marcações do PT atual (<i>, </i>, {\\an8}, ♪) nas mesmas posições;
- use "\\n" para quebra de linha; no máximo 2 linhas, cerca de 42 caracteres por linha; mantenha o número de linhas do PT atual sempre que der;
- diálogo com dois falantes continua com "-" no início de cada linha;
- português brasileiro natural e falado, coerente com o tom da cena.

Em "motivo" explique em até 15 palavras, em português. Se não houver nada a corrigir, devolva a lista vazia. Corrija o mínimo necessário."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["correcoes"],
    "properties": {
        "correcoes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "pt", "tipo", "motivo"],
                "properties": {
                    "id": {"type": "integer"},
                    "pt": {"type": "string"},
                    "tipo": {"type": "string", "enum": [
                        "sentido", "contexto", "nao_traduzido", "genero_concordancia",
                        "inconsistencia", "nome_proprio", "gramatica", "outro"]},
                    "motivo": {"type": "string"},
                },
            },
        }
    },
}

_client = None


def client():
    global _client
    if _client is None:
        if not E("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY não definida (coloque em /state/secrets.env)")
        _client = anthropic.Anthropic(max_retries=4)
    return _client


def flat(lines):
    return " ⏎ ".join(lines)


def render_chunk(label, pairs, lo, hi):
    out = [f"Título: {label}", ""]
    ctx_lo = max(0, lo - CONTEXT)
    if ctx_lo < lo:
        out.append("--- CONTEXTO (não corrigir) ---")
        for i in range(ctx_lo, lo):
            e, p = pairs[i]
            out.append(f"[{i}] EN: {flat(e['lines']) if e else '(sem original)'}\n     PT: {flat(p['lines'])}")
        out.append("--- REVISAR ---")
    for i in range(lo, hi):
        e, p = pairs[i]
        out.append(f"[{i}] EN: {flat(e['lines']) if e else '(sem original)'}\n     PT: {flat(p['lines'])}")
    return "\n".join(out)


def call_cost(model, u):
    pin, pout, pcw, pcr = PRICES.get(model, PRICE_UNKNOWN)
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    return (u.input_tokens * pin + u.output_tokens * pout + cw * pcw + cr * pcr) / 1e6, cw, cr


def estimate(text):
    tok_in = len(text) / 3 + 2000  # ~3 caracteres/token em PT/EN + prompt do sistema
    return (tok_in * PRICES[MODEL][0] + 4000 * PRICES[MODEL][1]) / 1e6


class BudgetExceeded(Exception):
    pass


class Refused(Exception):
    pass


def ask_claude(con, path, text, limit):
    est = estimate(text)
    if spent(con) + est > limit:
        raise BudgetExceeded(f"gasto do mês US$ {spent(con):.2f} + estimativa US$ {est:.2f} > limite US$ {limit:.2f}")
    resp = client().beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": text}],
    )
    cost, cw, cr = call_cost(resp.model, resp.usage)
    con.execute("insert into calls(at,month,path,model,input,output,cache_write,cache_read,cost) values(?,?,?,?,?,?,?,?,?)",
                (dt.datetime.now().isoformat(timespec="seconds"), month(), path, resp.model,
                 resp.usage.input_tokens, resp.usage.output_tokens, cw, cr, cost))
    con.commit()
    log.info("  %s: %d in / %d out tokens, US$ %.4f (mês: US$ %.2f)", resp.model, resp.usage.input_tokens,
             resp.usage.output_tokens, cost, spent(con))
    if resp.stop_reason == "refusal":
        raise Refused(str(getattr(resp, "stop_details", "")))
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("resposta cortada (max_tokens)")
    body = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(body)["correcoes"], cost


# ---------------------------------------------------------------- validação e aplicação

def validate(fix, pblock, eblock):
    """Devolve (linhas novas, None) ou (None, motivo da rejeição)."""
    new = [l.strip() for l in fix["pt"].replace("⏎", "\n").split("\n") if l.strip()]
    old = pblock["lines"]
    if not new:
        return None, "texto vazio"
    if new == [l.strip() for l in old]:
        return None, "igual ao atual"
    if len(new) > 2 and len(new) > len(old):
        return None, f"{len(new)} linhas"
    if sorted(TAG.findall("\n".join(new))) != sorted(TAG.findall("\n".join(old))):
        # caso comum: o bloco todo em itálico e o modelo esqueceu a tag
        o = "\n".join(old)
        if o.startswith("<i>") and o.endswith("</i>") and not TAG.findall("\n".join(new)):
            new[0], new[-1] = "<i>" + new[0], new[-1] + "</i>"
        else:
            return None, "tags diferentes"
    lead = re.compile(r"^(?:<[^>]+>|\{[^}]*\})*\s*")
    dash_old = sum(lead.sub("", l).startswith("-") for l in old)
    dash_new = sum(lead.sub("", l).startswith("-") for l in new)
    if dash_old >= 2 and dash_new < 2:
        return None, "perdeu o travessão de diálogo"
    return new, None


def review_file(con, row, limit, apply=True):
    path, en_path = row["path"], row["en_path"]
    pt_text, en_text = read_text(path), read_text(en_path)
    pt, en = parse_srt(pt_text), parse_srt(en_text)
    if not pt:
        return "ignorado", 0, 0, "PT vazio/ilegível"
    norm = lambda x: re.sub(r"\s+", " ", x.replace("\ufeff", "")).strip()
    if norm(write_srt(pt, pt_text)) != norm(pt_text):
        return "ignorado", 0, 0, "formato do SRT não suportado (regravar perderia conteúdo)"
    pairs, match = align(en, pt)
    if match < 0.8:
        return "ignorado", 0, 0, f"só {match:.0%} dos blocos batem com o EN (não parece tradução dele)"

    label = media_label(path)
    log.info("revisando (%d blocos): %s", len(pt), label)
    fixes, cost = [], 0.0
    for lo in range(0, len(pairs), CHUNK):
        hi = min(lo + CHUNK, len(pairs))
        got, c = ask_claude(con, path, render_chunk(label, pairs, lo, hi), limit)
        cost += c
        fixes += [f for f in got if lo <= f.get("id", -1) < hi]

    now = dt.datetime.now().isoformat(timespec="seconds")
    applied = []
    for f in fixes:
        e, p = pairs[f["id"]]
        new, why = validate(f, p, e)
        con.execute("insert into fixes(at,path,block,en,before,after,tipo,motivo,applied) values(?,?,?,?,?,?,?,?,?)",
                    (now, path, p["num"], flat(e["lines"]) if e else "", flat(p["lines"]),
                     flat(new) if new else f["pt"], f["tipo"], f["motivo"] + (f" [rejeitada: {why}]" if why else ""),
                     int(bool(new) and apply)))
        if new:
            applied.append((f, e, list(p["lines"]), new))
            p["lines"] = new
        else:
            log.info("  correção do bloco %s rejeitada (%s): %s", p["num"], why, f["pt"])
    con.commit()

    write_report(path, label, applied, cost)
    if applied and apply and not DRY_RUN:
        backup(path)
        tmp = path + ".revisor.tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(write_srt([p for _, p in pairs], pt_text))
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    for f, e, old, new in applied:
        log.info("  [%s] %s\n      antes:  %s\n      depois: %s  (%s)", f["tipo"], flat(e["lines"]) if e else "",
                 flat(old), flat(new), f["motivo"])
    return "feito", cost, len(applied), None


def backup(path):
    rel = os.path.relpath(path, MEDIA_ROOT)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_DIR, rel + f".{stamp}.orig")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(path, dst)


def write_report(path, label, applied, cost):
    d = os.path.join(REPORT_DIR, month())
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, label + ".md"), "w", encoding="utf-8") as fh:
        fh.write(f"# {label}\n\n{path}\n\n{len(applied)} correções · US$ {cost:.4f} · "
                 f"{dt.datetime.now():%d/%m/%Y %H:%M}\n\n")
        for f, e, old, new in applied:
            fh.write(f"- **{f['tipo']}** — {f['motivo']}\n  - EN: {flat(e['lines']) if e else ''}\n"
                     f"  - antes: {flat(old)}\n  - depois: {flat(new)}\n")


# ---------------------------------------------------------------- worker / comandos

def work_once(con):
    row = con.execute("select * from files where status='pendente' order by priority, added_at limit 1").fetchone()
    if not row:
        return False
    limit = BUDGET if row["priority"] == 0 else BUDGET - RESERVE
    if not os.path.exists(row["path"]) or not os.path.exists(row["en_path"] or ""):
        status, cost, n, note = "ignorado", 0, 0, "arquivo sumiu"
    else:
        try:
            status, cost, n, note = review_file(con, row, limit)
        except BudgetExceeded as e:
            if meta(con, "budget_warned") != f"{month()}:{row['priority']}":
                log.warning("orçamento: %s — pausando %s até o mês que vem", e,
                            "a fila toda" if row["priority"] == 0 else "o acervo antigo")
                meta(con, "budget_warned", f"{month()}:{row['priority']}")
            return False
        except Refused as e:
            status, cost, n, note = "recusado", 0, 0, f"modelo recusou: {e}"
        except anthropic.APIStatusError as e:
            if e.status_code in (429, 500, 502, 503, 504, 529):
                log.warning("API indisponível (%s), tento de novo depois", e.status_code)
                return False
            status, cost, n, note = "erro", 0, 0, f"API {e.status_code}: {e.message}"[:500]
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            log.warning("sem conexão com a API (%s), tento de novo depois", e)
            return False
        except Exception as e:  # noqa: BLE001 — um arquivo ruim não pode travar a fila
            log.exception("erro revisando %s", row["path"])
            status, cost, n, note = "erro", 0, 0, str(e)[:500]
    con.execute("update files set status=?, done_at=?, cost=?, n_fixes=?, note=?, pt_sha=? where path=?",
                (status, dt.datetime.now().isoformat(timespec="seconds"), cost, n, note,
                 sha(row["path"]) if os.path.exists(row["path"]) else None, row["path"]))
    con.commit()
    log.info("%s: %s (%d correções%s)", status, os.path.basename(row["path"]), n, f"; {note}" if note else "")
    return True


def servico():
    con = db()
    log.info("legenda-revisor: %s effort=%s, orçamento US$ %.2f/mês (reserva novas US$ %.2f)%s",
             MODEL, EFFORT, BUDGET, RESERVE, " [DRY_RUN]" if DRY_RUN else "")
    while not E("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY não definida em /state/secrets.env; esperando (docker restart depois de colocar)")
        time.sleep(600)
        load_secrets(os.environ.get("SECRETS_FILE", "/state/secrets.env"))
    while True:
        poll_lingarr(con)
        while work_once(con):
            pass
        time.sleep(POLL)


def cmd_revisar(args):
    force = "--forcar" in args
    con, n = db(), 0
    for a in (x for x in args if not x.startswith("--")):
        files = [a] if os.path.isfile(a) else glob.glob(os.path.join(glob.escape(a), "**", "*.srt"), recursive=True)
        for f in sorted(files):
            if re.search(r"\.(pt-BR|pt_BR|pob|pt|por)(\.\w+)?\.srt$", f) and enqueue(con, f, "manual", 1, force):
                n += 1
    print(f"{n} legendas na fila (prioridade do acervo; usam até US$ {BUDGET - RESERVE:.2f}/mês)")


def cmd_testar(args):
    """Revisa um arquivo agora e mostra as correções SEM gravar (o custo conta no orçamento)."""
    con = db()
    path = args[0]
    row = {"path": path, "en_path": en_sibling(path)}
    if not row["en_path"]:
        raise SystemExit("não achei a legenda EN ao lado")
    status, cost, n, note = review_file(con, row, BUDGET, apply=False)
    print(f"{status}: {n} correções, US$ {cost:.4f}" + (f" ({note})" if note else ""))


def cmd_status(_):
    con = db()
    s = spent(con)
    print(f"Mês {month()}: US$ {s:.2f} de US$ {BUDGET:.2f} (acervo antigo para em US$ {BUDGET - RESERVE:.2f})")
    for r in con.execute("select status, count(*) n, coalesce(sum(n_fixes),0) f from files group by status"):
        print(f"  {r['status']:10} {r['n']:5} arquivos  {r['f']:5} correções")
    print("Últimos:")
    for r in con.execute("select * from files where done_at is not null order by done_at desc limit 10"):
        print(f"  {r['done_at']}  {r['status']:9} {r['n_fixes']:3} corr. US$ {r['cost']:.3f}  "
              f"{os.path.basename(r['path'])}" + (f"  ({r['note']})" if r["note"] else ""))


def cmd_desfazer(args):
    path = args[0]
    rel = os.path.relpath(path, MEDIA_ROOT)
    cands = sorted(glob.glob(glob.escape(os.path.join(BACKUP_DIR, rel)) + ".*.orig"))
    if not cands:
        raise SystemExit("nenhum backup desse arquivo")
    shutil.copy2(cands[-1], path)
    con = db()
    con.execute("update files set status='desfeito', pt_sha=? where path=?", (sha(path), path))
    con.commit()
    print(f"restaurado de {cands[-1]}")


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else "servico"), sys.argv[2:]
    {"servico": lambda a: servico(), "revisar": cmd_revisar, "testar": cmd_testar,
     "status": cmd_status, "desfazer": cmd_desfazer}.get(cmd, lambda a: print(__doc__))(args)
