"""Marketplaces. Mercado Livre via API oficial (OAuth do app do vendedor: ML_CLIENT_ID/ML_CLIENT_SECRET/
ML_REFRESH_TOKEN em secrets.env). Shopee: Open Platform exige conta de vendedor aprovada → por enquanto o anúncio
aprovado fica salvo para colar (o mesmo vale para o ML enquanto o app não estiver configurado).

AINDA NÃO TESTADO contra uma conta real: perguntas/pedidos do ML ligam sozinhos quando as credenciais existirem;
a publicação automática de anúncio será feita e testada com a conta (categoria + atributos obrigatórios variam)."""
import json
import time

from common import E, LLM_ON, brl, buttons, db, esc, http, kv_get, kv_set, llm, log, post, vk_task

ML = "https://api.mercadolibre.com"


def ml_on():
    return bool(E("ML_CLIENT_ID") and E("ML_CLIENT_SECRET") and (kv_get("ml_refresh") or E("ML_REFRESH_TOKEN")))


def ml_token():
    tok, exp = kv_get("ml_access"), float(kv_get("ml_access_exp", "0"))
    if tok and exp > time.time() + 120:
        return tok
    r = http("POST", f"{ML}/oauth/token", form={"grant_type": "refresh_token", "client_id": E("ML_CLIENT_ID"),
                                                "client_secret": E("ML_CLIENT_SECRET"),
                                                "refresh_token": kv_get("ml_refresh") or E("ML_REFRESH_TOKEN")})
    kv_set("ml_access", r["access_token"])
    kv_set("ml_access_exp", time.time() + r.get("expires_in", 21600))
    kv_set("ml_refresh", r["refresh_token"])          # o refresh token do ML é de uso único: guardar o novo
    kv_set("ml_user", r.get("user_id", ""))
    return r["access_token"]


def ml(method, path, body=None):
    return http(method, ML + path, {"Authorization": "Bearer " + ml_token()}, body, timeout=30)


def publicar(data, prod):
    if ml_on():
        return ("✅ Aprovado e salvo. A publicação automática no ML será ligada depois de testarmos com a sua conta "
                "(categoria e atributos obrigatórios). Por enquanto copie do rascunho acima.")
    return ("✅ Aprovado e salvo (anúncio em /state/anuncios e no cartão do produto). Copie o título e a descrição "
            "do rascunho acima para o Mercado Livre e a Shopee. Publicação automática quando o app do ML estiver configurado.")


# ------------------------------------------------------------------ Atendente (perguntas do ML)
def perguntas():
    if not ml_on():
        return
    seller = kv_get("ml_user") or ml("GET", "/users/me")["id"]
    r = ml("GET", f"/questions/search?seller_id={seller}&status=UNANSWERED&api_version=4&sort_fields=date_created&sort_types=DESC")
    for q in r.get("questions", []):
        if kv_get(f"mlq_{q['id']}"):
            continue
        item = ml("GET", f"/items/{q['item_id']}")
        rasc = ""
        if LLM_ON:
            try:
                rasc = llm(f"Pergunta de um comprador no Mercado Livre sobre o anúncio \"{item.get('title')}\" "
                           f"(preço {brl(item.get('price'))}):\n\"{q['text']}\"\nEscreva a resposta do vendedor: cordial, "
                           "curta, sem contato externo, sem links, sem prometer o que não sabe (se não souber, diga que "
                           "vai confirmar). Termine com 'Obrigado pela pergunta!'", temperature=0.3)
            except Exception as e:
                log.warning("atendente IA: %s", e)
        with db() as c:
            did = c.execute("INSERT INTO drafts (kind, data, created) VALUES ('resposta_ml',?,?)",
                            (json.dumps({"question_id": q["id"], "text": rasc, "pergunta": q["text"],
                                         "item": item.get("title")}), time.time())).lastrowid
        kv_set(f"mlq_{q['id']}", did)
        post("pedidos", f"❓ <b>Pergunta no ML</b> — {esc(item.get('title'))}\n“{esc(q['text'])}”\n\n"
             f"<b>Resposta sugerida:</b>\n{esc(rasc) or '(sem IA — responda no app)'}",
             kb=buttons([[("✅ Enviar resposta", f"mlq:ok:{did}"), ("❌ Eu respondo", f"mlq:no:{did}")]]))


def responder(did):
    with db() as c:
        d = json.loads(c.execute("SELECT data FROM drafts WHERE id=?", (did,)).fetchone()[0])
        c.execute("UPDATE drafts SET status='aprovado' WHERE id=?", (did,))
    ml("POST", "/answers", {"question_id": d["question_id"], "text": d["text"][:2000]})
    return "✅ Resposta enviada no Mercado Livre."


# ------------------------------------------------------------------ Pedidos do ML → Vikunja
def pedidos_ml():
    if not ml_on():
        return
    seller = kv_get("ml_user") or ml("GET", "/users/me")["id"]
    r = ml("GET", f"/orders/search?seller={seller}&order.status=paid&sort=date_desc&limit=20")
    for o in r.get("results", []):
        if kv_get(f"mlo_{o['id']}"):
            continue
        itens = "; ".join(f"{i['quantity']}× {i['item']['title']}" for i in o.get("order_items", []))
        comprador = (o.get("buyer") or {}).get("nickname", "comprador")
        t = vk_task("pedidos", f"ML {o['id']} — {comprador} — {itens}"[:200],
                    f"<p><b>Pedido ML {o['id']}</b><br>{esc(itens)}<br>Total {brl(o.get('total_amount'))}</p>",
                    labels=["Mercado Livre"], bucket="Fila de impressão")
        kv_set(f"mlo_{o['id']}", t["id"])
        post("pedidos", f"🛒 <b>Venda no Mercado Livre!</b>\n{esc(itens)} · {brl(o.get('total_amount'))}\n"
             f"Cartão criado na Fila de impressão.")
