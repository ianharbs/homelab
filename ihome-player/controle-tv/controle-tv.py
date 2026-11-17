#!/usr/bin/python3
"""controle-tv: usa o controle XING WEI do iHome Player (Kodi) também na TV Sony.

Um clique no DEL alterna:
  - modo Kodi: as teclas vão para um controle virtual (uinput) que o Kodi lê;
  - modo TV:   as teclas viram comandos IRCC enviados à Bravia pela rede (PSK).

O receptor original fica escondido do Kodi pela regra udev 70-controle-tv.rules
(LIBINPUT_IGNORE_DEVICE), por isso este serviço precisa estar sempre rodando.
"""
import concurrent.futures
import json
import os
import select
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import evdev
from evdev import ecodes as e

NOME_RECEPTOR = "XING WEI 2.4G USB USB Composite Device"
TV_IP = os.environ.get("TV_IP", "192.168.0.12")  # reserva DHCP no Omada; se mudar, TV.descobre() acha
TV_MODELO = "XBR-55X905E"
REDE = "192.168.0."
PSK = open(os.environ.get("TV_PSK_FILE", "/etc/controle-tv/psk")).read().strip()
KODI_RPC = ("127.0.0.1", 9090)
TROCA = e.KEY_BACKSPACE  # botão DEL: um clique alterna Kodi <-> TV (não vai mais ao Kodi)
HDMI_KODI = 4  # segurar Home no modo Kodi põe a TV nesta entrada
# No vídeo (modo Kodi): PgUp/PgDown = legenda anterior/próxima; o botão do microfone
# (manda KEY_SEARCH e KEY_VOICECOMMAND alternadamente) faz PgUp/PgDown trocarem o áudio, até apertar de novo.
FAIXA = {e.KEY_PAGEUP: "previous", e.KEY_PAGEDOWN: "next"}
MICROFONE = {e.KEY_SEARCH, e.KEY_VOICECOMMAND}

# Códigos IRCC padrão das Bravia (atualizados pelo getRemoteControllerInfo da TV)
IRCC = {
    "TvPower": "AAAAAQAAAAEAAAAVAw==", "Input": "AAAAAQAAAAEAAAAlAw==",
    "Home": "AAAAAQAAAAEAAABgAw==", "Return": "AAAAAgAAAJcAAAAjAw==",
    "Up": "AAAAAQAAAAEAAAB0Aw==", "Down": "AAAAAQAAAAEAAAB1Aw==",
    "Left": "AAAAAQAAAAEAAAA0Aw==", "Right": "AAAAAQAAAAEAAAAzAw==",
    "Confirm": "AAAAAQAAAAEAAABlAw==", "Options": "AAAAAgAAAJcAAAA2Aw==",
    "ActionMenu": "AAAAAgAAAMQAAABLAw==",
    "VolumeUp": "AAAAAQAAAAEAAAASAw==", "VolumeDown": "AAAAAQAAAAEAAAATAw==",
    "Mute": "AAAAAQAAAAEAAAAUAw==",
    "ChannelUp": "AAAAAQAAAAEAAAAQAw==", "ChannelDown": "AAAAAQAAAAEAAAARAw==",
    "Play": "AAAAAgAAAJcAAAAaAw==", "Pause": "AAAAAgAAAJcAAAAZAw==",
    "Stop": "AAAAAgAAAJcAAAAYAw==", "Next": "AAAAAgAAAJcAAAA9Aw==",
    "Prev": "AAAAAgAAAJcAAAA8Aw==", "Forward": "AAAAAgAAAJcAAAAcAw==",
    "Rewind": "AAAAAgAAAJcAAAAbAw==",
    "Num1": "AAAAAQAAAAEAAAAAAw==", "Num2": "AAAAAQAAAAEAAAABAw==",
    "Num3": "AAAAAQAAAAEAAAACAw==", "Num4": "AAAAAQAAAAEAAAADAw==",
    "Num5": "AAAAAQAAAAEAAAAEAw==", "Num6": "AAAAAQAAAAEAAAAFAw==",
    "Num7": "AAAAAQAAAAEAAAAGAw==", "Num8": "AAAAAQAAAAEAAAAHAw==",
    "Num9": "AAAAAQAAAAEAAAAIAw==", "Num0": "AAAAAQAAAAEAAAAJAw==",
}

