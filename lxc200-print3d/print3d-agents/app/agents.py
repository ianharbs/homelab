"""Os agentes. Tudo que sai para fora (anúncio, preço, resposta a cliente) vira RASCUNHO com botões no Telegram;
o que é interno (estoque, cartões no Vikunja, catálogo no Manyfold) é automático."""
import datetime as dt
import json
import os
import time

import custo
from common import (LIBRARY, LLM_ON, E, brl, buttons, config, db, download, esc, http, inventree, kv_get, kv_set,
                    llm, log, post, slug, spoolman, spool_price_kg, vk_link, vk_open_titles, vk_task, vikunja, VMAP)

PRINTABLES = "https://api.printables.com/graphql/"
PFILES = "https://media.printables.com/"
# Licenças que permitem vender a peça impressa. ND = pode vender sem modificar. NC / "Standard Digital File" = não.
LIC_OK = {"CC0", "CC BY", "CC BY-SA", "CC BY-ND", "GNU GPL", "GNU LGPL", "BSD", "MIT"}


def _lic_ok(abbr):
    a = (abbr or "").upper().replace("-", " ").replace("  ", " ")
    return any(a == x.upper().replace("-", " ") or a.startswith(x.upper().replace("-", " ") + " ") for x in LIC_OK) and "NC" not in a.split()


def printables(query, variables=None):
    r = http("POST", PRINTABLES, body={"query": query, "variables": variables or {}}, timeout=40)
    if r.get("errors"):
        raise RuntimeError(r["errors"][0].get("message"))
    return r["data"]


FIELDS = ("id name slug downloadCount likesCount category { name } tags { name } license { name abbreviation } "
          "image { filePath } user { publicUsername } datePublished nsfw stls { id name fileSize }")


