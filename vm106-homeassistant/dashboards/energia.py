"""Gera o dashboard "Energia" (url dashboard-energia) do Home Assistant.

Uso no pve:  python3 vm106-homeassistant/dashboards/energia.py > energia.json
O JSON é salvo no HA com `lovelace/config/save` (url_path=dashboard-energia).
O usuário pode editar pela UI: antes de regerar, compare com a config viva.

Fontes (medidores Tuya "Medidor Energia Entrada" = rede e "Medidor Energia Solar"):
- potência (W, ~10 s): sensor.solar_casa, sensor.potencia_casa (consumo total = solar + rede),
  sensor.medidor_energia_entrada_potencia (com sinal: + comprando, - devolvendo),
  sensor.consumo_casa (comprando) e sensor.geracao_casa (devolvendo).
- energia acumulada (kWh, integração): sensor.energia_solar_kw, energia_consumo_kw (comprado),
  energia_geracao_kw (devolvido). Medidores diários/mensais: sensor.{solar,rede_comprada,
  rede_devolvida}_{hoje,mes}; derivados consumo_casa_*, saldo_rede_*, autoconsumo_solar_*,
  autossuficiencia_*.
- previsão (Forecast.Solar, um plano leste e um oeste de 3,42 kWp / 15°): soma em
  sensor.previsao_solar_{hoje,restante_hoje,amanha,potencia_agora}.

Edícula: quando o medidor for instalado, preencher EDICULA_POTENCIA / EDICULA_ENERGIA.

Cards: power-flow-card-plus, mushroom, apexcharts-card + cards nativos de energia.
"""

import json

# Quando o medidor da Edícula existir, preencher (ex.: "sensor.edicula_potencia").
EDICULA_POTENCIA = None
EDICULA_ENERGIA = None

AMARELO = "#f5a623"   # solar
VERMELHO = "#e5484d"  # comprado da rede
VERDE = "#30a46c"     # devolvido à rede
AZUL = "#3e8ed0"      # consumo da casa
ROXO = "#8e6bd8"      # previsão

N = lambda e: f"states('{e}') | float(0)"


def kw(e):
    """Texto Jinja para potência em W → 'x W' ou 'x,yz kW'."""
    return (f"{{% set v = {N(e)} %}}{{{{ (v | round(0) | int ~ ' W') if v | abs < 1000 "
            f"else ((v / 1000) | round(2) | string | replace('.', ',') ~ ' kW') }}}}")


def kwh(e):
    return f"{{{{ ({N(e)}) | round(1) | string | replace('.', ',') }}}} kWh"


def tpl(primary, secondary, icon, color, entity=None, cols=6, **kw_):
    c = {
        "type": "custom:mushroom-template-card",
        "primary": primary,
        "secondary": secondary,
        "icon": icon,
        "color": color,
        "grid_options": {"columns": cols},
    }
    if entity:
        c["entity"] = entity
        c["tap_action"] = {"action": "more-info"}
    c.update(kw_)
    return c


def heading(t, icon, badges=None):
    h = {"type": "heading", "heading": t, "heading_style": "title", "icon": icon}
    if badges:
        h["badges"] = badges
    return h


def gauge(entity, name):
    return {
        "type": "gauge",
        "entity": entity,
        "name": name,
        "min": 0,
        "max": 100,
        "needle": True,
        "severity": {"red": 0, "yellow": 35, "green": 65},
        "grid_options": {"columns": 6},
    }


# ---------------------------------------------------------------- Agora

fluxo = {
    "type": "custom:power-flow-card-plus",
    "entities": {
        "solar": {
            "entity": "sensor.solar_casa",
            "name": "Solar",
            "display_zero_state": True,
            "secondary_info": {"entity": "sensor.previsao_solar_potencia_agora",
                               "unit_of_measurement": "W prev.", "decimals": 0},
        },
        "grid": {
            "entity": "sensor.medidor_energia_entrada_potencia",
            "name": "Rede",
            "display_state": "one_way",
            "color_circle": True,
            "color": {"consumption": VERMELHO, "production": VERDE},
        },
        "home": {
            "entity": "sensor.potencia_casa",
            "name": "Casa",
            "color_icon": "solar",
        },
    },
    "watt_threshold": 1000,
    "kw_decimals": 2,
    "w_decimals": 0,
    "min_flow_rate": 0.75,
    "max_flow_rate": 6,
    "clickable_entities": True,
    "use_new_flow_rate_model": True,
    "grid_options": {"columns": 12},
}
if EDICULA_POTENCIA:
    fluxo["entities"]["individual"] = [
        {"entity": EDICULA_POTENCIA, "name": "Edícula", "icon": "mdi:home-floor-1", "color": AZUL}
    ]

