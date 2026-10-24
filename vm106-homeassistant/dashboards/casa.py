"""Gera o dashboard "Casa" (url dashboard-casa) do Home Assistant.

Uso no pve:  python3 vm106-homeassistant/dashboards/casa.py > casa.json
O JSON é salvo no HA com `lovelace/config/save` (url_path=dashboard-casa).
Pensado para celular: seções de 2 tiles por linha, toque liga/desliga, toque longo abre detalhes.
"""

import json


def luz(entity, nome):
    return {
        "type": "tile",
        "entity": entity,
        "name": nome,
        "color": "amber",
        "tap_action": {"action": "toggle"},
        "icon_tap_action": {"action": "toggle"},
        "hold_action": {"action": "more-info"},
        "grid_options": {"columns": 6},
    }


def botao(entity, nome, icon=None, color="blue"):
    """Tile que executa o script/cena ao tocar."""
    card = {
        "type": "tile",
        "entity": entity,
        "name": nome,
        "color": color,
        "show_entity_picture": False,
        "hide_state": True,
        "tap_action": {"action": "perform-action", "perform_action": f"{entity.split('.')[0]}.turn_on",
                       "target": {"entity_id": entity}},
        "icon_tap_action": {"action": "perform-action", "perform_action": f"{entity.split('.')[0]}.turn_on",
                            "target": {"entity_id": entity}},
        "grid_options": {"columns": 6},
    }
    if icon:
        card["icon"] = icon
    return card


def tile(entity, nome=None, cols=6, **extra):
    card = {"type": "tile", "entity": entity, "grid_options": {"columns": cols}}
    if nome:
        card["name"] = nome
    card.update(extra)
    return card


def titulo(texto, icon, apagar=None, sub=False):
    card = {"type": "heading", "heading": texto, "icon": icon,
            "heading_style": "subtitle" if sub else "title"}
    if apagar:
        card["badges"] = [{
            "type": "entity", "entity": apagar, "show_state": False, "show_icon": True,
            "tap_action": {"action": "perform-action", "perform_action": "script.turn_on",
                           "target": {"entity_id": apagar}},
        }]
    return card


def secao(*cards):
    return {"type": "grid", "cards": list(cards)}


portao = tile("cover.portao_garagem", "Portão", 12,
              features=[{"type": "cover-open-close"}], features_position="inline")

inicio = {
    "title": "Casa",
    "path": "casa",
    "icon": "mdi:home",
    "type": "sections",
    "max_columns": 4,
    "badges": [
        {"type": "entity", "entity": "sensor.luzes_acesas", "color": "amber",
         "tap_action": {"action": "more-info"}, "show_name": False},
        {"type": "entity", "entity": "sensor.janelas_abertas", "show_name": False},
        {"type": "entity", "entity": "sensor.bateria_baixa", "color": "red", "show_name": False,
         "visibility": [{"condition": "state", "entity": "sensor.bateria_baixa", "state_not": "0"}]},
        {"type": "entity", "entity": "weather.openweathermap", "show_name": False,
         "show_entity_picture": True, "state_content": ["temperature", "state"]},
    ],
    "sections": [
        secao(
            titulo("Atalhos", "mdi:gesture-tap"),
            botao("script.apagar_tudo", "Apagar tudo", color="red"),
            botao("scene.racao_interno", "Ração interna", "mdi:food-drumstick", "orange"),
            botao("scene.limpar_casa", "Limpar casa", "mdi:robot-vacuum", "green"),
            tile("person.ian_augusto", "Ian"),
            portao,
        ),
        secao(
            titulo("Casa Pais", "mdi:home", "script.apagar_casa_pais"),
            luz("light.sala", "Sala"),
            luz("light.cozinha", "Cozinha"),
            luz("light.quarto_pais", "Quarto Pais"),
            luz("light.banheiro_casa", "Banheiro"),
            luz("light.quarto_visitas", "Quarto Visitas"),
            luz("light.cabeceira", "Cabeceira"),
            titulo("Sensores", "mdi:motion-sensor", sub=True),
            tile("binary_sensor.janela_sala_lateral", "Janela Sala"),
            tile("binary_sensor.presenca_sala", "Movimento Sala"),
        ),
        secao(
            titulo("Área Externa", "mdi:tree", "script.apagar_area_externa"),
            luz("light.frente", "Frente"),
            luz("light.garagem", "Garagem"),
            luz("light.churrasqueira_esquerda", "Churrasq. Esq."),
            luz("light.churrasqueira_direita", "Churrasq. Dir."),
            portao,
        ),
        secao(
            titulo("Edícula", "mdi:home-floor-1", "script.apagar_edicula"),
            luz("light.escritorio", "Escritório"),
            luz("light.suite", "Suíte"),
            luz("light.banheiro_edicula", "Banheiro"),
            luz("light.espelho", "Espelho"),
            luz("light.externo_edicula", "Externo"),
            luz("light.luminaria", "Luminária"),
            tile("climate.ar_suite", "Ar Suíte", 12,
                 features=[{"type": "target-temperature"}], features_position="inline"),
            titulo("Escritório", "mdi:desk", sub=True),
            tile("switch.monitores", "Monitores", color="blue", tap_action={"action": "toggle"}),
            tile("switch.ian_pc", "PC", color="blue", tap_action={"action": "toggle"}),
        ),
        secao(
            titulo("Câmera", "mdi:cctv"),
            {"type": "picture-entity", "entity": "camera.smart_camera", "name": "Sala",
             "camera_view": "auto", "show_state": False, "grid_options": {"columns": 12}},
        ),
    ],
}