# ================================================================== CURADOR
def curar(termo=None, n=None):
    """Busca modelos (em alta na semana, ou por termo), filtra licença comercial, a IA avalia potencial de venda
    no Brasil e risco de marca/personagem; os melhores vão para o tópico Curadoria com Aprovar/Recusar."""
    cfg = config()
    n = n or cfg["curadoria_por_semana"]
    if termo:
        q = "query($q:String!){ searchPrints2(query:$q, limit:60) { items { %s } } }" % FIELDS
        items = printables(q, {"q": termo})["searchPrints2"]["items"]
    else:
        items = []
        for order in ("-likes_count_7_days", "-download_count_7_days"):
            q = "query($o:String!){ morePrints(ordering:$o, limit:60) { items { %s } } }" % FIELDS
            items += printables(q, {"o": order})["morePrints"]["items"]
    with db() as c:
        seen = {r[0] for r in c.execute("SELECT ext_id FROM candidates WHERE source='printables'")}
    pool, ids = [], set()
    for it in items:
        if it["id"] in seen or it["id"] in ids or it.get("nsfw") or not it.get("stls"):
            continue
        if not _lic_ok((it.get("license") or {}).get("abbreviation")):
            continue
        ids.add(it["id"])
        pool.append(it)
    if not pool:
        return post("curadoria", f"🔎 Nada novo com licença comercial {'para “' + esc(termo) + '”' if termo else 'esta semana'}.")
    pool = pool[:40]
    if LLM_ON:
        lista = "\n".join(f'{i}. "{p["name"]}" | categoria {p["category"]["name"] if p.get("category") else "-"} | '
                          f'tags {", ".join(t["name"] for t in p.get("tags", [])[:8])} | {p["likesCount"]} curtidas, '
                          f'{p["downloadCount"]} downloads' for i, p in enumerate(pool))
        prompt = (f"Avalie estes modelos 3D para VENDER A PEÇA IMPRESSA no Mercado Livre/Shopee Brasil"
                  f"{' (busca do dono: ' + termo + ')' if termo else ''}.\n"
                  "Para cada um: nota 0-10 de potencial de venda (demanda real no Brasil, valor percebido, "
                  "facilidade de imprimir e enviar sem quebrar, pouca mão de obra); ZERO se usar marca, logo ou "
                  "personagem protegido (Pokémon, Disney, Marvel, times, carros etc.) mesmo com licença livre, "
                  "ou se for arma/réplica perigosa. Responda JSON: {\"itens\":[{\"i\":0,\"nota\":7,\"nicho\":\"...\","
                  "\"publico\":\"...\",\"preco_brasil\":\"R$ 30–50\",\"motivo\":\"1 frase\",\"risco\":\"nenhum|marca|..\"}]}\n\n" + lista)
        try:
            ev = {x["i"]: x for x in llm(prompt, json_out=True, temperature=0.2, writer=True).get("itens", [])}
        except Exception as e:
            log.error("curador IA: %s", e)
            ev = {}
    else:
        ev = {}
    ranked = sorted(range(len(pool)), key=lambda i: (ev.get(i, {}).get("nota", 0), pool[i]["likesCount"]), reverse=True)
    sent = 0
    for i in ranked:
        p, e = pool[i], ev.get(i, {})
        if ev and e.get("nota", 0) < 5:
            continue
        with db() as c:
            cur = c.execute("INSERT OR IGNORE INTO candidates (source, ext_id, name, url, license, image, data, score, created)"
                            " VALUES ('printables',?,?,?,?,?,?,?,?)",
                            (p["id"], p["name"], f"https://www.printables.com/model/{p['id']}-{p['slug']}",
                             p["license"]["abbreviation"], PFILES + p["image"]["filePath"] if p.get("image") else None,
                             json.dumps({**p, "ia": e}), e.get("nota", 0), time.time()))
            cid = cur.lastrowid
        mb = sum(s.get("fileSize") or 0 for s in p["stls"]) / 1e6
        txt = (f"🔎 <b>{esc(p['name'])}</b>\n"
               f"{'⭐ ' + str(e.get('nota')) + '/10 · ' if e else ''}{esc(e.get('nicho', p['category']['name'] if p.get('category') else ''))}\n"
               + (f"👥 {esc(e.get('publico', ''))}\n💵 Mercado: {esc(e.get('preco_brasil', '?'))}\n💡 {esc(e.get('motivo', ''))}\n" if e else "")
               + f"📜 {esc(p['license']['name'])} · ❤️ {p['likesCount']} · ⬇️ {p['downloadCount']}\n"
               f"📁 {len(p['stls'])} STL ({mb:.1f} MB) · por {esc(p['user']['publicUsername'])}\n"
               f"<a href=\"https://www.printables.com/model/{p['id']}-{p['slug']}\">ver no Printables</a>")
        post("curadoria", txt, photo=PFILES + p["image"]["filePath"] if p.get("image") else None,
             kb=buttons([[("✅ Aprovar", f"cur:ok:{cid}"), ("❌ Recusar", f"cur:no:{cid}")]]))
        sent += 1
        if sent >= n:
            break
    if not sent:
        post("curadoria", "🔎 Os modelos novos desta busca não passaram na avaliação (nota < 5 ou risco de marca).")


