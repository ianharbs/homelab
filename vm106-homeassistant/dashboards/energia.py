"""Gera o dashboard "Energia" (url dashboard-energia) do Home Assistant.

Uso no pve:  python3 vm106-homeassistant/dashboards/energia.py > energia.json
O JSON é salvo no HA com `lovelace/config/save` (url_path=dashboard-energia).
O usuário pode editar pela UI: antes de regerar, compare com a config viva.

Escolha do usuário (05/11): igual ao painel de Energia padrão do HA, trocando só
- o seletor de período por custom:energy-period-selector-plus
- o diagrama de distribuição por custom:energy-flow-card-plus (a Edícula entra como
  consumo individual ligado à Casa).
Os cards nativos leem as fontes do painel de Energia (energy prefs): solar
sensor.energia_solar_kw (+ previsão Forecast.Solar leste/oeste), rede comprada
sensor.energia_consumo_kw, devolvida sensor.energia_geracao_kw.

Edícula: quando o medidor existir, preencher EDICULA_ENERGIA (sensor kWh com
state_class total/total_increasing) e adicioná-lo em device_consumption do energy prefs.
"""

import json

EDICULA_ENERGIA = None  # ex.: "sensor.medidor_edicula_energia_total"

seletor = {
    "type": "custom:energy-period-selector-plus",
    "card_background": False,
    "today_button": True,
    "today_button_type": "text",
    "prev_next_buttons": True,
    "compare_button_type": "icon",
    "period_buttons": ["day", "week", "month", "year", "custom"],
    "grid_options": {"columns": "full"},
}

fluxo = {
    "type": "custom:energy-flow-card-plus",
    "title": "Fluxo de energia",
    "energy_date_selection": True,
    "entities": {
        "solar": {"entity": "sensor.energia_solar_kw", "name": "Solar", "display_zero_state": True},
        "grid": {
            "entity": {"consumption": "sensor.energia_consumo_kw", "production": "sensor.energia_geracao_kw"},
            "name": "Rede",
            "display_state": "two_way",
            "color_circle": True,
        },
        "home": {"name": "Casa", "color_icon": "solar"},
    },
    "clickable_entities": True,
    "display_zero_lines": True,
    "use_new_flow_rate_model": True,
    "kwh_decimals": 1,
    "wh_decimals": 0,
    "wh_kwh_threshold": 1000,
    "min_flow_rate": 1,
    "max_flow_rate": 6,
    "min_expected_energy": 10,
    "max_expected_energy": 2000,
    "grid_options": {"columns": "full"},
}
if EDICULA_ENERGIA:
    fluxo["entities"]["individual"] = [{
        "entity": EDICULA_ENERGIA,
        "name": "Edícula",
        "icon": "mdi:home-floor-1",
        "display_zero": True,
    }]

coluna_graficos = [
    {"type": "energy-usage-graph", "grid_options": {"columns": "full"}},
    {"type": "energy-solar-graph", "grid_options": {"columns": "full"}},
]
coluna_lado = [
    fluxo,
    {"type": "energy-grid-neutrality-gauge", "grid_options": {"columns": 12}},
    {"type": "energy-solar-consumed-gauge", "grid_options": {"columns": 6}},
    {"type": "energy-self-sufficiency-gauge", "grid_options": {"columns": 6}},
]
baixo = [{"type": "energy-sources-table", "grid_options": {"columns": "full"}}]
if EDICULA_ENERGIA:
    baixo += [
        {"type": "energy-devices-graph", "grid_options": {"columns": "full"}},
        {"type": "energy-devices-detail-graph", "grid_options": {"columns": "full"}},
    ]

view = {
    "title": "Energia",
    "path": "energia",
    "icon": "mdi:lightning-bolt",
    "type": "sections",
    "max_columns": 3,
    "sections": [
        {"type": "grid", "column_span": 3, "cards": [seletor]},
        {"type": "grid", "column_span": 2, "cards": coluna_graficos},
        {"type": "grid", "column_span": 1, "cards": coluna_lado},
        {"type": "grid", "column_span": 3, "cards": baixo},
    ],
}

config = {"title": "Energia", "views": [view]}

if __name__ == "__main__":
    print(json.dumps(config, ensure_ascii=False, indent=1))
