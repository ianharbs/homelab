"""Gera o dashboard "Casa" (url dashboard-casa) do Home Assistant.

Uso no pve:  python3 vm106-homeassistant/dashboards/casa.py > casa.json
O JSON é salvo no HA com `lovelace/config/save` (url_path=dashboard-casa).

Pensado para celular, no estilo do dashboard antigo do apartamento:
- Início: saudação, contadores (luzes/portas/janelas/movimento/bateria/clima), pessoas,
  atalhos, "agora em casa" (o que está aceso/aberto/ativo) e um card por cômodo.
- Uma subview por cômodo (toque no card do cômodo) com luzes, sensores, Echo e aparelhos.
- Abas Luzes, Segurança, Alexa (com anúncio) e subviews de portas, janelas, movimento,
  baterias, clima e rede/PC.
Listas de sensores usam auto-entities por área/device_class: sensor novo na Tuya aparece sozinho.

Cards usados (HACS): mushroom, room-summary-card, auto-entities, battery-state-card,
mini-graph-card, weather-card, weather-radar-card.
"""

import json

DASH = "/dashboard-casa"

# ---------------------------------------------------------------- casa

PISOS = [
    ("casa_pais", "Casa Pais", "mdi:home", "script.apagar_casa_pais"),
    ("area_externa", "Área Externa", "mdi:tree", "script.apagar_area_externa"),
    ("edicula", "Edícula", "mdi:home-floor-1", "script.apagar_edicula"),
]

# area_id, nome, ícone, piso, path, luzes [(entity, nome)], echo, extras
COMODOS = [
    dict(area="sala", nome="Sala", icon="mdi:sofa", piso="casa_pais", path="sala",
         luzes=[("light.sala", "Sala")], echo="media_player.echo_show_sala",
         camera="camera.smart_camera", aspirador="vacuum.aspirador_robo",
         midia=["media_player.sala_de_estar"]),
    dict(area="cozinha", nome="Cozinha", icon="mdi:countertop", piso="casa_pais", path="cozinha",
         luzes=[("light.cozinha", "Cozinha")], echo="media_player.echo_show_sala"),
    dict(area="quarto_pais", nome="Quarto Pais", icon="mdi:bed-king", piso="casa_pais", path="quarto-pais",
         luzes=[("light.quarto_pais", "Quarto")], echo="media_player.echo_dot_quarto_pais"),
    dict(area="quarto_visita", nome="Quarto Visitas", icon="mdi:bed", piso="casa_pais", path="quarto-visitas",
         luzes=[("light.quarto_visitas", "Quarto"), ("light.cabeceira", "Cabeceira")],
         echo="media_player.echo_pop_quarto_visitas"),
    dict(area="banheiro_2", nome="Banheiro", icon="mdi:shower", piso="casa_pais", path="banheiro-casa",
         luzes=[("light.banheiro_casa", "Banheiro")], echo="media_player.echo_dot_banheiro_casa"),
    dict(area="despensa", nome="Despensa", icon="mdi:archive", piso="casa_pais", path="despensa",
         luzes=[], alimentador=("number.alimentador_alimentar", "Alimentador Interno",
                                "binary_sensor.alimentador_alimentando", "sensor.alimentador_ultimo_valor")),
    dict(area="patio", nome="Pátio", icon="mdi:outdoor-lamp", piso="area_externa", path="patio",
         luzes=[("light.frente", "Frente")]),
    dict(area="garagem", nome="Garagem", icon="mdi:garage", piso="area_externa", path="garagem",
         luzes=[("light.garagem", "Garagem")], portao="cover.portao_garagem",
         alimentador=("number.alimentador_alimentar_2", "Alimentador Externo",
                      "binary_sensor.alimentador_alimentando_2", "sensor.alimentador_ultimo_valor_2")),
    dict(area="churrasqueira", nome="Churrasqueira", icon="mdi:grill", piso="area_externa", path="churrasqueira",
         luzes=[("light.churrasqueira_esquerda", "Esquerda"), ("light.churrasqueira_direita", "Direita")],
         echo="media_player.echo_dot_churrasqueira", midia=["media_player.firetv_churrasqueira"]),
    dict(area="edicula", nome="Externo", icon="mdi:home-floor-1", piso="edicula", path="externo-edicula",
         luzes=[("light.externo_edicula", "Externo")]),
    dict(area="escritorio_2", nome="Escritório", icon="mdi:desk", piso="edicula", path="escritorio",
         luzes=[("light.escritorio", "Escritório")], echo="media_player.echo_show_escritorio",
         pc=True),
    dict(area="suite", nome="Suíte", icon="mdi:bed-queen", piso="edicula", path="suite",
         luzes=[("light.suite", "Suíte"), ("light.luminaria", "Luminária")], echo="media_player.echo_spot_suite",
         clima="climate.ar_suite",
         midia=["media_player.tv_do_quarto_2", "media_player.quarto", "media_player.firetv_suite"]),
    dict(area="banheiro_edicula", nome="Banheiro", icon="mdi:shower-head", piso="edicula", path="banheiro-edicula",
         luzes=[("light.banheiro_edicula", "Banheiro"), ("light.espelho", "Espelho")],
         echo="media_player.echo_dot_banheiro_edicula"),
]

