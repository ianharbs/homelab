# -*- coding: utf-8 -*-
"""Lambda iHomeMusic (us-east-1): repassa os pedidos do skill de música "iHome Music"
para o serviço alexa-busca em casa (CT 103), que tem toda a lógica.

Variáveis de ambiente:
  BASE_URL  https://alexa-busca.ihome.dev.br/music
  SECRET    segredo compartilhado (o mesmo de /opt/docker/alexa-skill/secrets/music_secret.txt)
"""
import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ["BASE_URL"]
SECRET = os.environ["SECRET"]


def lambda_handler(event, context):
    req = urllib.request.Request(
        BASE_URL,
        data=json.dumps(event).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-iHome-Secret": SECRET},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=7) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read()[:500])
        raise
