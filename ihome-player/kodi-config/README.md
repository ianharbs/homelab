# Configuração do Kodi (iHome Player, Arctic Fuse 3) — cópia de referência

Copiado de `~/.kodi/userdata` do iHome Player em 25/11/2026 (a cópia viva é a da caixa; edite lá).

- `skinvariables-nodes/` → `addon_data/script.skinvariables/nodes/skin.arctic.fuse.3/`:
  abas Início (`home`), Filmes (`1101`), Séries (`1102`) e Desenhos (`1103`, ex-Descobrir; nome em
  `Skin.SetString(HomeSwitcher.1103.Name,Desenhos)`). Depois de editar:
  `RunScript(script.skinvariables,action=buildtemplate,force)` e `ReloadSkin()`.
- `playlists/` → `playlists/video/`: listas filtradas pela etiqueta da biblioteca (Séries / Desenhos),
  para os desenhos não invadirem os widgets de séries.
- Widgets próprios: "Filmes/Séries em breve (iHome)" (add-on `plugin.video.ihome.embreve`) e
  "Descobrir e pedir" (Kodiseerr `?mode=popular_movies|popular_tv&widget=1`).
- Biblioteca Desenhos sincronizada pelo add-on do Jellyfin (`?mode=synclib&id=7bb6030393de990bbec35da3efd37a61`).