ECHOS = [  # (entity, nome, piso)
    ("media_player.echo_show_sala", "Sala", "casa_pais"),
    ("media_player.echo_dot_quarto_pais", "Quarto Pais", "casa_pais"),
    ("media_player.echo_pop_quarto_visitas", "Quarto Visitas", "casa_pais"),
    ("media_player.echo_dot_banheiro_casa", "Banheiro Casa", "casa_pais"),
    ("media_player.echo_dot_churrasqueira", "Churrasqueira", "area_externa"),
    ("media_player.echo_show_escritorio", "Escritório", "edicula"),
    ("media_player.echo_spot_suite", "Suíte", "edicula"),
    ("media_player.echo_dot_banheiro_edicula", "Banheiro Edícula", "edicula"),
]

PESSOAS = ["person.ian_augusto", "person.ana_paula"]

# ---------------------------------------------------------------- blocos


def nav(path):
    return {"action": "navigate", "navigation_path": f"{DASH}/{path}"}


def run(entity, **data):
    domain = entity.split(".")[0]
    act = {"action": "perform-action", "perform_action": f"{domain}.turn_on", "target": {"entity_id": entity}}
    if data:
        act["data"] = data
    return act


def cols(card, n):
    card["grid_options"] = {**card.get("grid_options", {}), "columns": n}
    return card


def secao(*cards, span=None):
    s = {"type": "grid", "cards": [c for c in cards if c]}
    if span:
        s["column_span"] = span
    return s


def titulo(texto, icon=None, sub=False, badges=None):
    c = {"type": "heading", "heading": texto, "heading_style": "subtitle" if sub else "title"}
    if icon:
        c["icon"] = icon
    if badges:
        c["badges"] = badges
    return c


def badge_apagar(script):
    return {"type": "entity", "entity": script, "show_state": False, "show_icon": True, "color": "red",
            "tap_action": {**run(script), "confirmation": {"text": "Apagar estas luzes?"}}}


def voltar():
    return {"type": "custom:mushroom-chips-card", "chips": [{"type": "back"}]}


def menu(nome, icon, cor, acao):
    return {"type": "custom:mushroom-template-card", "primary": nome, "icon": icon, "icon_color": cor,
            "layout": "vertical", "tap_action": acao, "fill_container": True}


def luz_card(entity, nome):
    return cols({"type": "custom:mushroom-light-card", "entity": entity, "name": nome,
                 "use_light_color": True, "fill_container": True, "layout": "vertical",
                 "tap_action": {"action": "toggle"}, "hold_action": {"action": "more-info"}}, 6)


def echo_card(entity, nome=None):
    c = {"type": "custom:mushroom-media-player-card", "entity": entity, "use_media_info": True,
         "show_volume_level": True, "fill_container": True,
         "volume_controls": ["volume_mute", "volume_set"],
         "media_controls": ["play_pause_stop", "previous", "next"]}
    if nome:
        c["name"] = nome
    return cols(c, 12)