def aprovar_candidato(cid):
    """Baixa os arquivos para a biblioteca do Manyfold, calcula custo/preço e cria o cartão em Ideias de produtos."""
    with db() as c:
        cand = c.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
    if not cand:
        return "candidato não encontrado"
    p = json.loads(cand["data"])
    folder = os.path.join(LIBRARY, "Catalogo", f"{slug(p['name'])}-{p['id']}")
    os.makedirs(folder, exist_ok=True)
    paths, total = [], 0
    for s in p["stls"][:12]:
        if (s.get("fileSize") or 0) > 80e6 or total > 200e6:
            continue
        link = printables('mutation($id:ID!,$p:ID!){ getDownloadLink(id:$id, printId:$p, fileType:stl, source:model_detail)'
                          '{ ok output { link } } }', {"id": s["id"], "p": p["id"]})["getDownloadLink"]["output"]["link"]
        dest = os.path.join(folder, os.path.basename(s["name"]) or f"{s['id']}.stl")
        total += download(link, dest)
        paths.append(dest)
    if p.get("image"):
        try:
            download(PFILES + p["image"]["filePath"], os.path.join(folder, "capa" + os.path.splitext(p["image"]["filePath"])[1]), 15)
        except Exception as e:
            log.warning("capa: %s", e)
    with open(os.path.join(folder, "datapackage.json"), "w") as f:   # metadados que o Manyfold importa no scan
        json.dump({"name": slug(p["name"]).lower(), "title": p["name"], "homepage": cand["url"],
                   "licenses": [{"name": p["license"]["abbreviation"], "title": p["license"]["name"]}],
                   "contributors": [{"title": p["user"]["publicUsername"], "roles": ["creator"]}],
                   "keywords": [t["name"] for t in p.get("tags", [])] + ["print3d-agents"]}, f, ensure_ascii=False, indent=2)
    a = custo.analyze(paths)
    pr = custo.price(a["grams"], a["hours"])
    sku = "PRD-" + slug(p["name"], 24).upper()
    ia = p.get("ia") or {}
    html = (f"<p><b>Origem:</b> <a href=\"{cand['url']}\">{esc(p['name'])}</a> — {esc(p['license']['name'])} "
            f"(autor {esc(p['user']['publicUsername'])}; dê o crédito no anúncio se a licença for BY)</p>"
            f"<p><b>SKU:</b> {sku}<br><b>Arquivos:</b> {esc(folder.replace(LIBRARY, 'Manyfold: '))}</p>"
            f"<p><b>Estimativa:</b> {a['grams']:.0f} g · {a['hours']:.1f} h · custo {brl(pr['cost'])}<br>"
            + "<br>".join(f"{v['name']}: {brl(v['price'])}" for v in pr["prices"].values()) + "</p>"
            + (f"<p><b>IA:</b> nota {ia.get('nota')} — {esc(ia.get('motivo', ''))}</p>" if ia else "")
            + "<p>Próximo: imprimir um teste, fotografar, gerar o anúncio.</p>")
    task = vk_task("ideias", p["name"], html, labels=["Catálogo"])
    with db() as c:
        c.execute("UPDATE candidates SET status='aprovado' WHERE id=?", (cid,))
        cur = c.execute("INSERT INTO products (name, sku, candidate_id, folder, grams, hours, estimated, cost, prices,"
                        " vikunja_task, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (p["name"], sku, cid, folder, a["grams"], a["hours"], int(a["estimated"]), pr["cost"],
                         json.dumps(pr["prices"]), task["id"], time.time(), time.time()))
        pid = cur.lastrowid
    msg = post("curadoria", f"✅ <b>{esc(p['name'])}</b> no catálogo ({len(paths)} arquivos)\n"
               + custo.summary(a["grams"], a["hours"], a["estimated"], pr)
               + f"\n🗂 <a href=\"{vk_link(task['id'])}\">cartão em Ideias de produtos</a>",
               kb=buttons([[("📝 Gerar anúncio", f"anu:new:{pid}")]]))
    if msg:
        kv_set(f"msg_{msg['message_id']}", pid)
    return None


# ================================================================== ANUNCIANTE
def _cut(t, n):
    """Corta no último espaço antes do limite (nunca no meio da palavra) e tira conectivo solto no fim."""
    t = " ".join(str(t).split())
    if len(t) <= n:
        return t
    t = t[:n + 1].rsplit(" ", 1)[0]
    words = t.split()
    while words and words[-1].lower() in {"de", "da", "do", "para", "com", "e", "em", "a", "o", "p/"}:
        words.pop()
    return " ".join(words)


