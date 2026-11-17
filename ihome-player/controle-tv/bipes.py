import math, struct, wave, os, sys
pasta = sys.argv[1]; os.makedirs(pasta, exist_ok=True)
def tom(f, dur, vol=0.35, taxa=48000):
    n = int(taxa*dur); out = []
    for i in range(n):
        env = min(1, i/(taxa*0.008), (n-i)/(taxa*0.02))  # sem estalo
        out.append(vol*env*math.sin(2*math.pi*f*i/taxa))
    return out
def salva(nome, partes):
    amostras = [x for p in partes for x in p]
    with wave.open(os.path.join(pasta, nome), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(b"".join(struct.pack("<h", int(x*32767)) for x in amostras))
salva("ouvindo.wav", [tom(660, .09), tom(0, .03), tom(990, .12)])
salva("erro.wav", [tom(520, .12), tom(0, .03), tom(350, .18)])
print("ok")