def lista_auto(filtros, vazio, colunas=2, sort="name"):
    """Grid de tiles gerado pelo auto-entities; mostra `vazio` quando não há nada."""
    return {
        "type": "custom:auto-entities",
        "card": {"type": "grid", "columns": colunas, "square": False},
        "card_param": "cards",
        "filter": {"include": filtros, "exclude": [{"integration": "group"}, {"state": "unavailable"}]},
        "sort": {"method": sort},
        "show_empty": True,
        "else": {"type": "custom:mushroom-template-card", "primary": vazio, "icon": "mdi:check-circle",
                 "icon_color": "green"},
        "grid_options": {"columns": 12},
    }


def tile_opts(**extra):
    return {"type": "tile", **extra}


# ---------------------------------------------------------------- início

SAUDACAO = """{% set h = now().hour %}
{% if h >= 18 or h < 5 %}# Boa noite, {{ user }}!{% elif h >= 12 %}# Boa tarde, {{ user }}!{% else %}# Bom dia, {{ user }}!{% endif %}"""


def contador(entity, path, cor=None, esconder_zero=False):
    b = {"type": "entity", "entity": entity, "show_name": False, "show_state": True, "show_icon": True,
         "tap_action": nav(path)}
    if cor:
        b["color"] = cor
    if esconder_zero:
        b["visibility"] = [{"condition": "state", "entity": entity, "state_not": "0"}]
    return b


def luzes_do_comodo_on(c):
    if not c["luzes"]:
        return "0"
    ids = ", ".join(f"'{e}'" for e, _ in c["luzes"])
    return f"{{{{ [{ids}] | select('is_state', 'on') | list | count }}}}"


def card_comodo(c):
    luzes = [e for e, _ in c["luzes"]]
    entities = [{"entity_id": e, "label": n, "on_color": "amber"} for e, n in c["luzes"]]
    for k, icon in (("clima", None), ("portao", None)):
        if c.get(k):
            entities.append({"entity_id": c[k]})
    if c.get("echo") and c["area"] != "cozinha":
        entities.append({"entity_id": c["echo"], "icon": "mdi:speaker",
                         "tap_action": {"action": "more-info"}})
    if c.get("alimentador"):
        entities.append({"entity_id": c["alimentador"][2], "icon": "mdi:food-drumstick",
                         "tap_action": {"action": "more-info"}})
    card = {
        "type": "custom:room-summary-card",
        "area": c["area"],
        "area_name": c["nome"],
        "entities": entities[:4],
        "features": ["exclude_default_entities", "multi_light_background", "hide_area_stats", "sticky_entities"],
        "actions": {"tap_action": nav(c["path"])},
        "occupancy": {"entities": []},
        "grid_options": {"columns": 6, "rows": 3},
    }
    if luzes:
        card["entity"] = {"entity_id": luzes[0], "icon": c["icon"], "tap_action": {"action": "toggle"}}
        card["lights"] = luzes
    else:
        card["entity"] = {"entity_id": c["alimentador"][0] if c.get("alimentador") else luzes[0],
                          "icon": c["icon"], "tap_action": nav(c["path"])}
    if c["area"] == "sala":
        card["occupancy"]["entities"] = ["binary_sensor.presenca_sala"]
    if not card["occupancy"]["entities"]:
        del card["occupancy"]
    return card


AGORA_FILTROS = [
    {"domain": "light", "state": "on", "options": tile_opts(color="amber", tap_action={"action": "toggle"})},
    {"domain": "binary_sensor", "attributes": {"device_class": "window"}, "state": "on",
     "options": tile_opts(color="blue")},
    {"domain": "binary_sensor", "attributes": {"device_class": "door"}, "state": "on",
     "options": tile_opts(color="blue")},
    {"domain": "binary_sensor", "attributes": {"device_class": "motion"}, "state": "on",
     "options": tile_opts(color="green")},
    {"domain": "cover", "state": "open", "options": tile_opts(color="red")},
    {"domain": "climate", "state": "/^(cool|heat|dry|auto|fan_only|heat_cool)$/",
     "options": tile_opts(color="cyan")},
]


