# controle-tv (iHome Player)

Usa o controle XING WEI do Kodi também na TV Sony Bravia (192.168.0.12, reserva DHCP no Omada; se sumir, o serviço procura na rede).
**Um clique no DEL** alterna modo Kodi ↔ modo TV; o Kodi mostra um aviso a cada troca
(o DEL não chega mais ao Kodi, então não mostra/esconde legenda).
No modo Kodi, segurar Voltar/Home (o controle manda Home) também põe a TV na HDMI 4.
No vídeo (tela cheia ou OSD), **PgUp/PgDown = legenda anterior/próxima** (liga a legenda se estiver
desligada); o **botão do microfone** faz PgUp/PgDown trocarem o **áudio** até ser apertado de novo
(o botão manda KEY_SEARCH e KEY_VOICECOMMAND alternadamente; os dois contam). Feito por JSON-RPC
(TCP 9090) com aviso do nome da faixa; fora do vídeo as teclas vão ao Kodi como antes.

- `70-controle-tv.rules` → `/etc/udev/rules.d/`: esconde o receptor do Kodi (LIBINPUT_IGNORE_DEVICE).
- `controle-tv.py` → `/usr/local/bin/`: lê o receptor (grab) e repassa ao controle virtual
  "Controle iHome (virtual)" (uinput) no modo Kodi; no modo TV manda IRCC para a TV com PSK.
- `controle-tv.service` → `/etc/systemd/system/` (root, Restart=always).
- PSK da TV em `/etc/controle-tv/psk` (600, fora do git; cópia em /root/freemytv/sony-psk no pve).

Se o serviço parar, o controle fica mudo no Kodi: `sudo systemctl status controle-tv`,
`journalctl -u controle-tv -f` (mostra teclas sem mapa no modo TV).