echos = [
    ("media_player.echo_show_sala", "Sala"),
    ("media_player.echo_dot_quarto_pais", "Quarto Pais"),
    ("media_player.echo_pop_quarto_visitas", "Quarto Visitas"),
    ("media_player.echo_dot_banheiro_casa", "Banheiro Casa"),
    ("media_player.echo_dot_churrasqueira", "Churrasqueira"),
    ("media_player.echo_show_escritorio", "Escritório"),
    ("media_player.echo_spot_suite", "Suíte"),
    ("media_player.echo_dot_banheiro_edicula", "Banheiro Edícula"),
]

alexa = {
    "title": "Alexa",
    "path": "alexa",
    "icon": "mdi:speaker",
    "type": "sections",
    "max_columns": 4,
    "sections": [
        secao(
            titulo("Echos", "mdi:speaker"),
            *[tile(e, n, 12, features=[{"type": "media-player-volume-slider"}],
                   features_position="bottom") for e, n in echos],
        ),
        secao(
            titulo("Grupos e TVs", "mdi:television"),
            tile("media_player.todo_lugar", "Todo lugar", 12),
            tile("media_player.firetv_suite", "FireTV Suíte"),
            tile("media_player.firetv_churrasqueira", "FireTV Churrasq."),
        ),
    ],
}

sistema = {
    "title": "Sistema",
    "path": "sistema",
    "icon": "mdi:cog",
    "type": "sections",
    "max_columns": 4,
    "sections": [
        secao(
            titulo("Energia", "mdi:lightning-bolt",),
            tile("sensor.energia_solar_kw", "Solar"),
            tile("sensor.energia_consumo_kw", "Consumo"),
            tile("sensor.energia_geracao_kw", "Geração", 12,
                 tap_action={"action": "navigate", "navigation_path": "/dashboard-energia"}),
        ),
        secao(
            titulo("Internet", "mdi:web"),
            tile("sensor.speedtest_baixar", "Download"),
            tile("sensor.speedtest_carregar", "Upload"),
            tile("sensor.speedtest_ping", "Ping"),
        ),
        secao(
            titulo("Atualizações", "mdi:update"),
            {"type": "entity-filter", "entities": [],
             "conditions": [{"condition": "state", "state": "on"}],
             "card": {"type": "entities", "title": None},
             "show_empty": False},
        ),
    ],
}

if __name__ == "__main__":
    print(json.dumps({"title": "Casa", "views": [inicio, alexa, sistema]}, ensure_ascii=False, indent=1))