# tecla do controle -> comando da TV ("PlayPause" alterna Play/Pause)
MAPA = {
    e.KEY_UP: "Up", e.KEY_DOWN: "Down", e.KEY_LEFT: "Left", e.KEY_RIGHT: "Right",
    e.KEY_ENTER: "Confirm", e.KEY_KPENTER: "Confirm", e.KEY_SELECT: "Confirm", e.KEY_OK: "Confirm",
    e.KEY_BACK: "Return", e.KEY_ESC: "Return",
    e.KEY_HOMEPAGE: "Home", e.KEY_HOME: "Home",
    e.KEY_MENU: "ActionMenu", e.KEY_COMPOSE: "ActionMenu", e.KEY_CONTEXT_MENU: "ActionMenu",
    e.KEY_VOLUMEUP: "VolumeUp", e.KEY_VOLUMEDOWN: "VolumeDown", e.KEY_MUTE: "Mute",
    e.KEY_PAGEUP: "ChannelUp", e.KEY_PAGEDOWN: "ChannelDown",
    e.KEY_CHANNELUP: "ChannelUp", e.KEY_CHANNELDOWN: "ChannelDown",
    e.KEY_PLAYPAUSE: "PlayPause", e.KEY_PLAY: "Play", e.KEY_PAUSE: "Pause",
    e.KEY_STOPCD: "Stop", e.KEY_STOP: "Stop",
    e.KEY_NEXTSONG: "Next", e.KEY_PREVIOUSSONG: "Prev",
    e.KEY_FASTFORWARD: "Forward", e.KEY_REWIND: "Rewind",
    e.KEY_POWER: "TvPower", e.KEY_SLEEP: "TvPower",
    **{getattr(e, f"KEY_{n}"): f"Num{n}" for n in range(10)},
    **{getattr(e, f"KEY_KP{n}"): f"Num{n}" for n in range(10)},
}
REPETE = {"Up", "Down", "Left", "Right", "VolumeUp", "VolumeDown"}


def log(*a):
    print(*a, flush=True)


def kodi_aviso(titulo, msg, tempo=3000):
    try:
        req = {"jsonrpc": "2.0", "id": 1, "method": "GUI.ShowNotification",
               "params": {"title": titulo, "message": msg, "displaytime": tempo}}
        with socket.create_connection(KODI_RPC, timeout=2) as s:
            s.sendall(json.dumps(req).encode())
    except OSError as ex:
        log("aviso Kodi falhou:", ex)


def kodi_rpc(metodo, params=None):
    """Chamada JSON-RPC (TCP 9090) esperando a resposta; ignora notificações que o Kodi mande no meio."""
    req = {"jsonrpc": "2.0", "id": 7, "method": metodo, "params": params or {}}
    with socket.create_connection(KODI_RPC, timeout=1) as s:
        s.sendall(json.dumps(req).encode())
        buf, dec = "", json.JSONDecoder()
        while True:
            pedaco = s.recv(65536)
            if not pedaco:
                raise OSError("Kodi fechou a conexão")
            buf += pedaco.decode(errors="replace")
            while buf.strip():
                buf = buf.lstrip()
                try:
                    obj, fim = dec.raw_decode(buf)
                except ValueError:
                    break  # JSON incompleto: lê mais
                buf = buf[fim:]
                if obj.get("id") == 7:
                    if "error" in obj:
                        raise RuntimeError(obj["error"])
                    return obj.get("result")


def kodi_no_video():
    """playerid do vídeo se o Kodi está na tela do vídeo (cheia ou OSD), senão None."""
    r = kodi_rpc("XBMC.GetInfoBooleans", {"booleans": [
        "Window.IsActive(fullscreenvideo)", "Window.IsActive(videoosd)"]})
    if not any(r.values()):
        return None
    for p in kodi_rpc("Player.GetActivePlayers"):
        if p.get("type") == "video":
            return p["playerid"]
    return None


def _nome_faixa(f):
    if not f:
        return "nenhuma"
    nome = f.get("name") or ""
    lingua = f.get("language") or ""
    return " · ".join(x for x in (lingua, nome) if x) or f"faixa {f.get('index', '?')}"