rede_agora = tpl(
    "{% set v = " + N("sensor.medidor_energia_entrada_potencia") + " %}"
    "{{ 'Comprando da rede' if v > 5 else ('Devolvendo à rede' if v < -5 else 'Rede equilibrada') }}",
    "{% set v = " + N("sensor.medidor_energia_entrada_potencia") + " | abs %}"
    "{{ (v | round(0) | int ~ ' W') if v < 1000 else ((v / 1000) | round(2) | string | replace('.', ',') ~ ' kW') }}",
    "{% set v = " + N("sensor.medidor_energia_entrada_potencia") + " %}"
    "{{ 'mdi:transmission-tower-import' if v > 5 else ('mdi:transmission-tower-export' if v < -5 else 'mdi:transmission-tower') }}",
    "{% set v = " + N("sensor.medidor_energia_entrada_potencia") + " %}"
    "{{ 'red' if v > 5 else ('green' if v < -5 else 'grey') }}",
    entity="sensor.medidor_energia_entrada_potencia",
)
solar_agora = tpl(
    "Solar agora",
    kw("sensor.solar_casa") + " · previsto " + kw("sensor.previsao_solar_potencia_agora"),
    "mdi:solar-power-variant",
    "amber",
    entity="sensor.solar_casa",
)

secao_agora = {
    "type": "grid",
    "cards": [heading("Agora", "mdi:flash"), fluxo, solar_agora, rede_agora],
}

# ---------------------------------------------------------------- Hoje

secao_hoje = {
    "type": "grid",
    "cards": [
        heading("Hoje", "mdi:calendar-today"),
        tpl("Gerado", kwh("sensor.solar_hoje") + " · {{ states('sensor.previsao_solar_acerto_hoje') }}% do previsto",
            "mdi:solar-power", "amber", entity="sensor.solar_hoje", cols=12),
        tpl("Comprado", kwh("sensor.rede_comprada_hoje"), "mdi:transmission-tower-import", "red",
            entity="sensor.rede_comprada_hoje"),
        tpl("Devolvido", kwh("sensor.rede_devolvida_hoje"), "mdi:transmission-tower-export", "green",
            entity="sensor.rede_devolvida_hoje"),
        tpl("Consumo da casa", kwh("sensor.consumo_casa_hoje"), "mdi:home-lightning-bolt-outline", "blue",
            entity="sensor.consumo_casa_hoje"),
        tpl("Saldo com a rede",
            "{% set v = " + N("sensor.saldo_rede_hoje") + " %}{{ '+' if v > 0 else '' }}"
            "{{ v | round(1) | string | replace('.', ',') }} kWh",
            "mdi:scale-balance",
            "{{ 'green' if " + N("sensor.saldo_rede_hoje") + " >= 0 else 'red' }}",
            entity="sensor.saldo_rede_hoje"),
        gauge("sensor.autossuficiencia_hoje", "Autossuficiência"),
        gauge("sensor.autoconsumo_solar_hoje", "Solar usado em casa"),
    ],
}

# ---------------------------------------------------------------- Previsão

