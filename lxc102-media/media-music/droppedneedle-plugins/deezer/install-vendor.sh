#!/bin/sh
# Instala as bibliotecas do plugin em vendor/ (DroppedNeedle v3 não tem pip nem estas libs na imagem).
# Rodar no CT 102 depois de copiar o plugin para /opt/docker/droppedneedle/plugins/deezer:
#   sh /opt/docker/droppedneedle/plugins/deezer/install-vendor.sh
# Usa a mesma base da imagem do DN (Debian bookworm, Python 3.11) para as rodas binárias baterem.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
rm -rf "$DIR/vendor"
docker run --rm -u "$(stat -c %u:%g "$DIR")" -v "$DIR:/plugin" python:3.11-slim-bookworm \
  pip install --no-cache-dir --disable-pip-version-check --target /plugin/vendor \
  "httpx>=0.27,<1" "cryptography>=42" "mutagen>=1.47"
echo "ok: $(ls "$DIR/vendor" | wc -l) pacotes em $DIR/vendor"
