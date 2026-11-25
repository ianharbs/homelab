# Tema do Jellyfin (web)

- `custom.css` = Painel › Geral › Branding › CSS personalizado (aplicado via POST /System/Configuration/branding).
  ElegantFin v26.09.05 + ordem da barra superior (layout React do Jellyfin 12).
- Ordem das bibliotecas na barra/"Minha mídia" = `OrderedViews` de cada usuário:
  Filmes, Séries, Desenhos, TV ao Vivo, Coleções. Favoritos/Discover/Requests vão pro fim via CSS `order`.
- `livetv_card.py` gera a capa da "TV ao Vivo" (mosaico de logos dos canais) — o Jellyfin não gera sozinho.
  Rodar no CT 102 com K=<api key> e subir em POST /Items/7e2946dbb8d2a44159af3c69ddb3c1ad/Images/Primary (base64).
- Backups (branding/usuários antes da mudança): /data/data/backup/jellyfin-tema/