secao_previsao = {
    "type": "grid",
    "cards": [
        heading("Previsão solar", "mdi:weather-partly-cloudy"),
        tpl("Restante hoje", kwh("sensor.previsao_solar_restante_hoje") + " · total previsto "
            + kwh("sensor.previsao_solar_hoje"),
            "mdi:solar-power-variant-outline", "amber", entity="sensor.previsao_solar_hoje", cols=12),
        tpl("Amanhã", kwh("sensor.previsao_solar_amanha")
            + "{% set a = as_timestamp(states('sensor.previsao_solar_leste_pico_amanha'), 0) %}"
            "{% set b = as_timestamp(states('sensor.previsao_solar_oeste_pico_amanha'), 0) %}"
            "{% if a and b %} · pico por volta das {{ ((a + b) / 2) | timestamp_custom('%H:%M') }}{% endif %}",
            "mdi:weather-sunny-alert",
            "{% set v = " + N("sensor.previsao_solar_amanha") + " %}"
            "{{ 'amber' if v >= 25 else ('orange' if v >= 12 else 'blue-grey') }}",
            entity="sensor.previsao_solar_amanha", cols=12),
        {"type": "energy-solar-graph", "title": "Hoje: gerado × previsto", "grid_options": {"columns": 12}},
    ],
}

# ---------------------------------------------------------------- gráficos

def serie(entity, name, color, kind="area", invert=False, opacity=0.25):
    s = {
        "entity": entity,
        "name": name,
        "color": color,
        "type": kind,
        "stroke_width": 2,
        "opacity": opacity,
        "group_by": {"func": "avg", "duration": "5min"},
        "extend_to": "now",
    }
    if invert:
        s["transform"] = "return -x;"
    return s


grafico_24h = {
    "type": "custom:apexcharts-card",
    "header": {"show": True, "title": "Últimas 24 horas", "show_states": True, "colorize_states": True},
    "graph_span": "24h",
    "now": {"show": True, "label": "agora"},
    "yaxis": [{"min": "~0", "decimals": 0, "apex_config": {"tickAmount": 5}}],
    "apex_config": {
        "chart": {"height": 300},
        "legend": {"show": True, "position": "bottom"},
        "stroke": {"curve": "smooth"},
        "tooltip": {"x": {"format": "HH:mm"}},
    },
    "series": [
        serie("sensor.solar_casa", "Solar", AMARELO),
        serie("sensor.potencia_casa", "Consumo da casa", AZUL, kind="line", opacity=1),
        serie("sensor.consumo_casa", "Comprado", VERMELHO, opacity=0.35),
        serie("sensor.geracao_casa", "Devolvido", VERDE, invert=True, opacity=0.35),
        dict(serie("sensor.previsao_solar_potencia_agora", "Previsão solar", ROXO, kind="line", opacity=0.8),
             stroke_dash=4, show={"in_header": False}, group_by={"func": "last", "duration": "15min"}),
    ],
    "grid_options": {"columns": 12},
}


def barras_dia(entity, name, color):
    return {
        "entity": entity,
        "name": name,
        "color": color,
        "type": "column",
        "statistics": {"type": "change", "period": "day", "align": "start"},
        "group_by": {"func": "last", "duration": "1d"},
    }


grafico_30d = {
    "type": "custom:apexcharts-card",
    "header": {"show": True, "title": "Por dia (30 dias)", "show_states": False},
    "graph_span": "30d",
    "span": {"end": "day"},
    "yaxis": [{"min": 0, "decimals": 0}],
    "apex_config": {
        "chart": {"height": 280},
        "legend": {"show": True, "position": "bottom"},
        "plotOptions": {"bar": {"columnWidth": "70%", "borderRadius": 2}},
        "dataLabels": {"enabled": False},
        "tooltip": {"x": {"format": "dd/MM"}},
        "xaxis": {"labels": {"format": "dd/MM"}},
    },
    "series": [
        barras_dia("sensor.energia_solar_kw", "Gerado", AMARELO),
        barras_dia("sensor.energia_consumo_kw", "Comprado", VERMELHO),
        barras_dia("sensor.energia_geracao_kw", "Devolvido", VERDE),
    ],
    "grid_options": {"columns": 12},
}

secao_graficos = {
    "type": "grid",
    "column_span": 3,
    "cards": [heading("Gráficos", "mdi:chart-areaspline"), grafico_24h, grafico_30d],
}

# ---------------------------------------------------------------- Mês