# Tela larga: uma seção de 4 colunas (grade de 48) dividida em 1,5 + 2,5 por dois vertical-stacks.
# 4 colunas de seção só cabem a partir de ~1440 px de conteúdo (+ sidebar); abaixo disso, layout de celular.
LARGO = [{"condition": "screen", "media_query": "(min-width: 1700px)"}]
ESTREITO = [{"condition": "screen", "media_query": "(max-width: 1699px)"}]


def pilha(cards, n):
    return cols({"type": "vertical-stack", "cards": cards}, n)


def topo_inicio():
    return [
        {"type": "horizontal-stack", "cards": [
            {"type": "custom:mushroom-person-card", "entity": p, "icon_type": "entity-picture",
             "layout": "vertical", "fill_container": True} for p in PESSOAS]},
        {"type": "horizontal-stack", "cards": [
            menu("Segurança", "mdi:shield-home", "green", nav("seguranca")),
            menu("Alexa", "mdi:speaker-message", "cyan", nav("alexa")),
            menu("Energia", "mdi:lightning-bolt", "yellow", {"action": "navigate", "navigation_path": "/dashboard-energia"}),
            menu("Rede/PC", "mdi:lan", "purple", nav("rede-pc")),
        ]},
        {"type": "custom:mushroom-chips-card", "alignment": "center", "chips": [
            {"type": "template", "icon": "mdi:lightbulb-group-off", "icon_color": "red", "content": "Apagar tudo",
             "tap_action": {**run("script.apagar_tudo"), "confirmation": {"text": "Apagar todas as luzes?"}}},
            {"type": "template", "icon": "mdi:garage-variant", "content": "Portão",
             "icon_color": "{{ 'red' if is_state('cover.portao_garagem', 'open') else 'green' }}",
             "tap_action": {"action": "toggle"}, "entity": "cover.portao_garagem",
             "hold_action": {"action": "more-info"}},
            {"type": "template", "icon": "mdi:food-drumstick", "icon_color": "orange", "content": "Ração",
             "tap_action": {**run("scene.racao_interno"), "confirmation": {"text": "Soltar ração interna?"}}},
            {"type": "template", "icon": "mdi:robot-vacuum", "icon_color": "green", "content": "Limpar",
             "tap_action": run("scene.limpar_casa")},
        ]},
    ]


def agora_em_casa():
    return [titulo("Agora em casa", "mdi:home-search"), lista_auto(AGORA_FILTROS, "Tudo apagado e fechado")]


def cameras_inicio():
    return [titulo("Câmeras", "mdi:cctv"),
            {"type": "picture-entity", "entity": "camera.smart_camera", "camera_view": "auto",
             "name": "Sala", "show_state": False, "tap_action": nav("seguranca")}]


def piso_cards(piso, nome, icon, script):
    return [titulo(nome, icon, badges=[badge_apagar(script)]),
            *[card_comodo(c) for c in COMODOS if c["piso"] == piso]]


def linha_piso(cabecalho, *comodos):
    return [cabecalho, {"type": "grid", "square": False, "columns": len(comodos), "cards": list(comodos)}]


# ordem no Início escolhida pelo usuário (PISOS segue a ordem da casa nas outras abas)
PISOS_INICIO = [PISOS[2], PISOS[1], PISOS[0]]