def kodi_troca_faixa(pid, audio, direcao):
    """direcao = 'next' ou 'previous'; troca a legenda (ligando-a) ou o áudio e avisa qual ficou."""
    if audio:
        kodi_rpc("Player.SetAudioStream", {"playerid": pid, "stream": direcao})
        f = kodi_rpc("Player.GetProperties", {"playerid": pid, "properties": ["currentaudiostream"]})
        kodi_aviso("Áudio", _nome_faixa(f.get("currentaudiostream")), 1500)
    else:
        kodi_rpc("Player.SetSubtitle", {"playerid": pid, "subtitle": direcao, "enable": True})
        f = kodi_rpc("Player.GetProperties", {"playerid": pid, "properties": ["currentsubtitle"]})
        kodi_aviso("Legenda", _nome_faixa(f.get("currentsubtitle")), 1500)


class TV:
    def __init__(self):
        self.fila = []
        self.cv = threading.Condition()
        self.tocando = True
        self.codigos_ok = False
        self.ip = TV_IP
        threading.Thread(target=self._loop, daemon=True).start()

    def _post1(self, ip, path, body, headers, timeout):
        req = urllib.request.Request(f"http://{ip}{path}", data=body.encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def _post(self, path, body, headers, timeout=3):
        try:
            return self._post1(self.ip, path, body, headers, timeout)
        except (urllib.error.URLError, OSError):
            if not self.descobre():
                raise
            return self._post1(self.ip, path, body, headers, timeout)

    def descobre(self):
        """A TV sumiu do IP conhecido: procura a Bravia na rede (DHCP pode ter mudado)."""
        corpo = json.dumps({"method": "getInterfaceInformation", "id": 1,
                            "params": [], "version": "1.0"})

        def testa(i):
            ip = f"{REDE}{i}"
            try:
                r = self._post1(ip, "/sony/system", corpo, {"Content-Type": "application/json"}, 1.5)
                return ip if json.loads(r)["result"][0].get("modelName") == TV_MODELO else None
            except Exception:
                return None

        with concurrent.futures.ThreadPoolExecutor(64) as ex:
            for ip in ex.map(testa, range(1, 255)):
                if ip:
                    if ip != self.ip:
                        log("TV achada em", ip, "(antes", self.ip + ")")
                    self.ip = ip
                    return True
        log("TV não encontrada na rede")
        return False

    def _atualiza_codigos(self):
        try:
            raw = self._post("/sony/system", json.dumps(
                {"method": "getRemoteControllerInfo", "id": 1, "params": [], "version": "1.0"}),
                {"Content-Type": "application/json"})
            for c in json.loads(raw)["result"][1]:
                IRCC[c["name"]] = c["value"]
            self.codigos_ok = True
            log("códigos IRCC atualizados da TV:", len(IRCC))
        except Exception as ex:
            log("getRemoteControllerInfo falhou:", ex)

    def envia(self, nome):
        if nome == "PlayPause":
            nome = "Pause" if self.tocando else "Play"
            self.tocando = not self.tocando
        with self.cv:
            self.fila.append(nome)
            self.cv.notify()

    def _rpc(self, servico, metodo, params, versao="1.0"):
        raw = self._post(f"/sony/{servico}", json.dumps(
            {"method": metodo, "id": 1, "params": params, "version": versao}),
            {"Content-Type": "application/json", "X-Auth-PSK": PSK})
        d = json.loads(raw)
        if "error" in d:
            raise RuntimeError(d["error"])
        return d.get("result")

    def entrada(self, porta):
        with self.cv:
            self.fila.append(("hdmi", porta))
            self.cv.notify()

    def _hdmi(self, porta):
        try:
            if self._rpc("system", "getPowerStatus", [])[0].get("status") != "active":
                self._rpc("system", "setPowerStatus", [{"status": True}])
                log("TV ligada")
                time.sleep(4)
            self._rpc("avContent", "setPlayContent", [{"uri": f"extInput:hdmi?port={porta}"}])
            log("TV <- HDMI", porta)
        except Exception as ex:
            log("TV falhou HDMI", porta, ex)

    def _loop(self):
        while True:
            with self.cv:
                while not self.fila:
                    self.cv.wait()
                nome = self.fila.pop(0)
            if isinstance(nome, tuple):  # ("hdmi", porta)
                self._hdmi(nome[1])
                continue
            if not self.codigos_ok:
                self._atualiza_codigos()
            codigo = IRCC.get(nome)
            if not codigo:
                log("TV sem código para", nome)
                continue
            body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                    '<u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1">'
                    f'<IRCCCode>{codigo}</IRCCCode></u:X_SendIRCC></s:Body></s:Envelope>')
            try:
                self._post("/sony/IRCC", body, {
                    "Content-Type": 'text/xml; charset=UTF-8',
                    "SOAPACTION": '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"',
                    "X-Auth-PSK": PSK})
                log("TV <-", nome)
            except Exception as ex:
                log("TV falhou", nome, ex)


