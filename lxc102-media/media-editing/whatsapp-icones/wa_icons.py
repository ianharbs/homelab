# ícones 640x640 p/ os grupos do WhatsApp: gradiente + emoji Noto (512 px) + nome
import io, urllib.request
from PIL import Image, ImageDraw, ImageFont
F = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
ICONS = {  # arquivo: (emoji codepoint, texto, cor1, cor2)
    'series':  ('1f4fa', 'Séries',  (37, 99, 235), (15, 23, 42)),
    'filmes':  ('1f3ac', 'Filmes',  (220, 38, 38), (30, 10, 20)),
    'livros':  ('1f4da', 'Livros',  (217, 119, 6), (40, 20, 5)),
    'musica':  ('1f3b5', 'Música',  (147, 51, 234), (25, 10, 45)),
    'status':  ('1f916', 'Status\ne Pedidos',  (5, 150, 105), (5, 30, 30)),
    'aprovacoes': ('2705', 'Aprovar', (22, 163, 74), (5, 30, 15)),
    'casa':    ('1f3e1', 'Status e\nPedidos Casa', (234, 88, 12), (45, 15, 5)),
    'homelab': ('1f5a5', 'Homelab', (8, 145, 178), (8, 25, 40)),
    'comunidade': ('1f3e0', 'iHome', (79, 70, 229), (12, 18, 38)),
}
S = 640
import sys
for name, (cp, label, c1, c2) in ICONS.items():
    if len(sys.argv) > 1 and name not in sys.argv[1:]:
        continue
    img = Image.new('RGB', (S, S))
    d = ImageDraw.Draw(img)
    for y in range(S):  # gradiente diagonal simples
        t = y / S
        d.line([(0, y), (S, y)], fill=tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3)))
    em = Image.open(io.BytesIO(urllib.request.urlopen(
        f'https://raw.githubusercontent.com/googlefonts/noto-emoji/main/3D/png/512/emoji_u{cp}.png').read())).convert('RGBA')
    linhas = label.split('\n')
    tam, top, esp = (72, 450, 0) if len(linhas) == 1 else (60, 400, 66)  # 2 linhas: emoji menor e mais alto
    lado = 300 if len(linhas) == 1 else 250
    em = em.resize((lado, lado), Image.LANCZOS)
    img.paste(em, ((S - lado) // 2, 120 if len(linhas) == 1 else 110), em)
    f = ImageFont.truetype(F, tam)
    for k, ln in enumerate(linhas):
        b = d.textbbox((0, 0), ln, font=f)
        d.text(((S - (b[2] - b[0])) // 2 - b[0], top + k * esp), ln, font=f, fill='white')
    img.save(f'/root/wa-icons/{name}.jpg', quality=92)
    print(name)