def anunciar(pid=None, texto=None):
    """Rascunho de anúncio p/ Mercado Livre e Shopee (título, descrição, palavras-chave, atributos, fotos)."""
    cfg = config()
    prod = None
    if pid:
        with db() as c:
            prod = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if prod:
        prices = json.loads(prod["prices"])
        cand = None
        with db() as c:
            if prod["candidate_id"]:
                cand = c.execute("SELECT * FROM candidates WHERE id=?", (prod["candidate_id"],)).fetchone()
        info = json.loads(cand["data"]) if cand else {}
        base = (f"Produto: {prod['name']}\nTags do modelo: {', '.join(t['name'] for t in info.get('tags', []))}\n"
                f"Licença: {info.get('license', {}).get('name', '-')} (autor {info.get('user', {}).get('publicUsername', '-')})\n"
                f"Peso aprox.: {prod['grams']:.0f} g. Preços: ML {brl(prices['ml']['price'])}, Shopee {brl(prices['shopee']['price'])}.")
    else:
        base = f"Produto descrito pelo dono: {texto}"
        prices = None
    prompt = (base + "\n\nEscreva o anúncio em português do Brasil correto, COM acentos e maiúsculas normais "
              "(primeira letra das frases e nomes próprios em maiúscula; só não escreva palavras inteiras em CAIXA ALTA). "
              "Regras: Mercado Livre título até 60 caracteres, sem emoji, "
              "palavras que o comprador busca primeiro; Shopee título até 100 caracteres. Descrição em texto puro "
              "(sem HTML, sem links, sem contato), com benefícios, medidas aproximadas (deixe [MEDIDAS] se não souber), "
              "material PLA, cores disponíveis, cuidados (não deixar no sol/carro quente), prazo de produção, e que é "
              "impresso em 3D (linhas de camada são normais). Se a licença for BY, inclua crédito ao autor no fim. "
              "Responda JSON: {\"ml\":{\"titulo\":\"\",\"descricao\":\"\",\"categoria_sugerida\":\"\","
              "\"atributos\":{\"Marca\":\"Genérica\",\"Material\":\"PLA\"}},\"shopee\":{\"titulo\":\"\",\"descricao\":\"\","
              "\"hashtags\":[\"\"]},\"palavras_chave\":[\"\"],\"fotos\":[\"lista curta de fotos a tirar\"],"
              "\"variacoes\":[\"cores/tamanhos sugeridos\"]}")
    d = llm(prompt, json_out=True, temperature=0.6, writer=True)
    d["ml"]["titulo"] = _cut(d["ml"]["titulo"], 60)
    d["shopee"]["titulo"] = _cut(d["shopee"]["titulo"], 100)
    with db() as c:
        did = c.execute("INSERT INTO drafts (product_id, kind, data, created) VALUES (?,?,?,?)",
                        (pid, "anuncio", json.dumps({"texto": texto, **d}), time.time())).lastrowid
    pr_txt = (f"\n💵 ML <b>{brl(prices['ml']['price'])}</b> · Shopee <b>{brl(prices['shopee']['price'])}</b>" if prices else "")
    txt = (f"📝 <b>Rascunho de anúncio #{did}</b>{pr_txt}\n\n"
           f"<b>Mercado Livre</b> ({len(d['ml']['titulo'])}/60)\n<code>{esc(d['ml']['titulo'])}</code>\n"
           f"Categoria: {esc(d['ml'].get('categoria_sugerida', ''))}\n"
           f"Atributos: {esc(', '.join(f'{k}: {v}' for k, v in d['ml'].get('atributos', {}).items()))}\n\n"
           f"<b>Shopee</b>\n<code>{esc(d['shopee']['titulo'])}</code>\n{esc(' '.join(d['shopee'].get('hashtags', [])))}\n\n"
           f"<b>Descrição</b>\n<pre>{esc(d['ml']['descricao'][:2500])}</pre>\n"
           f"📸 Fotos: {esc('; '.join(d.get('fotos', [])))}\n🎨 Variações: {esc('; '.join(d.get('variacoes', [])))}")
    post("anuncios", txt, kb=buttons([[("✅ Aprovar", f"anu:ok:{did}"), ("🔁 Refazer", f"anu:redo:{did}"),
                                        ("❌ Descartar", f"anu:no:{did}")]]))


def aprovar_anuncio(did):
    with db() as c:
        d = c.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
        c.execute("UPDATE drafts SET status='aprovado' WHERE id=?", (did,))
        prod = c.execute("SELECT * FROM products WHERE id=?", (d["product_id"],)).fetchone() if d["product_id"] else None
    data = json.loads(d["data"])
    os.makedirs("/state/anuncios", exist_ok=True)
    path = f"/state/anuncios/{did:04d}-{slug(data['ml']['titulo'], 40)}.md"
    with open(path, "w") as f:
        f.write(f"# {data['ml']['titulo']}\n\n## Mercado Livre\n{data['ml']['titulo']}\n\n{data['ml']['descricao']}\n\n"
                f"Atributos: {json.dumps(data['ml'].get('atributos', {}), ensure_ascii=False)}\n\n## Shopee\n"
                f"{data['shopee']['titulo']}\n\n{data['shopee']['descricao']}\n\n{' '.join(data['shopee'].get('hashtags', []))}\n")
    if prod and prod["vikunja_task"]:
        try:
            t = vikunja("GET", f"/tasks/{prod['vikunja_task']}")
            t["description"] = (t.get("description") or "") + (
                f"<h3>Anúncio aprovado</h3><p><b>ML:</b> {esc(data['ml']['titulo'])}<br><b>Shopee:</b> "
                f"{esc(data['shopee']['titulo'])}</p><pre>{esc(data['ml']['descricao'])}</pre>")
            vikunja("POST", f"/tasks/{prod['vikunja_task']}", t)
        except Exception as e:
            log.warning("vikunja anúncio: %s", e)
    with db() as c:
        if prod:
            c.execute("UPDATE products SET status='anunciar', updated=? WHERE id=?", (time.time(), prod["id"]))
    import mercado
    return mercado.publicar(data, prod)


