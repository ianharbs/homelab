"""Agente de Custo: gramas e tempo de uma peça e preço por canal.

- Sem fatiamento: estima pela malha (STL/3MF): casca (área × ~1 mm de paredes/topo/fundo) + 15 % de preenchimento
  do miolo, PLA 1,24 g/cm³; tempo = gramas ÷ g_per_hour. Sempre marcado como ESTIMATIVA.
- Com fatiamento (3MF fatiado do OrcaSlicer/Anycubic Slicer Next ou G-code): usa o peso e o tempo do fatiador.
A fórmula do preço é a mesma da calculadora (print3d-calc)."""
import math
import re
import struct
import zipfile
import xml.etree.ElementTree as ET

from common import config, avg_filament_kg, brl

DENSITY = 1.24      # PLA g/cm³ (PETG 1,27)
SHELL_MM = 1.0      # espessura equivalente de paredes + topo/fundo
INFILL = 0.15


def _tri_acc(tris):
    vol = area = 0.0
    for (a, b, c) in tris:
        vol += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        cx, cy, cz = u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]
        area += math.sqrt(cx * cx + cy * cy + cz * cz) / 2
    return abs(vol), area


def stl_mesh(path):
    """(volume mm³, área mm²) de um STL binário ou ASCII."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + n * 50 == len(data):
            tris = (struct.unpack_from("<9f", data, 84 + i * 50 + 12) for i in range(n))
            return _tri_acc(((t[0:3], t[3:6], t[6:9]) for t in tris))
    txt = data.decode("latin-1")
    vs = [tuple(map(float, m)) for m in re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", txt)]
    return _tri_acc(zip(vs[0::3], vs[1::3], vs[2::3]))


def threemf_mesh(path):
    vol = area = 0.0
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.lower().endswith(".model"):
                continue
            root = ET.fromstring(z.read(name))
            for mesh in root.iter():
                if not mesh.tag.endswith("mesh"):
                    continue
                verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
                         for v in mesh.iter() if v.tag.endswith("vertex")]
                tris = [(verts[int(t.get("v1"))], verts[int(t.get("v2"))], verts[int(t.get("v3"))])
                        for t in mesh.iter() if t.tag.endswith("triangle")]
                v, a = _tri_acc(tris)
                vol, area = vol + v, area + a
    return vol, area


def grams_from_mesh(vol_mm3, area_mm2):
    shell = min(vol_mm3, area_mm2 * SHELL_MM)
    solid = shell + max(0.0, vol_mm3 - shell) * INFILL
    return solid / 1000 * DENSITY


def _hms(s):
    """'1h 2m 3s' / '1d 2h' / '02:03:04' → horas."""
    s = s.strip()
    if re.fullmatch(r"\d+:\d+:\d+", s):
        h, m, sec = map(int, s.split(":"))
        return h + m / 60 + sec / 3600
    tot = 0.0
    for val, unit in re.findall(r"([\d.]+)\s*([dhms])", s):
        tot += float(val) * {"d": 24, "h": 1, "m": 1 / 60, "s": 1 / 3600}[unit]
    return tot or None


def sliced_3mf(path):
    """(gramas, horas) do 3MF fatiado (Metadata/slice_info.config), ou None se não estiver fatiado."""
    with zipfile.ZipFile(path) as z:
        if "Metadata/slice_info.config" not in z.namelist():
            return None
        root = ET.fromstring(z.read("Metadata/slice_info.config"))
    grams = secs = 0.0
    for plate in root.iter("plate"):
        md = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
        secs += float(md.get("prediction") or 0)
        w = float(md.get("weight") or 0)
        if not w:
            w = sum(float(f.get("used_g") or 0) for f in plate.iter("filament"))
        grams += w
    return (grams, secs / 3600) if grams else None


def sliced_gcode(path):
    grams = hours = None
    with open(path, "rb") as f:
        head = f.read(200_000).decode("latin-1")
        try:
            f.seek(-200_000, 2)
        except OSError:
            f.seek(0)
        tail = f.read().decode("latin-1")
    txt = head + "\n" + tail
    m = re.search(r";\s*(?:total )?filament (?:used|weight) \[g\]\s*[=:]\s*([\d.,\s]+)", txt, re.I)
    if m:
        grams = sum(float(x) for x in re.findall(r"[\d.]+", m.group(1)))
    m = re.search(r";\s*(?:estimated printing time(?: \(normal mode\))?|model printing time|total estimated time)\s*[=:]\s*([^\n;]+)", txt, re.I)
    if m:
        hours = _hms(m.group(1))
    if grams is None:
        m = re.search(r";\s*filament used \[mm\]\s*=\s*([\d.,\s]+)", txt)
        if m:   # 1,75 mm: 2,405 mm² de seção
            mm = sum(float(x) for x in re.findall(r"[\d.]+", m.group(1)))
            grams = mm * 2.405 / 1000 * DENSITY
    return (grams, hours) if grams else None


def analyze(paths):
    """Gramas/horas de um conjunto de arquivos. Retorna dict com estimated=True/False e detalhes."""
    cfg = config()
    sliced_g = sliced_h = 0.0
    mesh_g, notes, sliced_any = 0.0, [], False
    for p in paths:
        low = p.lower()
        try:
            if low.endswith(".3mf"):
                s = sliced_3mf(p)
                if s:
                    sliced_any = True
                    sliced_g += s[0]
                    sliced_h += s[1]
                    continue
                v, a = threemf_mesh(p)
                mesh_g += grams_from_mesh(v, a)
            elif low.endswith(".stl"):
                v, a = stl_mesh(p)
                mesh_g += grams_from_mesh(v, a)
            elif low.endswith((".gcode", ".gco", ".g")):
                s = sliced_gcode(p)
                if s:
                    sliced_any = True
                    sliced_g += s[0]
                    sliced_h += s[1] or s[0] / cfg["g_per_hour"]
        except Exception as e:
            notes.append(f"{p.rsplit('/', 1)[-1]}: {e}")
    if sliced_any:
        return {"grams": sliced_g, "hours": sliced_h, "estimated": False, "notes": notes}
    return {"grams": mesh_g, "hours": mesh_g / cfg["g_per_hour"], "estimated": True, "notes": notes}


def price(grams, hours, per_kg=None, supplies=0.0, material="PLA"):
    """Custo por peça e preço sugerido por canal (fórmula da calculadora)."""
    c = config()
    per_kg = per_kg or avg_filament_kg(material)
    material_cost = grams / 1000 * per_kg
    energy = hours * c["watts"] / 1000 * c["tariff"]
    machine = hours * (c["printer_cost"] / max(1, c["life_hours"]) + c["maint_h"])
    labor = c["labor_min"] / 60 * c["labor_rate"]
    fail = (material_cost + energy + machine) * c["fail_pct"] / 100
    cost = material_cost + energy + machine + labor + supplies + c["packaging"] + fail
    prices = {}
    for key, ch in c["channels"].items():
        p = (cost * c["markup"] + ch["fixed"]) / max(0.01, 1 - ch["pct"] / 100)
        p = math.ceil(p) - 0.10 if math.ceil(p) - 0.10 >= p else math.ceil(p) + 0.90
        floor = (cost * c["min_markup"] + ch["fixed"]) / max(0.01, 1 - ch["pct"] / 100)
        prices[key] = {"name": ch["name"], "price": round(p, 2), "floor": round(floor, 2),
                       "profit": round(p - p * ch["pct"] / 100 - ch["fixed"] - cost, 2)}
    return {"cost": round(cost, 2), "per_kg": round(per_kg, 2), "parts": {
        "filamento": material_cost, "energia": energy, "máquina": machine, "mão de obra": labor,
        "insumos": supplies, "embalagem": c["packaging"], "falhas": fail}, "prices": prices}


def summary(grams, hours, estimated, pr):
    tag = "≈ estimativa pela malha" if estimated else "do fatiador"
    lines = [f"⚖️ <b>{grams:.0f} g</b> · ⏱ <b>{hours:.1f} h</b> ({tag})",
             f"💰 Custo por peça: <b>{brl(pr['cost'])}</b> (filamento a {brl(pr['per_kg'])}/kg)"]
    for p in pr["prices"].values():
        lines.append(f"  • {p['name']}: <b>{brl(p['price'])}</b> (lucro {brl(p['profit'])}, mínimo {brl(p['floor'])})")
    if estimated:
        lines.append("<i>Mande o 3MF fatiado ou o G-code respondendo esta mensagem para o valor exato.</i>")
    return "\n".join(lines)
