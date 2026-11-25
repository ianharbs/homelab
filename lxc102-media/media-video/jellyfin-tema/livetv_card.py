# gera capa 1280x720 p/ a "TV ao Vivo" no estilo das capas automáticas do Jellyfin
import json, io, os, urllib.request, random
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter
K=os.environ['K']; H={'Authorization':'MediaBrowser Token="%s"'%K}
def get(p):
    return urllib.request.urlopen(urllib.request.Request('http://localhost:8096'+p,headers=H)).read()
ch=json.loads(get('/LiveTv/Channels?Limit=400&EnableImages=true'))['Items']
ch=[c for c in ch if c.get('ImageTags',{}).get('Primary')]
W,Hh=1280,720; cols,rows=8,5; cw,chh=W//cols,Hh//rows
bg=Image.new('RGB',(W,Hh),(18,22,34)); random.seed(7)
pick=ch[:cols*rows] if len(ch)>=cols*rows else ch
for i,c in enumerate(pick):
    try: logo=Image.open(io.BytesIO(get('/Items/%s/Images/Primary?maxWidth=200'%c['Id']))).convert('RGBA')
    except Exception: continue
    tile=Image.new('RGB',(cw,chh),(28+(i%3)*6,34,52)); logo.thumbnail((cw-30,chh-30))
    tile.paste(logo,((cw-logo.width)//2,(chh-logo.height)//2),logo)
    bg.paste(tile,((i%cols)*cw,(i//cols)*chh))
img=ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(1.2))).enhance(0.45)
d=ImageDraw.Draw(img); f=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',150)
t='TV ao Vivo'; b=d.textbbox((0,0),t,font=f)
d.text(((W-(b[2]-b[0]))//2-b[0],(Hh-(b[3]-b[1]))//2-b[1]),t,font=f,fill='white')
img.save('/root/views/livetv.png'); print(len(pick),'logos')