# ================================================================== ESTOQUE
def estoque(force=False):
    """Carretéis abaixo do limite (Spoolman) e insumos abaixo do mínimo (InvenTree) → tarefas em Compras."""
    cfg = config()
    faltas = []
    try:
        for s in spoolman("/spool?allow_archived=false"):
            rest = s.get("remaining_weight")
            if rest is not None and rest < cfg["low_spool_g"]:
                f = s.get("filament") or {}
                nome = f"Filamento {f.get('material', '')} {f.get('name', '')} ({(f.get('vendor') or {}).get('name', '')})".replace(" ()", "")
                faltas.append((nome, f"Carretel #{s['id']} com {rest:.0f} g", "spoolman"))
    except Exception as e:
        log.error("estoque/spoolman: %s", e)
    try:
        for p in inventree("GET", "/part/?low_stock=true&active=true&limit=200").get("results", []):
            faltas.append((p["name"], f"{p.get('in_stock', 0):g} em estoque, mínimo {p.get('minimum_stock', 0):g} "
                           f"{p.get('units') or ''} (IPN {p.get('IPN') or '-'})", "inventree"))
    except Exception as e:
        log.error("estoque/inventree: %s", e)
    abertas = vk_open_titles("compras")
    novas = []
    for nome, det, src in faltas:
        title = f"Comprar: {nome}"
        if title in abertas:
            continue
        vk_task("compras", title, f"<p>{esc(det)}</p><p>Origem: {src} (agente de estoque)</p>")
        novas.append(f"• {esc(nome)} — {esc(det)}")
    if novas or force:
        post("estoque", (f"📦 <b>{len(novas)} item(ns) novos para comprar</b>\n" + "\n".join(novas[:40]) if novas
                         else "📦 Nada novo para comprar.")
             + f"\n\nTotal abaixo do mínimo: {len(faltas)} · lista em <a href=\"{E('VIKUNJA_PUBLIC', '')}/projects/{VMAP['projects']['compras']}\">Compras</a>")


def snapshot_filamento():
    """Guarda o total de filamento usado (para o relatório semanal)."""
    try:
        used = sum(s.get("used_weight") or 0 for s in spoolman("/spool?allow_archived=true"))
        kv_set(f"used_{dt.date.today().isoformat()}", used)
    except Exception as e:
        log.warning("snapshot: %s", e)


# ================================================================== PEDIDOS (manual: WhatsApp/Instagram/balcão)
def pedido(texto):
    hoje = dt.date.today().isoformat()
    d = llm(f"Hoje é {hoje}. Extraia o pedido desta mensagem do dono do negócio:\n\"{texto}\"\n"
            "JSON: {\"cliente\":\"\",\"contato\":\"\",\"canal\":\"WhatsApp|Instagram|Mercado Livre|Shopee|Encomenda|outro\","
            "\"itens\":[{\"produto\":\"\",\"qtd\":1,\"cor\":\"\",\"obs\":\"\"}],\"valor_total\":0,\"pago\":\"não|sinal|sim\","
            "\"entrega\":\"retirada|envio|?\",\"prazo\":\"AAAA-MM-DD ou vazio\",\"aprovado\":true,\"observacoes\":\"\"}",
            json_out=True, temperature=0)
    itens = "; ".join(f"{i.get('qtd', 1)}× {i.get('produto', '')}{' ' + i['cor'] if i.get('cor') else ''}" for i in d.get("itens", []))
    title = f"{d.get('cliente') or 'Cliente'} — {itens}"[:200]
    html = (f"<p><b>Cliente:</b> {esc(d.get('cliente'))} {esc(d.get('contato'))}<br><b>Itens:</b> {esc(itens)}<br>"
            f"<b>Valor:</b> {brl(d.get('valor_total') or 0)} · <b>Pago:</b> {esc(d.get('pago'))}<br>"
            f"<b>Entrega:</b> {esc(d.get('entrega'))}</p><p>{esc(d.get('observacoes'))}</p><p><i>Mensagem original:</i> {esc(texto)}</p>")
    due = f"{d['prazo']}T18:00:00-03:00" if d.get("prazo") and len(d["prazo"]) == 10 else None
    canal = {"WhatsApp": "WhatsApp", "Instagram": "Instagram", "Mercado Livre": "Mercado Livre", "Shopee": "Shopee"}.get(d.get("canal"), "Encomenda")
    t = vk_task("pedidos", title, html, labels=[canal], bucket="Aprovado" if d.get("aprovado") else "Orçamento", due=due)
    post("pedidos", f"🧾 Pedido criado: <b>{esc(title)}</b>\n{brl(d.get('valor_total') or 0)} · {esc(canal)}"
         f"{' · prazo ' + d['prazo'] if d.get('prazo') else ''}\n<a href=\"{vk_link(t['id'])}\">abrir no Vikunja</a>")