def view_inicio():
    # tela larga: esquerda 1,5 (pessoas, atalhos, agora em casa, câmeras) | direita 2,5 (um piso por linha)
    largo = secao(
        pilha(topo_inicio() + agora_em_casa() + cameras_inicio(), 18),
        pilha([c for p in PISOS_INICIO for c in linha_piso(*piso_cards(*p))], 30),
        span=4)
    largo["visibility"] = LARGO
    estreitas = [secao(*[cols(c, 12) for c in topo_inicio()]), secao(*agora_em_casa())]
    estreitas += [secao(*piso_cards(*p)) for p in PISOS_INICIO]
    for s in estreitas:
        s["visibility"] = ESTREITO
    secoes = [largo] + estreitas
    return {
        "title": "Início", "path": "inicio", "icon": "mdi:home", "type": "sections", "max_columns": 4,
        "header": {"card": {"type": "markdown", "text_only": True, "content": SAUDACAO},
                   "layout": "responsive", "badges_position": "bottom", "badges_wrap": "scroll"},
        "badges": [
            contador("sensor.luzes_acesas", "luzes", "amber"),
            contador("sensor.portas_abertas", "portas", "blue"),
            contador("sensor.janelas_abertas", "janelas", "blue"),
            contador("sensor.movimento_ativo", "movimento", "green"),
            contador("sensor.bateria_baixa", "baterias", "red"),
            {"type": "entity", "entity": "weather.openweathermap", "show_name": False, "show_entity_picture": True,
             "state_content": ["temperature", "state"], "tap_action": nav("clima")},
        ],
        "sections": secoes,
    }


# ---------------------------------------------------------------- cômodos


def cabecalho_comodo(c):
    sec = []
    if c["luzes"]:
        sec.append(luzes_do_comodo_on(c) + " de " + str(len(c["luzes"])) + " luzes acesas")
    if c.get("clima"):
        sec.append(f"Ar: {{{{ states('{c['clima']}') }}}}")
    return cols({"type": "custom:mushroom-template-card", "primary": c["nome"],
                 "secondary": " · ".join(sec), "icon": c["icon"],
                 "icon_color": "{{ 'amber' if " + (luzes_do_comodo_on(c)[3:-3] if c["luzes"] else "0") + " > 0 else 'grey' }}",
                 "fill_container": True}, 12)


def view_comodo(c):
    secoes = [secao(voltar(), cabecalho_comodo(c))]
    if c["luzes"]:
        secoes.append(secao(titulo("Luzes", "mdi:lightbulb-group"), *[luz_card(e, n) for e, n in c["luzes"]]))
    extras = []
    if c.get("clima"):
        extras.append(cols({"type": "thermostat", "entity": c["clima"],
                            "features": [{"type": "climate-hvac-modes"}]}, 12))
    if c.get("portao"):
        extras.append(cols({"type": "custom:mushroom-cover-card", "entity": c["portao"], "name": "Portão",
                            "show_buttons_control": True, "fill_container": True}, 12))
    if c.get("alimentador"):
        num, nome, alimentando, ultimo = c["alimentador"]
        extras += [
            cols({"type": "custom:mushroom-template-card", "primary": nome,
                  "secondary": f"Última porção: {{{{ states('{ultimo}') }}}}",
                  "icon": "mdi:food-drumstick",
                  "icon_color": f"{{{{ 'orange' if is_state('{alimentando}', 'on') else 'grey' }}}}",
                  "badge_icon": f"{{{{ 'mdi:loading' if is_state('{alimentando}', 'on') else '' }}}}",
                  "tap_action": {"action": "perform-action", "perform_action": "number.set_value",
                                 "target": {"entity_id": num}, "data": {"value": 1},
                                 "confirmation": {"text": f"Soltar 1 porção no {nome}?"}},
                  "hold_action": {"action": "more-info"}, "fill_container": True}, 12),
        ]
    if c.get("aspirador"):
        extras.append(cols({"type": "custom:mushroom-vacuum-card", "entity": c["aspirador"],
                            "icon_animation": True,
                            "commands": ["start_pause", "stop", "locate", "return_home"],
                            "fill_container": True}, 12))
    if c.get("camera"):
        extras.append(cols({"type": "picture-entity", "entity": c["camera"], "camera_view": "auto",
                            "show_state": False, "name": "Câmera"}, 12))
    if c.get("pc"):
        extras += pc_cards()
    if extras:
        secoes.append(secao(titulo("Aparelhos", "mdi:devices"), *extras))
    secoes.append(secao(
        titulo("Sensores", "mdi:motion-sensor"),
        lista_auto([{"area": c["area"], "domain": "binary_sensor", "options": tile_opts()},
                    {"area": c["area"], "domain": "sensor", "attributes": {"device_class": "temperature"},
                     "options": tile_opts()},
                    {"area": c["area"], "domain": "sensor", "attributes": {"device_class": "humidity"},
                     "options": tile_opts()}],
                   "Nenhum sensor neste cômodo ainda"),
    ))
    midia = ([echo_card(c["echo"], "Alexa")] if c.get("echo") else []) + \
            [echo_card(m) for m in c.get("midia", [])]
    if midia:
        secoes.append(secao(titulo("Som e TV", "mdi:speaker"), *midia))
    return {"title": c["nome"], "path": c["path"], "icon": c["icon"], "type": "sections",
            "max_columns": 4, "subview": True, "back_path": f"{DASH}/inicio", "sections": secoes}