secao_mes = {
    "type": "grid",
    "cards": [
        heading("Este mês", "mdi:calendar-month"),
        tpl("Gerado", kwh("sensor.solar_mes"), "mdi:solar-power", "amber", entity="sensor.solar_mes"),
        tpl("Consumo da casa", kwh("sensor.consumo_casa_mes"), "mdi:home-lightning-bolt-outline", "blue",
            entity="sensor.consumo_casa_mes"),
        tpl("Comprado", kwh("sensor.rede_comprada_mes"), "mdi:transmission-tower-import", "red",
            entity="sensor.rede_comprada_mes"),
        tpl("Devolvido", kwh("sensor.rede_devolvida_mes"), "mdi:transmission-tower-export", "green",
            entity="sensor.rede_devolvida_mes"),
        tpl("Saldo com a rede",
            "{% set v = " + N("sensor.saldo_rede_mes") + " %}{{ '+' if v > 0 else '' }}"
            "{{ v | round(1) | string | replace('.', ',') }} kWh",
            "mdi:scale-balance",
            "{{ 'green' if " + N("sensor.saldo_rede_mes") + " >= 0 else 'red' }}",
            entity="sensor.saldo_rede_mes", cols=12),
        gauge("sensor.autossuficiencia_mes", "Autossuficiência"),
        gauge("sensor.autoconsumo_solar_mes", "Solar usado em casa"),
    ],
}

# ---------------------------------------------------------------- Edícula

if EDICULA_POTENCIA:
    edicula_cards = [
        tpl("Edícula agora", kw(EDICULA_POTENCIA), "mdi:home-floor-1", "blue", entity=EDICULA_POTENCIA, cols=12),
    ]
    if EDICULA_ENERGIA:
        edicula_cards.append({
            "type": "statistic", "entity": EDICULA_ENERGIA, "name": "Edícula hoje",
            "period": {"calendar": {"period": "day"}}, "stat_type": "change",
            "grid_options": {"columns": 6},
        })
        edicula_cards.append({
            "type": "statistic", "entity": EDICULA_ENERGIA, "name": "Edícula no mês",
            "period": {"calendar": {"period": "month"}}, "stat_type": "change",
            "grid_options": {"columns": 6},
        })
else:
    edicula_cards = [
        tpl("Medidor da Edícula", "Aguardando instalação — aparece aqui (e no fluxo) quando for ligado",
            "mdi:home-floor-1", "grey", cols=12, multiline_secondary=True),
    ]

secao_edicula = {"type": "grid", "cards": [heading("Edícula", "mdi:home-floor-1")] + edicula_cards}

# ---------------------------------------------------------------- views

view_agora = {
    "title": "Energia",
    "path": "agora",
    "icon": "mdi:lightning-bolt",
    "type": "sections",
    "max_columns": 3,
    "sections": [secao_agora, secao_hoje, secao_previsao, secao_graficos, secao_mes, secao_edicula],
}

view_historico = {
    "title": "Histórico",
    "path": "historico",
    "icon": "mdi:chart-bar",
    "type": "sections",
    "max_columns": 3,
    "header": {"card": {"type": "markdown", "text_only": True,
                        "content": "## Histórico de energia\nEscolha o período abaixo (dia, semana, mês, ano)."}},
    "sections": [
        {"type": "grid", "column_span": 3, "cards": [
            {"type": "energy-date-selection", "grid_options": {"columns": 12}},
            {"type": "energy-usage-graph", "title": "Consumo e rede", "grid_options": {"columns": 12}},
            {"type": "energy-solar-graph", "title": "Geração solar (com previsão)", "grid_options": {"columns": 12}},
        ]},
        {"type": "grid", "cards": [
            {"type": "energy-distribution", "title": "Fluxo no período", "link_dashboard": False},
        ]},
        {"type": "grid", "cards": [
            {"type": "energy-solar-consumed-gauge", "grid_options": {"columns": 6}},
            {"type": "energy-self-sufficiency-gauge", "grid_options": {"columns": 6}},
            {"type": "energy-grid-neutrality-gauge", "grid_options": {"columns": 12}},
        ]},
        {"type": "grid", "cards": [
            {"type": "energy-sources-table", "title": "Fontes"},
        ] + ([{"type": "energy-devices-graph", "title": "Edícula"}] if EDICULA_ENERGIA else [])},
    ],
}

config = {"title": "Energia", "views": [view_agora, view_historico]}

if __name__ == "__main__":
    print(json.dumps(config, ensure_ascii=False, indent=1))
