# Tema do Jellyfin (web)

- `custom.css` = Painel › Geral › Branding › CSS personalizado (aplicado via POST /System/Configuration/branding).
  ElegantFin v26.09.05 + ordem da barra superior (layout React do Jellyfin 12).
- Ordem das bibliotecas na barra/"Minha mídia" = `OrderedViews` de cada usuário:
  Filmes, Séries, Desenhos, TV ao Vivo, Coleções. Favoritos/Discover/Requests vão pro fim via CSS `order`.
- `livetv_card.py` gera a capa da "TV ao Vivo" (mosaico de logos dos canais) — o Jellyfin não gera sozinho.
  Rodar no CT 102 com K=<api key> e subir em POST /Items/7e2946dbb8d2a44159af3c69ddb3c1ad/Images/Primary (base64).
- Backups (branding/usuários antes da mudança): /data/data/backup/jellyfin-tema/

## Identidade visual (25/11/2026)
- Ícone principal do iHome (`imagens/icones/…/ihome-01-comunidade-ihome-*`) aplicado pelo **Jellyfin Enhanced**
  (Painel › Plugins › Jellyfin Enhanced › Branding, ou `POST /JellyfinEnhanced/UploadBrandingImage` com `file` +
  `fileName`): `favicon.ico`, `icon-transparent.png` (cabeçalho), `apple-touch-icon.png`, `banner-light.png` /
  `banner-dark.png` (ícone + "iHome" estilizado: "i" ciano com brilho, "Home" prateado, fonte Poppins Bold).
  Vale para jf.ihome.dev.br e video.ihome.dev.br (mesmo servidor). Arquivos em /data/data/backup/jellyfin-tema/branding/.
- Tela de abertura: Jellyfin Enhanced `EnableCustomSplashScreen=true`, imagem = banner-light **com o hash do
  jellyfin-web** (`/web/banner-light.<hash>.png`; o endpoint do plugin pede login). Se o Jellyfin atualizar e a
  abertura ficar sem imagem, ajustar `SplashScreenImageUrl` para o novo nome (`ls /usr/share/jellyfin/web | grep banner-light`).
  Só aparece para quem já está logado, enquanto a página carrega.
- `series-em-breve-uma-por-serie.js`: no plugin **JavaScript Injector** (script "iHome: Séries em breve, uma por
  série"); deixa um card por série na seção "Séries em breve" (o próximo episódio) com "(+N ep.)".