def pc_cards():
    btn = lambda ent, nome, icon: {"type": "template", "icon": icon, "content": nome,
                                   "tap_action": {"action": "perform-action", "perform_action": "button.press",
                                                  "target": {"entity_id": ent}, "confirmation": {"text": f"{nome}?"}}}
    return [
        cols({"type": "custom:mushroom-template-card", "primary": "PC do Ian", "entity": "switch.ian_pc",
              "icon": "mdi:desktop-classic",
              "secondary": "{{ 'Ligado' if states('sensor.ian_pc_ian_pc_ultimaatividade') not in ['unavailable','unknown'] else 'Desligado' }}",
              "icon_color": "{{ 'green' if states('sensor.ian_pc_ian_pc_ultimaatividade') not in ['unavailable','unknown'] else 'grey' }}",
              "tap_action": {"action": "toggle"}, "fill_container": True}, 6),
        cols({"type": "custom:mushroom-entity-card", "entity": "switch.monitores", "name": "Monitores",
              "icon_color": "blue", "tap_action": {"action": "toggle"}, "fill_container": True}, 6),
        cols({"type": "custom:mushroom-chips-card", "alignment": "center", "chips": [
            btn("button.ian_pc_ian_pc_display", "Monitor", "mdi:monitor"),
            btn("button.ian_pc_ian_pc_tv", "TV", "mdi:television"),
            btn("button.ian_pc_ian_pc_surround", "Surround", "mdi:surround-sound"),
            btn("button.ian_pc_ian_pc_sleep", "Suspender", "mdi:power-sleep"),
            btn("button.ian_pc_ian_pc_reiniciar", "Reiniciar", "mdi:restart"),
            btn("button.ian_pc_ian_pc_desligar", "Desligar", "mdi:power"),
        ]}, 12),
    ]


# ---------------------------------------------------------------- abas


def view_luzes():
    secoes = [secao(
        cols({"type": "custom:mushroom-template-card", "primary": "Luzes",
              "secondary": "{{ states('sensor.luzes_acesas') }} acesas",
              "icon": "mdi:lightbulb-group", "icon_color": "amber", "fill_container": True}, 12),
        cols({"type": "custom:mushroom-chips-card", "alignment": "center", "chips": [
            {"type": "template", "icon": "mdi:lightbulb-group-off", "icon_color": "red", "content": "Apagar tudo",
             "tap_action": {**run("script.apagar_tudo"), "confirmation": {"text": "Apagar todas as luzes?"}}}]}, 12),
    )]
    for piso, nome, icon, script in PISOS:
        cards = [titulo(nome, icon, badges=[badge_apagar(script)])]
        for c in COMODOS:
            if c["piso"] == piso:
                cards += [luz_card(e, f"{c['nome']} · {n}" if n != c["nome"] else n) for e, n in c["luzes"]]
        secoes.append(secao(*cards))
    return {"title": "Luzes", "path": "luzes", "icon": "mdi:lightbulb-group", "type": "sections",
            "max_columns": 4, "sections": secoes}