def acha_dispositivos():
    devs = []
    for p in evdev.list_devices():
        d = evdev.InputDevice(p)
        caps = d.capabilities()
        if d.name.startswith(NOME_RECEPTOR) and "Mouse" not in d.name and caps.get(e.EV_KEY):
            devs.append(d)
        else:
            d.close()
    return devs


def main():
    devs = []
    for _ in range(60):
        devs = acha_dispositivos()
        if devs:
            break
        time.sleep(2)
    if not devs:
        log("receptor não encontrado")
        sys.exit(1)
    time.sleep(1)  # deixa o udev terminar de marcar o receptor

    virt = {}
    for d in devs:
        d.grab()
        virt[d.fd] = evdev.UInput.from_device(
            d, name=d.name.replace(NOME_RECEPTOR, "Controle iHome (virtual)"),
            vendor=0x1d6b, product=0x7c01, phys="controle-tv")
        log("usando", d.path, d.name)
    fd2dev = {d.fd: d for d in devs}

    tv = TV()
    modo_tv = False
    modo_audio = False  # True: PgUp/PgDown trocam o áudio no vídeo (alterna com o microfone)
    engolidas = set()  # teclas tratadas aqui: o repete/solta delas também não vai ao Kodi

    while True:
        r, _, _ = select.select(list(fd2dev), [], [])
        for fd in r:
            try:
                eventos = list(fd2dev[fd].read())
            except OSError as ex:
                log("receptor sumiu:", ex)
                sys.exit(1)
            for ev in eventos:
                if ev.type != e.EV_KEY:
                    if not modo_tv:
                        virt[fd].write(ev.type, ev.code, ev.value)
                    continue
                code, val = ev.code, ev.value
                if code == TROCA:
                    if val != 1:
                        continue
                    modo_tv = not modo_tv
                    log("modo", "TV" if modo_tv else "Kodi")
                    kodi_aviso("Controle", "Agora controlando a TV" if modo_tv else "De volta ao Kodi")
                    continue
                if modo_tv:
                    nome = MAPA.get(code)
                    if val == 1 or (val == 2 and nome in REPETE):
                        if nome:
                            tv.envia(nome)
                        else:
                            log("modo TV: tecla sem mapa", e.KEY.get(code, code))
                else:
                    if code in FAIXA or code in MICROFONE:
                        if val != 1:
                            if code in engolidas:
                                if val == 0:
                                    engolidas.discard(code)
                                continue
                        else:
                            try:
                                pid = kodi_no_video()
                            except Exception as ex:
                                log("Kodi RPC falhou:", ex)
                                pid = None
                            if pid is not None:
                                engolidas.add(code)
                                try:
                                    if code in MICROFONE:
                                        modo_audio = not modo_audio
                                        log("PgUp/PgDown agora trocam", "áudio" if modo_audio else "legenda")
                                        kodi_aviso("PgUp / PgDown", "Trocam o áudio" if modo_audio
                                                   else "Trocam a legenda", 2000)
                                    else:
                                        kodi_troca_faixa(pid, modo_audio, FAIXA[code])
                                except Exception as ex:
                                    log("troca de faixa falhou:", ex)
                                continue
                    virt[fd].write(e.EV_KEY, code, val)
                    if code == e.KEY_HOMEPAGE and val == 1:  # segurar Voltar/Home → TV na HDMI do Kodi
                        tv.entrada(HDMI_KODI)


if __name__ == "__main__":
    main()