# ================================================================== GERENTE (relatório semanal)
def relatorio():
    hoje = dt.date.today()
    semana = (hoje - dt.timedelta(days=7)).isoformat()
    linhas = [f"📊 <b>Semana até {hoje.strftime('%d/%m')}</b>"]
    try:
        pid = VMAP["projects"]["pedidos"]
        done = vikunja("GET", f"/projects/{pid}/tasks?filter=done%3Dtrue%20%26%26%20done_at%3E%3D{semana}&per_page=200") or []
        abertos = vikunja("GET", f"/projects/{pid}/tasks?filter=done%3Dfalse&per_page=200") or []
        linhas.append(f"🧾 Pedidos entregues: <b>{len(done)}</b> · em aberto: <b>{len(abertos)}</b>")
        atras = [t for t in abertos if t.get("due_date") and not t["due_date"].startswith("0001") and t["due_date"][:10] < hoje.isoformat()]
        if atras:
            linhas.append("⚠️ Atrasados: " + "; ".join(esc(t["title"][:40]) for t in atras[:8]))
    except Exception as e:
        linhas.append(f"(Vikunja indisponível: {esc(e)})")
    a, b = kv_get(f"used_{semana}"), kv_get(f"used_{hoje.isoformat()}")
    if a and b:
        linhas.append(f"🧵 Filamento usado: <b>{float(b) - float(a):.0f} g</b>")
    with db() as c:
        ap = c.execute("SELECT COUNT(*) FROM candidates WHERE status='aprovado' AND created>=?", (time.time() - 7 * 86400,)).fetchone()[0]
        an = c.execute("SELECT COUNT(*) FROM drafts WHERE status='aprovado' AND created>=?", (time.time() - 7 * 86400,)).fetchone()[0]
    linhas.append(f"🔎 Modelos aprovados: {ap} · 📝 anúncios aprovados: {an}")
    try:
        low = inventree("GET", "/part/?low_stock=true&active=true&limit=1").get("count", 0)
        linhas.append(f"📦 Itens abaixo do mínimo: {low}")
    except Exception:
        pass
    texto = "\n".join(linhas)
    if LLM_ON:
        try:
            texto += "\n\n💡 " + esc(llm("Com base neste resumo semanal, dê UMA sugestão prática e curta (1–2 frases) "
                                         "para o dono melhorar vendas ou produção:\n" + texto, temperature=0.7))
        except Exception as e:
            log.warning("gerente IA: %s", e)
    post("relatorios", texto)


# ================================================================== CHAT (tópico Geral)
def conversa(texto):
    cfg = config()
    with db() as c:
        prods = [dict(r) for r in c.execute("SELECT name, sku, grams, hours, cost, status FROM products ORDER BY id DESC LIMIT 20")]
    ctx = {"config": {k: v for k, v in cfg.items() if k != "channels"}, "canais": cfg["channels"], "produtos": prods}
    try:
        ctx["carreteis"] = [{"id": s["id"], "filamento": (s.get("filament") or {}).get("name"),
                             "material": (s.get("filament") or {}).get("material"), "restante_g": s.get("remaining_weight"),
                             "rs_kg": spool_price_kg(s)} for s in spoolman("/spool?allow_archived=false")]
    except Exception:
        pass
    return llm("Contexto do negócio (JSON):\n" + json.dumps(ctx, ensure_ascii=False, default=str)[:12000]
               + f"\n\nPergunta do dono: {texto}\nResponda curto. Se ele quiser uma ação, indique o comando: "
               "/curar [termo], /anunciar <descrição>, /pedido <texto>, /estoque, /custo (respondendo um arquivo), "
               "/relatorio, /config chave valor.")