def view_lista(titulo_txt, path, icon, filtros, vazio, cor):
    return {"title": titulo_txt, "path": path, "icon": icon, "type": "sections", "max_columns": 4,
            "subview": True, "back_path": f"{DASH}/inicio", "sections": [secao(
                voltar(),
                cols({"type": "custom:mushroom-template-card", "primary": titulo_txt, "icon": icon,
                      "icon_color": cor, "fill_container": True}, 12),
                {**lista_auto(filtros, vazio, colunas=1, sort="state"), "sort": {"method": "state", "reverse": True}},
            )]}


def view_seguranca():
    return {"title": "Segurança", "path": "seguranca", "icon": "mdi:shield-home", "type": "sections",
            "max_columns": 4, "sections": [
                secao(titulo("Câmeras", "mdi:cctv"),
                      cols({"type": "picture-entity", "entity": "camera.smart_camera", "camera_view": "live",
                            "name": "Sala", "show_state": False}, 12)),
                secao(titulo("Portão", "mdi:garage-variant"),
                      cols({"type": "custom:mushroom-cover-card", "entity": "cover.portao_garagem",
                            "name": "Portão Garagem", "show_buttons_control": True, "fill_container": True}, 12),
                      titulo("Portas e janelas", "mdi:door", sub=True),
                      lista_auto([{"domain": "binary_sensor", "attributes": {"device_class": dc},
                                   "options": tile_opts()} for dc in ("door", "window", "garage_door")],
                                 "Nenhum sensor de porta/janela ainda", colunas=2, sort="state")),
                secao(titulo("Movimento", "mdi:motion-sensor"),
                      lista_auto([{"domain": "binary_sensor", "attributes": {"device_class": dc},
                                   "options": tile_opts()} for dc in ("motion", "occupancy", "presence")],
                                 "Nenhum sensor de movimento ainda", colunas=2, sort="state")),
            ]}


def view_alexa():
    por_piso = []
    for piso, nome, icon, _ in PISOS:
        cards = [echo_card(e, n) for e, n, p in ECHOS if p == piso]
        if cards:
            por_piso.append(secao(titulo(nome, icon), *cards))
    return {"title": "Alexa", "path": "alexa", "icon": "mdi:speaker-message", "type": "sections",
            "max_columns": 4, "sections": [
                secao(titulo("Anunciar", "mdi:bullhorn"),
                      cols({"type": "entities", "entities": [
                          {"entity": "input_text.anuncio_texto", "name": "Mensagem"},
                          {"entity": "input_select.anuncio_destino", "name": "Onde"}]}, 12),
                      cols({"type": "custom:mushroom-template-card", "primary": "Anunciar agora",
                            "icon": "mdi:bullhorn", "icon_color": "cyan",
                            "secondary": "{{ states('input_select.anuncio_destino') }}",
                            "tap_action": run("script.anunciar"), "fill_container": True}, 12)),
                *por_piso,
                secao(titulo("Grupos", "mdi:speaker-multiple"), echo_card("media_player.todo_lugar", "Todo lugar")),
            ]}


def view_baterias():
    return {"title": "Baterias", "path": "baterias", "icon": "mdi:battery", "type": "sections",
            "max_columns": 4, "subview": True, "back_path": f"{DASH}/inicio", "sections": [
                secao(voltar(),
                      cols({"type": "custom:mushroom-template-card", "primary": "Baterias",
                            "secondary": "{{ states('sensor.bateria_baixa') }} com bateria baixa",
                            "icon": "mdi:battery-heart-variant", "icon_color": "red", "fill_container": True}, 12)),
                secao(titulo("Sensores e aparelhos", "mdi:devices"),
                      {"type": "custom:battery-state-card", "sort": ["state"],
                       "filter": {"include": [{"name": "attributes.device_class", "value": "battery"}],
                                  "exclude": [{"name": "entity_id", "value": "sensor.baterias"},
                                              {"name": "entity_id", "value": "*galaxy*"}]},
                       "bulk_rename": [{"from": " Bateria"}, {"from": " Battery level"}]}),
                secao(titulo("Celulares", "mdi:cellphone"),
                      {"type": "custom:battery-state-card", "sort": ["state"],
                       "entities": ["sensor.galaxy_s24_ultra_battery_level"]}),
            ]}


