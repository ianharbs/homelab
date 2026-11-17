# controle-tv (iHome Player)

Usa o controle XING WEI do Kodi também na TV Sony Bravia (192.168.0.12, reserva DHCP no Omada; se sumir, o serviço procura na rede).
**Um clique no DEL** alterna modo Kodi ↔ modo TV; o Kodi mostra um aviso a cada troca
(o DEL não chega mais ao Kodi, então não mostra/esconde legenda).
No modo Kodi, segurar Voltar/Home (o controle manda Home) também põe a TV na HDMI 4.
No vídeo (tela cheia ou OSD), **PgUp/PgDown = legenda anterior/próxima** (liga a legenda se estiver
desligada); o **microfone duas vezes** faz PgUp/PgDown trocarem o **áudio** até repetir
(o botão manda KEY_SEARCH e KEY_VOICECOMMAND alternadamente; os dois contam). Feito por JSON-RPC
(TCP 9090) com aviso do nome da faixa; fora do vídeo as teclas vão ao Kodi como antes.

**Microfone uma vez**, nos dois modos = assistente de voz (`assistente.py` + `comandos.py`):
grava o microfone do receptor (ALSA `plughw:CARD=Device`) até a fala acabar → **Whisper** local
(faster-whisper small int8, serviço `whisper` da stack media-editing no CT 102, porta 8010, ~1 s) →
**regras** de `comandos.py` (legenda/áudio por idioma, pausa, avançar/voltar, próximo episódio, volume,
mudo, TV liga/desliga, HDMI, abrir app, tocar filme/série com busca aproximada) → senão **Assist do Home
Assistant** (casa) → senão **Gemini** (perguntas; só texto, tenta de novo/modelo reserva, prazo 20 s).
Sem Whisper, o áudio vai direto ao Gemini. Resposta num aviso do Kodi (não aparece se a TV estiver em
outra entrada). Segredos em `/etc/controle-tv/gemini-key` e `/etc/controle-tv/ha-token` (root, 600).
**Microfone duas vezes** (até 0,4 s) alterna PgUp/PgDown entre legenda e áudio no vídeo (a gravação
começa já no 1º clique para não cortar a fala e é descartada se vier o 2º).

- `assistente.py`, `comandos.py` → `/usr/local/bin/` (importados pelo controle-tv.py).
- `bipes.py /usr/local/share/controle-tv` gera `ouvindo.wav` (bipe ↑, toca 0,4 s após o clique) e `erro.wav`
  (↓, não ouviu/entendeu); tocados com `pw-play` no PipeWire do usuário `ihomeplayer` (mistura com o Kodi).
  Os 0,3 s iniciais da gravação são descartados (estalo do mic ao ligar). Últimas 10 falas em `/var/tmp/controle-tv/`.
- `70-controle-tv.rules` → `/etc/udev/rules.d/`: esconde o receptor do Kodi (LIBINPUT_IGNORE_DEVICE).
- `controle-tv.py` → `/usr/local/bin/`: lê o receptor (grab) e repassa ao controle virtual
  "Controle iHome (virtual)" (uinput) no modo Kodi; no modo TV manda IRCC para a TV com PSK.
- `controle-tv.service` → `/etc/systemd/system/` (root, Restart=always).
- PSK da TV em `/etc/controle-tv/psk` (600, fora do git; cópia em /root/freemytv/sony-psk no pve).

Se o serviço parar, o controle fica mudo no Kodi: `sudo systemctl status controle-tv`,
`journalctl -u controle-tv -f` (mostra teclas sem mapa no modo TV).