def view_clima():
    return {"title": "Clima", "path": "clima", "icon": "mdi:weather-partly-cloudy", "type": "sections",
            "max_columns": 4, "subview": True, "back_path": f"{DASH}/inicio", "sections": [secao(
                voltar(),
                {"type": "custom:weather-card", "entity": "weather.openweathermap", "current": True,
                 "details": True, "forecast": True, "hourly_forecast": True, "number_of_forecasts": "5"},
                {"type": "custom:weather-radar-card", "data_source": "RainViewer-DarkSky", "map_style": "Dark",
                 "zoom_level": 7, "show_zoom": True, "show_recenter": True, "show_marker": True,
                 "show_scale": True},
            )]}


def view_rede_pc():
    return {"title": "Rede/PC", "path": "rede-pc", "icon": "mdi:lan", "type": "sections", "max_columns": 4,
            "subview": True, "back_path": f"{DASH}/inicio", "sections": [
                secao(voltar(), titulo("Internet", "mdi:web"),
                      cols({"type": "custom:mushroom-entity-card", "entity": "sensor.speedtest_baixar",
                            "name": "Download", "icon": "mdi:download", "icon_color": "indigo"}, 6),
                      cols({"type": "custom:mushroom-entity-card", "entity": "sensor.speedtest_carregar",
                            "name": "Upload", "icon": "mdi:upload", "icon_color": "indigo"}, 6),
                      {"type": "custom:mini-graph-card", "name": "Velocidade (24 h)", "icon": "mdi:speedometer",
                       "hours_to_show": 24, "points_per_hour": 1,
                       "entities": [{"entity": "sensor.speedtest_baixar", "name": "Down"},
                                    {"entity": "sensor.speedtest_carregar", "name": "Up"}]},
                      cols({"type": "custom:mushroom-entity-card", "entity": "sensor.speedtest_ping",
                            "name": "Ping", "icon_color": "indigo"}, 12)),
                secao(titulo("PC", "mdi:desktop-classic"), *pc_cards()),
            ]}


def mushroom5(o):
    """O template card do Mushroom 5 usa `color`/`vertical` (o legado usava icon_color/layout)."""
    if isinstance(o, dict):
        if o.get("type") == "custom:mushroom-template-card":
            if "icon_color" in o:
                o["color"] = o.pop("icon_color")
            if o.pop("layout", None) == "vertical":
                o["vertical"] = True
            o.pop("fill_container", None)
        for v in o.values():
            mushroom5(v)
    elif isinstance(o, list):
        for v in o:
            mushroom5(v)
    return o


def dashboard():
    porta = [{"domain": "binary_sensor", "attributes": {"device_class": dc}, "options": tile_opts()}
             for dc in ("door", "garage_door")] + [{"domain": "cover", "options": tile_opts()}]
    janela = [{"domain": "binary_sensor", "attributes": {"device_class": "window"}, "options": tile_opts()}]
    mov = [{"domain": "binary_sensor", "attributes": {"device_class": dc}, "options": tile_opts()}
           for dc in ("motion", "occupancy", "presence")]
    return {"title": "Casa", "views": [
        view_inicio(), view_luzes(), view_seguranca(), view_alexa(),
        view_lista("Portas", "portas", "mdi:door", porta, "Nenhuma porta com sensor ainda", "blue"),
        view_lista("Janelas", "janelas", "mdi:window-closed-variant", janela, "Nenhuma janela com sensor ainda", "blue"),
        view_lista("Movimento", "movimento", "mdi:motion-sensor", mov, "Nenhum sensor de movimento ainda", "green"),
        view_baterias(), view_clima(), view_rede_pc(),
        *[view_comodo(c) for c in COMODOS],
    ]}


if __name__ == "__main__":
    print(json.dumps(mushroom5(dashboard()), ensure_ascii=False, indent=1))
