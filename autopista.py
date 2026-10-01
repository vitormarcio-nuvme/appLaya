import argparse
import json
import math
import random
import re
import sys
import threading
import time
import urllib.request

try:
    import tkinter as tk
    import tkinter.font as tkfont
except ImportError:
    sys.exit("tkinter não encontrado. No Debian/Ubuntu: sudo apt install python3-tk")

# ─────────────────────────── configuração ───────────────────────────

LAYA_URL   = "http://localhost:8000/v1/systemone"
TIMEOUT_S  = 3.0     # timeout de cada chamada ao Laya
DECIDE_S   = 0.52    # intervalo entre perguntas ao agente LLM
LOCAL_S    = 0.14    # intervalo do piloto heurístico
PROBE_S    = 3.0     # tentativa de reconexão quando offline
HOLD_S     = 0.75    # duração do efeito de ACELERAR/FREAR

# mundo (coordenadas lógicas em px · 1 m = 7 px)
W, H       = 480, 640
PXM        = 7.0
PLAYER_Y   = H - 140
LANES      = (128.0, 240.0, 352.0)
DIVS       = (184.0, 296.0)
ASPH_L, ASPH_R = 56.0, 424.0
EDGE_L, EDGE_R = 72.0, 408.0
CAR_W, CAR_H  = 54.0, 96.0
SIDE_M     = 1.2   # margem que caracteriza "AO LADO" (metros)
NOMES      = ("ESQUERDA", "CENTRO", "DIREITA")

PLAYER_TYPE = dict(kind="player", w=54.0, h=96.0)
TYPES = (
    dict(kind="compacto", w=52, h=88,  vmin=62, vmax=92),
    dict(kind="sedan",    w=56, h=104, vmin=58, vmax=88),
    dict(kind="caminhão", w=62, h=140, vmin=46, vmax=62),
)
OBST_COLORS = ("#b4553f", "#3f6d7a", "#d8cfba", "#6b7075", "#71804f", "#9b6a43")

# cores da interface
BG, PANEL, PANEL2, LINE = "#131210", "#1a1815", "#211e19", "#2c2822"
INK, DIM, FAINT         = "#ece7db", "#948b7a", "#655e50"
AMBER, RED, GREEN       = "#f0a13a", "#e5484d", "#a3b56b"
CMD_COLORS = {"ACELERAR": GREEN, "FREAR": RED, "ESQUERDA": INK,
              "DIREITA": INK, "MANTER": DIM}
CMD_LIST = ("ACELERAR", "FREAR", "ESQUERDA", "DIREITA", "MANTER")

SYS_PROMPT = """Você é o piloto automático de um carro em uma rodovia de 3 faixas (ESQUERDA, CENTRO, DIREITA), vista de cima.
Objetivo: não colidir e manter boa velocidade.

Segurança:
- "AO LADO" indica veículo na sua altura naquela faixa: NUNCA troque para ela. Se sua frente estiver bloqueada e as faixas vizinhas tiverem veículo ao lado, FREIE e aguarde o carro ao lado passar.
- Não troque para faixa cujo "atrás" esteja aproximando rápido.
- Com "troca: em andamento", NÃO comande outra troca; só é aceito o comando que volta à faixa de origem (abortar).

Condução:
- Troque de faixa quando houver veículo lento à frente e uma vizinha segura tiver mais espaço.
- Se a frente estiver perto e não houver rota segura, freie.
- "TTC" é o tempo até alcançar o veículo à frente: com TTC abaixo de 3 s, aja (faixa segura ou FREAR).
- A velocidade de cruzeiro vem na primeira linha. Acelere quando a frente estiver livre e você estiver abaixo dela; não passe dela.
- FREAR reduz bastante a velocidade e MANTER conserva a velocidade atual; depois de frear, use ACELERAR para retomar o cruzeiro.

Comandos válidos: ACELERAR, FREAR, ESQUERDA, DIREITA, MANTER.
Responda com UMA única palavra, em maiúsculas, sem pontuação e sem explicações."""

# ─────────────────────────── helpers ───────────────────────────

def clamp(v, a, b): return max(a, min(b, v))

def shade(hexcolor, amt):
    n = int(hexcolor[1:], 16)
    r = clamp((n >> 16) + amt, 0, 255)
    g = clamp(((n >> 8) & 255) + amt, 0, 255)
    b = clamp((n & 255) + amt, 0, 255)
    return f"#{r:02x}{g:02x}{b:02x}"

def avg(xs): return sum(xs) / len(xs) if xs else None

def fmt_time(s):
    return f"{int(s // 60):02d}:{int(s % 60):02d}"

FONTS, SC = {}, 1.25

def F(kind, size, weight="normal"):
    return (FONTS[kind], max(8, int(round(size * SC))), weight)

_font_cache = {}
def measure(fonttuple, text):
    f = _font_cache.get(fonttuple)
    if f is None:
        f = tkfont.Font(font=fonttuple); _font_cache[fonttuple] = f
    return f.measure(text)

# ────────────────────── cliente do Laya (LLM) ──────────────────────

CMD_WORDS = (
    ("ESQUERDA", re.compile(r"ESQUERDA|ESQUERD|\bESQ\b|LEFT", re.I)),
    ("DIREITA",  re.compile(r"DIREITA|\bDIR\b|RIGHT", re.I)),
    ("FREAR",    re.compile(r"FREAR|FREIO|BRAKE|\bBREAK\b", re.I)),
    ("ACELERAR", re.compile(r"ACELERA|\bACEL\b|GAS|THROTTLE", re.I)),
    ("MANTER",   re.compile(r"MANTER|SEGUIR|EM FRENTE|RETO|NADA|NONE|KEEP|CONTINUAR", re.I)),
)

def parse_cmd(txt):
    if not txt:
        return None
    t = str(txt).upper()
    best = None
    for nome, rx in CMD_WORDS:
        m = rx.search(t)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), nome) 
    if best:
        return best[1]
    m = re.fullmatch(r"\s*([LRUDABKM])\s*", t) 
    if m:
        return {"L": "ESQUERDA", "R": "DIREITA", "U": "ACELERAR", "D": "FREAR",
                "A": "ACELERAR", "B": "FREAR", "K": "MANTER", "M": "MANTER"}[m.group(1)]
    return None

def note_from(txt, cmd):
    if not txt:
        return ""
    s = re.sub(r"\s+", " ", str(txt)).strip()
    if cmd:
        i = s.upper().find(cmd)
        if i >= 0:
            s = s[:i] + s[i + len(cmd):]
    return s.strip(" :,-–—")[:70]

def ask_laya(url, model, user_msg, timeout=TIMEOUT_S):
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        "temperature": 0.2, "max_tokens": 24, "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(data, str):
        return data
    ch = (data.get("choices") or [{}])[0]
    txt = (ch.get("message") or {}).get("content") or ch.get("text") \
        or data.get("content") or data.get("response") \
        or data.get("output") or data.get("completion") or ""
    if isinstance(txt, list):
        txt = " ".join(map(str, txt))
    return str(txt)

# ─────────────────────────── o jogo ───────────────────────────

class Game:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.agent_mode = "llm"
        self.reset()

    def reset(self):
        self.mode = "ready"
        self.anim = 0.0; self.t = 0.0
        self.v = 0.0; self.set_v = 105.0
        self.x = LANES[1]; self.target_lane = 1; self.from_lane = None
        self.tilt = 0.0; self.spin = 0.0
        self.dist = 0.0; self.world_px = 0.0
        self.obstacles = []; self.sides = []; self.skids = []; self.parts = []
        self.blink = {"dir": 0, "until": 0.0}; self.brake = False
        self.cmd = None; self.cmd_at = -1e9; self.cmd_src = None; self.last_cmd = None
        self.shake = 0.0; self.spawn_px = 260.0; self.side_px = -999.0; self.side_gap = 120.0
        self.crashed_with = None; self.crash_type = None
        self.last_smoke = 0.0
        self.keys_up = self.keys_dn = False
        self.stats = dict(decisions=0, fails=0, fallback=0, lat=[], counts={})
        self.logs = []; self.log_dirty = True; self.hist_dirty = True
        for _ in range(12):                      # cenário vivo já na tela inicial
            self.spawn_side()
            self.sides[-1]["y"] = self.rng.uniform(-40, H + 40)

    def start_run(self):
        self.reset()
        self.mode = "run"

    def level(self):      return 1 + int(self.dist // 700)
    def vmax(self):       return min(230.0, 175.0 + 8.0 * self.level())
    def spawn_gap(self):  return max(190.0, 310.0 - 16.0 * (self.level() - 1))
    def changing(self):
        return self.from_lane is not None and abs(self.x - LANES[self.target_lane]) > 2.0
    def nearest_lane(self):
        return min(range(3), key=lambda L: abs(self.x - LANES[L]))
    def cruise(self):     return min(self.vmax() - 5.0, 95.0 + 10.0 * self.level())

    def perceive(self):
        out = []
        for L in range(3):
            d = dict(side=None, side_kind=None, side_pos=None,
                     ahead=None, ahead_v=None, ahead_kind=None,
                     behind=None, behind_v=None, behind_kind=None)
            for o in self.obstacles:
                if o["lane"] != L:
                    continue
                h = o["type"]["h"]
                dy = PLAYER_Y - o["y"] 
                half = (CAR_H + h) / 2.0
                own_front = (L == self.target_lane and dy > 0) 
                if abs(dy) < half + SIDE_M * PXM and not own_front: 
                    if d["side"] is None or abs(dy) < d["side"]:
                        d["side"] = abs(dy); d["side_kind"] = o["type"]["kind"]
                        dy_m = dy / PXM
                        d["side_pos"] = ("na mesma altura" if abs(dy_m) <= 1.0
                                         else ("levemente à frente" if dy_m > 0
                                               else "levemente atrás"))
                elif dy > 0: 
                    gap = max(0.0, (dy - half) / PXM)
                    if d["ahead"] is None or gap < d["ahead"]:
                        d["ahead"], d["ahead_v"], d["ahead_kind"] = gap, o["v"], o["type"]["kind"]
                else:                                        # atrás
                    gap = (-dy - half) / PXM
                    if d["behind"] is None or gap < d["behind"]:
                        d["behind"], d["behind_v"], d["behind_kind"] = gap, o["v"], o["type"]["kind"]
            out.append(d)
        return out

    def build_snapshot(self):
        per = self.perceive()
        troca = (f"em andamento para {NOMES[self.target_lane]} (abortável)"
                 if self.changing() else "nenhuma")
        linhas = [f"velocidade {self.v:.0f} km/h (cruzeiro {self.cruise():.0f}) "
                  f"· faixa {NOMES[self.nearest_lane()]} "
                  f"· troca: {troca} · nível {self.level()}"]
        for i, p in enumerate(per):
            partes = []
            if p["side"] is not None:
                partes.append(f"{p['side_kind']} AO LADO ({p['side_pos']})")
            elif p["ahead"] is None:
                partes.append("livre")
            else:
                txt = f"{p['ahead_kind']} a {p['ahead']:.0f} m a {p['ahead_v']:.0f} km/h"
                closing = (self.v - p["ahead_v"]) / 3.6 
                if closing > 0.5:
                    txt += f" (TTC {p['ahead'] / closing:.1f} s)"
                partes.append(txt)
            if p["behind"] is not None and p["behind"] < 30:
                tag = " (aproximando)" if p["behind_v"] - self.v > 8 else ""
                partes.append(f"atrás: {p['behind_kind']} a {p['behind']:.0f} m{tag}")
            linhas.append(f"{NOMES[i]}: " + " · ".join(partes))
        if self.logs:
            ult = self.logs[0]
            linhas.append(f"última ação: {ult['cmd']} (há {max(0.0, self.t - ult['t']):.1f} s)")
        linhas.append("Responda somente o comando.")
        return "\n".join(linhas)

    def local_pilot(self):
        per = self.perceive()
        cur, v = self.target_lane, self.v

        if self.changing() and per[cur]["side"] is not None:
            origem = self.from_lane if self.from_lane is not None else cur
            if origem != cur and per[origem]["side"] is None:
                return "DIREITA" if origem > cur else "ESQUERDA"
            return "FREAR"

        def rota_segura(L):
            p = per[L]
            if p["side"] is not None:
                return False
            if p["behind"] is not None and p["behind"] < 10 and p["behind_v"] - v > 8:
                return False
            return True

        def espaco(L):
            a = per[L]["ahead"]
            return 999.0 if a is None else a

        my = per[cur]; my_a = espaco(cur)
        safe, danger = 10 + 0.30 * v, 6 + 0.16 * v
        cands = [L for L in (cur - 1, cur + 1) if 0 <= L <= 2 and rota_segura(L)]

        if my_a < danger:
            if cands:
                best = max(cands, key=espaco)
                if espaco(best) > my_a + 15:
                    return "DIREITA" if best > cur else "ESQUERDA"
            return "FREAR" 

        if my_a < safe:
            if cands:
                best = max(cands, key=espaco)
                if espaco(best) > max(my_a * 1.5, my_a + 20):
                    return "DIREITA" if best > cur else "ESQUERDA"
            if my_a < safe * 0.65 and my["ahead_v"] is not None and my["ahead_v"] < v - 8:
                return "FREAR"

        if (my["behind"] is not None and my["behind"] < 12
                and my["behind_v"] - v > 15 and my_a > safe):
            return "ACELERAR"

        alvo = self.cruise()
        if v < alvo - 6 and my_a > safe * 1.3:
            return "ACELERAR"
        if v > alvo + 18:
            return "FREAR"
        return "MANTER"

    def apply_command(self, cmd, src, ms=None, note=""):
        if not cmd:
            return
        lateral = cmd in ("ESQUERDA", "DIREITA")
        if lateral and self.changing():
            d = -1 if cmd == "ESQUERDA" else 1
            if self.target_lane + d != self.from_lane:
                cmd, lateral = "MANTER", False
                note = note or "2ª troca ignorada"
        novo = cmd != self.last_cmd 
        if lateral:
            d = -1 if cmd == "ESQUERDA" else 1
            nl = clamp(self.target_lane + d, 0, 2)
            if nl != self.target_lane:
                self.from_lane = self.target_lane
                self.target_lane = nl
                self.blink = {"dir": d, "until": self.anim + 0.9}
                novo = True
            else:
                self.blink = {"dir": d, "until": self.anim + 0.45}
        if novo:
            self.stats["decisions"] += 1
            self.stats["counts"][cmd] = self.stats["counts"].get(cmd, 0) + 1
            self.hist_dirty = True
        self.cmd, self.cmd_at, self.cmd_src = cmd, self.t, src
        if ms is not None and src == "laya":
            self.stats["lat"].append(ms)
            if len(self.stats["lat"]) > 14:
                self.stats["lat"].pop(0)
        if cmd != self.last_cmd:
            self.last_cmd = cmd
            self.logs.insert(0, dict(t=self.t, cmd=cmd, src=src, ms=ms, note=note))
            del self.logs[40:]
            self.log_dirty = True

    # ── tráfego ──
    def try_spawn(self):
        perto = [o for o in self.obstacles if o["y"] < 330]
        ocupadas = {o["lane"] for o in perto}
        if len(ocupadas) >= 2 and (self.world_px - self.spawn_px) < 640:
            return 
        cands = [L for L in (0, 1, 2)
                 if not any(o["lane"] == L and o["y"] < 240 for o in self.obstacles)]
        if not cands:
            return
        lane = self.rng.choice(cands)
        r = self.rng.random()
        t = TYPES[2] if r < 0.24 else (TYPES[1] if r < 0.62 else TYPES[0])
        vd = self.rng.uniform(t["vmin"], t["vmax"])
        self.obstacles.append(dict(
            lane=lane, x=LANES[lane] + self.rng.uniform(-6, 6),
            y=-t["h"] / 2 - 24, v=vd, vdes=vd, 
            type=t, color=self.rng.choice(OBST_COLORS)))
        self.spawn_px = self.world_px

    def spawn_side(self):
        side = 0 if self.rng.random() < 0.5 else 1
        self.sides.append(dict(
            side=side, y=-60.0,
            x=(W - 14 - self.rng.uniform(0, 34)) if side else (14 + self.rng.uniform(0, 34)),
            kind=self.rng.choice(("tree", "tree", "bush", "post", "rock")),
            s=0.7 + self.rng.random() * 0.6))

    def update_traffic(self, dt):
        por_faixa = {}
        for o in self.obstacles:
            por_faixa.setdefault(o["lane"], []).append(o)
        for lst in por_faixa.values():
            lst.sort(key=lambda o: o["y"])                  # o mais à frente primeiro
            for i, o in enumerate(lst):
                alvo = o["vdes"]
                if i > 0:
                    l = lst[i - 1]
                    gap = (o["y"] - l["y"] - (o["type"]["h"] + l["type"]["h"]) / 2.0) / PXM
                    seguro = 6.0 + 0.8 * (o["v"] / 3.6) 
                    if gap < seguro:
                        alvo = min(alvo, l["v"])           
                    if gap < seguro * 0.5:
                        alvo = min(alvo, l["v"] * 0.85)  
                    if gap < 1.5:
                        alvo = min(alvo, l["v"] * 0.6)    
                if o["v"] > alvo:
                    o["v"] = max(alvo, o["v"] - 45.0 * dt) 
                else:
                    o["v"] = min(alvo, o["v"] + 15.0 * dt) 

    def update_sides(self, scroll):
        for o in self.sides:
            o["y"] += scroll
        self.sides = [o for o in self.sides if o["y"] < H + 80]
        if self.world_px - self.side_px > self.side_gap:
            self.side_px = self.world_px
            self.side_gap = 80 + self.rng.uniform(0, 150)
            self.spawn_side()

    def update_fx(self, dt, scroll_px):
        for p in self.parts:
            p["life"] -= dt
            if p["type"] == "smoke":
                p["r"] += p["vr"] * dt
                p["y"] += (p["vy"] + scroll_px) * dt
            else:
                p["x"] += p["vx"] * dt
                p["y"] += (p["vy"] + scroll_px) * dt
                p["vx"] *= max(0.0, 1 - 2.4 * dt)
                p["vy"] *= max(0.0, 1 - 2.4 * dt)
        self.parts = [p for p in self.parts if p["life"] > 0 and p["y"] < H + 40]
        for s in self.skids:
            s["y"] += scroll_px * dt
            s["life"] -= dt
        self.skids = [s for s in self.skids if s["life"] > 0 and s["y"] < H + 20]

    # ── física ──
    def update(self, dt):
        self.t += dt
        age = self.t - self.cmd_at
        ativo = self.cmd is not None and age < HOLD_S and self.agent_mode != "manual"
        braking = False
        if ativo:
            if self.cmd == "ACELERAR":
                self.set_v = min(self.vmax(), self.set_v + 44.0 * dt)
            elif self.cmd == "FREAR":
                self.set_v = max(28.0, self.set_v - 100.0 * dt)
                braking = True
        if self.agent_mode == "manual":
            if self.keys_up:
                self.set_v = min(self.vmax(), self.set_v + 85.0 * dt)
            if self.keys_dn:
                self.set_v = max(28.0, self.set_v - 125.0 * dt)
                braking = True
        self.brake = braking
        if self.v < self.set_v:
            self.v = min(self.set_v, self.v + 62.0 * dt)
        else:
            self.v = max(self.set_v, self.v - (165.0 if braking else 85.0) * dt)

        tx = LANES[self.target_lane]; dx = tx - self.x
        if abs(dx) < 0.6:
            self.x = tx; self.from_lane = None
            vlat = 0.0
        else:
            vlat = (1 if dx > 0 else -1) * min(abs(dx) * 9.0, 335.0)
            self.x += vlat * dt
        self.tilt += (vlat / 335.0 * 0.10 - self.tilt) * min(1.0, 14.0 * dt)

        if abs(vlat) > 175:
            self.skids.append(dict(x=self.x - 17, y=PLAYER_Y + 28, life=1.2))
            self.skids.append(dict(x=self.x + 17, y=PLAYER_Y + 28, life=1.2))
            if len(self.skids) > 240:
                del self.skids[: len(self.skids) - 240]
        if self.brake and self.v > 60 and self.t - self.last_smoke > 0.045:
            self.last_smoke = self.t
            for sd in (-1, 1):
                self.parts.append(dict(type="smoke", x=self.x + sd * 20,
                    y=PLAYER_Y + CAR_H / 2 + 6, r=4 + self.rng.uniform(0, 3),
                    vr=16, vy=26 + self.rng.uniform(0, 30), life=0.65))

        mps = self.v / 3.6
        scroll = mps * PXM * dt
        self.world_px += scroll; self.dist += mps * dt
        self.update_sides(scroll)
        self.update_traffic(dt)
        for o in self.obstacles:
            o["y"] += (mps - o["v"] / 3.6) * PXM * dt
        self.obstacles = [o for o in self.obstacles if -520 < o["y"] < H + 180]
        if self.world_px - self.spawn_px > self.spawn_gap():
            self.try_spawn()
        self.update_fx(dt, mps * PXM)

        for o in self.obstacles: 
            if (abs(o["x"] - self.x) < (o["type"]["w"] + CAR_W) / 2 * 0.68
                    and abs(o["y"] - PLAYER_Y) < (o["type"]["h"] + CAR_H) / 2 * 0.74):
                self.crash(o)
                break

    def crash(self, o):
        k = o["type"]["kind"]
        self.mode = "over"
        self.crashed_with = k
        if self.changing() and o["lane"] == self.target_lane:
            self.crash_type = f"lateral — trocou para faixa com {k} ao lado"
        elif o["lane"] == self.nearest_lane():
            self.crash_type = (f"traseira — {k} à frente" if o["y"] < PLAYER_Y
                               else f"traseira — {k} bateu por trás")
        else:
            self.crash_type = f"lateral — {k} em faixa vizinha"
        self.shake = 15.0
        self.spin = (1 if self.rng.random() < 0.5 else -1) * (1.6 + self.rng.random())
        self.cmd = None; self.cmd_at = -1e9
        for _ in range(26):
            a = self.rng.uniform(0, math.pi * 2)
            sp = 60 + self.rng.uniform(0, 220)
            self.parts.append(dict(type="debris", x=self.x, y=PLAYER_Y,
                vx=math.cos(a) * sp, vy=math.sin(a) * sp,
                life=0.9 + self.rng.uniform(0, 0.6),
                w=3 + self.rng.uniform(0, 6), h=2 + self.rng.uniform(0, 4),
                color=self.rng.choice(("#f0a13a", "#3a3632", "#c8c2b2", "#e5484d"))))

    def update_over(self, dt):
        self.t += dt
        self.v = max(0.0, self.v - 170.0 * dt)
        self.spin *= max(0.0, 1 - 2.5 * dt)
        self.tilt += self.spin * dt 
        self.brake = True
        mps = self.v / 3.6
        scroll = mps * PXM * dt
        self.world_px += scroll
        self.update_sides(scroll)
        self.update_traffic(dt)
        for o in self.obstacles:
            o["y"] += (mps - o["v"] / 3.6) * PXM * dt
        self.obstacles = [o for o in self.obstacles if -520 < o["y"] < H + 180]
        if self.t - self.last_smoke > 0.07:    # fumaça do capô amassado
            self.last_smoke = self.t
            self.parts.append(dict(type="smoke", x=self.x + self.rng.uniform(-9, 9),
                y=PLAYER_Y - CAR_H / 2 + 8, r=5 + self.rng.uniform(0, 4),
                vr=22, vy=10, life=0.9))
        self.update_fx(dt, mps * PXM)

# ─────────────────── agente Laya (thread + fallback) ───────────────────

class LayaAgent:
    def __init__(self, url, model_var):
        self.url = url
        self.model_var = model_var
        self.lock = threading.Lock()
        self.busy = False
        self.result = None
        self.last_ask = -1e9
        self.last_local = -1e9
        self.consecutive = 0
        self.state = "standby"
        self.gen = 0 

    def reset(self):
        with self.lock:
            self.gen += 1
            self.consecutive = 0
            self.state = "standby"
            self.last_ask = -1e9
            self.last_local = -1e9
            self.result = None

    def tick(self, g):
        intervalo = DECIDE_S if self.consecutive < 2 else PROBE_S
        with self.lock:
            if self.busy or g.t - self.last_ask < intervalo:
                return
            self.busy = True
            self.last_ask = g.t
            gen = self.gen
            model = self.model_var.get() or "systemone"
        snap = g.build_snapshot()
        threading.Thread(target=self._worker, args=(snap, model, gen), daemon=True).start()

    def _worker(self, snap, model, gen):
        t0 = time.perf_counter()
        cmd, note, err = None, "", None
        try:
            raw = ask_laya(self.url, model, snap)
            cmd = parse_cmd(raw)
            note = note_from(raw, cmd)
        except Exception as e:
            err = type(e).__name__
        ms = (time.perf_counter() - t0) * 1000
        with self.lock:
            self.result = dict(cmd=cmd, note=note, err=err, ms=ms, gen=gen)
            self.busy = False

    def poll(self):
        with self.lock:
            r = self.result
            self.result = None
            return r

def drive(g, agent):
    if g.mode != "run":
        return
    m = g.agent_mode
    if m == "manual":
        return
    if m == "local":
        if g.t - agent.last_local >= LOCAL_S:
            agent.last_local = g.t
            g.apply_command(g.local_pilot(), "local")
        return
    agent.tick(g)
    res = agent.poll()
    if res is not None and res["gen"] == agent.gen:
        if res["err"] is None:
            agent.consecutive = 0
            agent.state = "online"
            if res["cmd"]:
                g.apply_command(res["cmd"], "laya", ms=res["ms"], note=res["note"])
            else:
                g.stats["fallback"] += 1
                g.apply_command(g.local_pilot(), "local",
                                note=("resposta ilegível: " + res["note"])[:70])
        else:
            agent.consecutive += 1
            g.stats["fails"] += 1
            g.stats["fallback"] += 1
            if agent.consecutive >= 2:
                agent.state = "offline"
            g.apply_command(g.local_pilot(), "local", note=res["err"][:60])
    if agent.state == "offline" and g.t - agent.last_local >= LOCAL_S:
        agent.last_local = g.t
        g.apply_command(g.local_pilot(), "local")


class View:
    def __init__(self, canvas, game, agent):
        self.cv, self.game, self.agent = canvas, game, agent

    # -- terreno --
    def draw_grass(self):
        cv = self.cv; g = self.game
        cv.create_rectangle(0, 0, W, H, fill="#4d5936", outline="")
        p = 90
        y = (g.world_px % (p * 2)) - p * 2
        while y < H:
            cv.create_rectangle(0, y, W, y + p, fill="#495433", outline="")
            y += p * 2

    def draw_road(self):
        cv = self.cv; g = self.game
        cv.create_rectangle(ASPH_L, 0, ASPH_R, H, fill="#34373b", outline="")
        for lx in LANES:                                    # trilhas de pneus
            for dx in (-27, 16):
                cv.create_rectangle(lx + dx, 0, lx + dx + 11, H, fill="#2f3236", outline="")
        cv.create_rectangle(ASPH_L, 0, ASPH_L + 7, H, fill="#d8d2c1", outline="")
        cv.create_rectangle(ASPH_R - 7, 0, ASPH_R, H, fill="#d8d2c1", outline="")
        rp = 44
        y = (g.world_px % rp) - rp
        while y < H:                                        # rumble strips
            cv.create_rectangle(ASPH_L, y, ASPH_L + 7, y + rp, fill="#c2402f", outline="")
            cv.create_rectangle(ASPH_R - 7, y, ASPH_R, y + rp, fill="#c2402f", outline="")
            y += rp * 2
        cv.create_rectangle(EDGE_L - 2, 0, EDGE_L + 3, H, fill="#ece5cf", outline="")
        cv.create_rectangle(EDGE_R - 3, 0, EDGE_R + 2, H, fill="#ece5cf", outline="")
        dp = 64
        off = g.world_px % dp
        for x in DIVS:
            y = off - dp
            while y < H:
                cv.create_rectangle(x - 2.5, y, x + 2.5, y + 38, fill="#d9d2bd", outline="")
                y += dp

    def draw_side(self, o):
        cv = self.cv; x, y, s = o["x"], o["y"], o["s"]
        k = o["kind"]
        if k == "tree":
            cv.create_oval(x - 10 * s, y - 1 * s, x + 20 * s, y + 13 * s, fill="#3e4a2b", outline="")
            cv.create_rectangle(x - 2.5 * s, y - 4 * s, x + 2.5 * s, y + 7 * s, fill="#4a3b28", outline="")
            cv.create_oval(x - 15 * s, y - 23 * s, x + 15 * s, y + 7 * s, fill="#4f6134", outline="")
            cv.create_oval(x - 14 * s, y - 22 * s, x + 6 * s, y - 2 * s, fill="#61763f", outline="")
        elif k == "bush":
            cv.create_oval(x - 9 * s, y - 9 * s, x + 9 * s, y + 9 * s, fill="#566b39", outline="")
            cv.create_oval(x - 12 * s, y - 6 * s, x, y + 6 * s, fill="#6b7f45", outline="")
        elif k == "post":
            cv.create_rectangle(x - 2, y - 40 * s, x + 2, y + 2, fill="#8f8a7b", outline="")
            if o["side"]:
                cv.create_rectangle(x - 26, y - 40 * s, x - 2, y - 40 * s + 3, fill="#8f8a7b", outline="")
                cv.create_rectangle(x - 26, y - 38 * s, x - 18, y - 38 * s + 4, fill="#e8dcae", outline="")
            else:
                cv.create_rectangle(x + 2, y - 40 * s, x + 26, y - 40 * s + 3, fill="#8f8a7b", outline="")
                cv.create_rectangle(x + 18, y - 38 * s, x + 26, y - 38 * s + 4, fill="#e8dcae", outline="")
        else:
            cv.create_oval(x - 8 * s, y - 6 * s, x + 8 * s, y + 6 * s, fill="#78746a", outline="")
            cv.create_oval(x - 6 * s, y - 4 * s, x + 2 * s, y + 2 * s, fill="#8d897d", outline="")

    def draw_vehicle(self, x, y, t, color, ang=0.0, brake=False, alert=False,
                     player=False, blink_on=False, blink_dir=0):
        cv = self.cv
        w, h = t["w"], t["h"]
        ca, sa = math.cos(ang), math.sin(ang)

        def roff(lx, ly):
            return (x + lx * ca - ly * sa, y + lx * sa + ly * ca)

        def rpoly(ox, oy, rw, rh, fill, outline="", ow=0):
            pts = []
            for dx, dy in ((-rw/2, -rh/2), (rw/2, -rh/2), (rw/2, rh/2), (-rw/2, rh/2)):
                px, py = roff(ox + dx, oy + dy)
                pts += [px, py]
            cv.create_polygon(pts, fill=fill, outline=outline, width=ow)

        body_ol = RED if alert else "#241f1a"
        body_ow = 3 if alert else 2
        rpoly(4, 6, w - 4, h - 8, "#24272a")               # sombra

        if t["kind"] == "caminhão":
            cab = h * 0.30
            rpoly(0, -h/2 + cab/2, w, cab, color, body_ol, body_ow)
            rpoly(0, -h/2 + 5, w - 12, cab * 0.35, "#b9c8cd")
            rpoly(0, -h/2 + cab + (h - cab - 8)/2 + 4, w - 4, h - cab - 10,
                  shade(color, 26), "#241f1a", 2)
            for fr in (0.25, 0.55, 0.85):
                rpoly(0, -h/2 + cab + (h - cab) * fr, w - 14, 3, shade(color, -34))
        else:
            rpoly(0, 0, w, h, color, body_ol, body_ow)
            rpoly(0, -h * 0.06, w * 0.64, h * 0.42, shade(color, -34))       # teto
            rpoly(0, -h * 0.155, w * 0.64 - 6, h * 0.15, "#bcd0d4")          # para-brisa
            rpoly(0, h * 0.055, w * 0.64 - 6, h * 0.11, "#8c9ba0")           # vidro traseiro
            rpoly(-(w/2 - 9), -h/2 + 4, 9, 5, "#f4ecc7")                     # faróis
            rpoly( (w/2 - 9), -h/2 + 4, 9, 5, "#f4ecc7")
            lh = 7 if brake else 5
            tl = "#ff5148" if brake else "#b03a31"
            rpoly(-(w/2 - 12), h/2 - 3, 8, lh, tl)                           # lanternas
            rpoly( (w/2 - 12), h/2 - 3, 8, lh, tl)
        if player:                                                           # faixas + espelhos
            rpoly(-4.5, 0, 5, h - 12, "#1c1a17")
            rpoly( 4.5, 0, 5, h - 12, "#1c1a17")
            rpoly(-(w/2 + 2), -h * 0.10, 5, 7, shade(color, -40))
            rpoly( (w/2 + 2), -h * 0.10, 5, 7, shade(color, -40))
        if blink_on and blink_dir:                                           # setas
            s = blink_dir
            for tri in (((s*(w/2+3), -h/2+6), (s*(w/2+3), -h/2+16), (s*(w/2+10), -h/2+11)),
                        ((s*(w/2+3),  h/2-6), (s*(w/2+3),  h/2-16), (s*(w/2+10),  h/2-11))):
                pts = []
                for lx, ly in tri:
                    px, py = roff(lx, ly)
                    pts += [px, py]
                cv.create_polygon(pts, fill="#ffb43a", outline="")

    def draw_player(self):
        g = self.game
        blink_on = g.blink["until"] > g.anim and (g.anim * 5) % 1 < 0.55
        self.draw_vehicle(g.x, PLAYER_Y, PLAYER_TYPE, "#f0a13a", ang=g.tilt,
                          brake=g.brake, player=True,
                          blink_on=blink_on, blink_dir=g.blink["dir"])

    # -- HUD e sobreposições --
    def draw_hud(self):
        g = self.game; cv = self.cv
        cv.create_text(16, 18, anchor="w", text=f"DIST {g.dist/1000:.2f} km",
                       font=F("ui", 10, "bold"), fill="#f0ebdc")
        cv.create_text(W - 16, 18, anchor="e", text=f"NÍVEL {g.level()}",
                       font=F("ui", 10, "bold"), fill="#f0ebdc")
        if g.agent_mode == "llm":
            m, col = (("PILOTO LOCAL · LAYA OFFLINE", RED) if self.agent.state == "offline"
                      else ("PILOTO · LAYA LLM", DIM))
        elif g.agent_mode == "local":
            m, col = "PILOTO LOCAL", DIM
        else:
            m, col = "VOCÊ NO VOLANTE", DIM
        cv.create_text(W / 2, 18, text=m, font=F("ui", 9, "bold"), fill=col)
        sv = str(round(g.v))
        ft = F("title", 30)
        cv.create_text(16, H - 16, anchor="sw", text=sv, font=ft, fill=AMBER)
        cv.create_text(20 + measure(ft, sv), H - 16, anchor="sw", text="KM/H",
                       font=F("ui", 9, "bold"), fill="#a09682")
        if g.cmd and g.t - g.cmd_at < 1.2:
            cv.create_text(W - 16, H - 16, anchor="se", text=g.cmd,
                           font=F("ui", 11, "bold"),
                           fill=CMD_COLORS.get(g.cmd, INK))

    def _veil(self):
        self.cv.create_rectangle(0, 0, W, H, fill="#0c0b09",
                                 stipple="gray50", outline="")

    def _pulse(self, txt, y, size, color, weight="bold"):
        if (self.game.anim % 1.0) < 0.62:
            self.cv.create_text(W / 2, y, text=txt, font=F("ui", size, weight), fill=color)

    def overlay_ready(self):
        cv = self.cv
        self._veil()
        cv.create_text(W/2, 222, text="AUTOPISTA", font=F("title", 42), fill=AMBER)
        cv.create_text(W/2, 256, text="UM LLM DIRIGINDO · SERVIDOR LAYA LOCAL",
                       font=F("ui", 11, "bold"), fill="#ece7db")
        cv.create_text(W/2, 296, text="o agente lê a pista e responde um comando por vez:",
                       font=F("ui", 10), fill=DIM)
        cv.create_text(W/2, 316, text="ACELERAR · FREAR · ESQUERDA · DIREITA · MANTER",
                       font=F("ui", 10, "bold"), fill="#dcd6c6")
        cv.create_text(W/2, 346, text="ponto cego visível: carros AO LADO piscam em vermelho",
                       font=F("ui", 9), fill=AMBER)
        self._pulse("PRESSIONE ESPAÇO PARA INICIAR", 384, 12, "#ece7db")

    def overlay_pause(self):
        self._veil()
        self.cv.create_text(W/2, 292, text="PAUSA", font=F("title", 26), fill="#ece7db")
        self._pulse("ESPAÇO · CONTINUAR", 330, 11, DIM)

    def overlay_over(self):
        g = self.game; cv = self.cv
        self._veil()
        cv.create_rectangle(48, 150, 432, 430, fill="#14110d", outline="#3a352c")
        cv.create_text(W/2, 192, text="COLISÃO", font=F("title", 28), fill=RED)
        cv.create_text(W/2, 220, text=f"com {g.crashed_with}", font=F("ui", 10), fill=DIM)
        cv.create_text(W/2, 244, text=f"causa: {g.crash_type}",
                       font=F("ui", 9, "bold"), fill=AMBER, width=340)
        lat = avg(g.stats["lat"])
        rows = (("DISTÂNCIA", f"{g.dist/1000:.2f} km"),
                ("TEMPO", fmt_time(g.t)),
                ("MUDANÇAS DE COMANDO", str(g.stats["decisions"])),
                ("LATÊNCIA DO LAYA", f"{lat:.0f} ms" if lat is not None else "—"),
                ("FALLBACKS LOCAIS", str(g.stats["fallback"])))
        for i, (k, v) in enumerate(rows):
            y = 282 + i * 22
            cv.create_text(76, y, anchor="w", text=k, font=F("mono", 9), fill=DIM)
            cv.create_text(404, y, anchor="e", text=v, font=F("mono", 9), fill=INK)
        self._pulse("ESPAÇO · CORRER DE NOVO", 408, 11, AMBER)

    def draw(self):
        g = self.game; cv = self.cv
        cv.delete("all")
        self.draw_grass()
        for o in g.sides:
            self.draw_side(o)
        self.draw_road()
        for s in g.skids:
            cv.create_oval(s["x"]-3.4, s["y"]-3.4, s["x"]+3.4, s["y"]+3.4,
                           fill="#23231e", outline="")
        per = g.perceive()
        side_set = {i for i, p in enumerate(per) if p["side"] is not None}
        alert = (g.anim * 3) % 1 < 0.6      # o ponto cego, pulsando em vermelho
        for o in g.obstacles:
            self.draw_vehicle(o["x"], o["y"], o["type"], o["color"],
                              alert=(o["lane"] in side_set and alert))
        self.draw_player()
        for p in g.parts:
            if p["type"] == "smoke":
                cv.create_oval(p["x"]-p["r"], p["y"]-p["r"], p["x"]+p["r"], p["y"]+p["r"],
                               fill="#cec7b8", stipple="gray50", outline="")
            else:
                cv.create_rectangle(p["x"]-p["w"]/2, p["y"]-p["h"]/2,
                                    p["x"]+p["w"]/2, p["y"]+p["h"]/2,
                                    fill=p["color"], outline="")
        self.draw_hud()
        if g.mode == "ready":   self.overlay_ready()
        elif g.mode == "pause": self.overlay_pause()
        elif g.mode == "over":  self.overlay_over()
        if g.shake > 0.3:
            cv.move("all", (random.random()-0.5)*g.shake, (random.random()-0.5)*g.shake)
            g.shake *= 0.90
        else:
            g.shake = 0.0
        if SC != 1.0:
            cv.scale("all", 0, 0, SC, SC)

# ─────────────────────────── painel lateral ───────────────────────────

class Panel:
    PANEL_W = 350

    def __init__(self, parent, game, agent, on_pause, on_restart, on_mode):
        self.game, self.agent = game, agent
        self.last = 0.0
        fr = tk.Frame(parent, bg=PANEL, width=self.PANEL_W,
                      highlightthickness=1, highlightbackground=LINE)
        fr.pack(side="right", fill="y", padx=(0, 18), pady=18)
        fr.pack_propagate(False)
        self.body = tk.Frame(fr, bg=PANEL)
        self.body.pack(fill="both", expand=True)

        btnkw = dict(bg=PANEL2, fg=INK, activebackground="#2a251d",
                     activeforeground=AMBER, relief="flat", bd=0, padx=10, pady=7,
                     font=F("ui", 9, "bold"), takefocus=0, cursor="hand2",
                     highlightthickness=0)

        # cabeçalho
        head = tk.Frame(self.body, bg=PANEL, padx=16, pady= 8)
        head.pack(fill="x")
        tk.Label(head, text="AUTOPISTA", bg=PANEL, fg=INK,
                 font=F("title", 17)).pack(anchor="w")
        self.l_run = tk.Label(head, text="0.00 km · 00:00 · NV 1", bg=PANEL,
                              fg=DIM, font=F("mono", 10), anchor="w")
        self.l_run.pack(anchor="w", pady=(6, 0))

        # conexão
        conn = self._sec("CONEXÃO · LAYA")
        row = tk.Frame(conn, bg=PANEL); row.pack(anchor="w", fill="x", pady=(6, 0))
        self.dot = tk.Label(row, text="●", bg=PANEL, fg=AMBER, font=F("ui", 11))
        self.dot.pack(side="left")
        self.l_conn = tk.Label(row, text="aguardando a primeira decisão…", bg=PANEL,
                               fg=INK, font=F("ui", 9), anchor="w",
                               justify="left", wraplength=240)
        self.l_conn.pack(side="left", padx=(8, 0))
        self.l_meta = tk.Label(conn, text="— ms · 0 ações · 0 falhas · 0 fallbacks",
                               bg=PANEL, fg=FAINT, font=F("mono", 8), anchor="w")
        self.l_meta.pack(anchor="w", pady=(6, 0))
        mrow = tk.Frame(conn, bg=PANEL); mrow.pack(anchor="w", fill="x", pady=(8, 0))
        tk.Label(mrow, text="MODELO", bg=PANEL, fg=DIM,
                 font=F("ui", 7, "bold")).pack(side="left")
        tk.Entry(mrow, textvariable=agent.model_var, bg=PANEL2, fg=INK,
                 insertbackground=INK, relief="flat", font=F("mono", 9),
                 highlightthickness=1, highlightbackground=LINE,
                 highlightcolor=AMBER).pack(side="left", fill="x", expand=True, padx=(8, 0))

        # comando atual
        cmds = self._sec("COMANDO ATUAL")
        self.l_cmd = tk.Label(cmds, text="—", bg=PANEL, fg=DIM,
                              font=F("title", 20), anchor="w")
        self.l_cmd.pack(anchor="w", pady=(6, 0))
        self.l_cmdm = tk.Label(cmds, text="aguardando o piloto", bg=PANEL,
                               fg=FAINT, font=F("mono", 9), anchor="w")
        self.l_cmdm.pack(anchor="w")

        # percepção
        perc = self._sec("PERCEPÇÃO · METROS À FRENTE")
        self.perc_cv = tk.Canvas(perc, width=316, height=84, bg=PANEL, highlightthickness=0)
        self.perc_cv.pack(anchor="w", pady=(6, 0))

        # diário
        logsec = self._sec("DIÁRIO DE DECISÕES", expand=True)
        self.log = tk.Text(logsec, height=6, bg="#191713", fg=DIM, relief="flat",
                           font=F("mono", 9), state="disabled", wrap="none",
                           padx=6, pady=4, selectbackground=PANEL2, cursor="arrow")
        self.log.pack(fill="both", expand=True, pady=(6, 0))
        for cmd, c in CMD_COLORS.items():
            self.log.tag_configure(cmd, foreground=c)
        self.log.tag_configure("t", foreground=DIM)
        self.log.tag_configure("ms", foreground=FAINT)
        self.log.tag_configure("note", foreground=FAINT)

        # distribuição
        hist = self._sec("DISTRIBUIÇÃO DE COMANDOS")
        self.hist_cv = tk.Canvas(hist, width=316, height=96, bg=PANEL, highlightthickness=0)
        self.hist_cv.pack(anchor="w", pady=(6, 0))

        # controles
        ctrl = self._sec("CONTROLES")
        brow = tk.Frame(ctrl, bg=PANEL); brow.pack(fill="x", pady=(8, 6))
        self.btn_pause = tk.Button(brow, text="pausar", command=on_pause, **btnkw)
        self.btn_pause.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Button(brow, text="reiniciar", command=on_restart, **btnkw
                  ).pack(side="left", fill="x", expand=True)
        mrow2 = tk.Frame(ctrl, bg=PANEL); mrow2.pack(fill="x", pady=(0, 8))
        self.mode_btns = {}
        for m, lab in (("llm", "LAYA"), ("local", "LOCAL"), ("manual", "MANUAL")):
            b = tk.Button(mrow2, text=lab, command=lambda m=m: on_mode(m), **btnkw)
            b.pack(side="left", fill="x", expand=True,
                   padx=(0, 6) if m != "manual" else (0, 0))
            self.mode_btns[m] = b
        for txt in ("ESPAÇO inicia/pausa · R reinicia · 1/2/3 troca o piloto",
                    "setas dirigem no modo MANUAL · carros AO LADO piscam em vermelho"):
            tk.Label(ctrl, text=txt, bg=PANEL, fg=FAINT, font=F("ui", 8),
                     anchor="w", justify="left", wraplength=300).pack(anchor="w")

    def _sec(self, title, expand=False):
        fr = tk.Frame(self.body, bg=PANEL, highlightthickness=1,
                      highlightbackground=LINE)
        fr.pack(fill="both" if expand else "x", expand=expand)
        inner = tk.Frame(fr, bg=PANEL, padx=14, pady=10)
        inner.pack(fill="both", expand=True)
        tk.Label(inner, text=title, bg=PANEL, fg=DIM,
                 font=F("ui", 8, "bold"), anchor="w").pack(fill="x")
        return inner

    def refresh_mode(self):
        for m, b in self.mode_btns.items():
            if m == self.game.agent_mode:
                b.config(bg="#2a2117", fg=AMBER)
            else:
                b.config(bg=PANEL2, fg=DIM)

    def set_pause(self, running):
        self.btn_pause.config(text="pausar" if running else "continuar")

    def refresh_log(self):
        g = self.game; t = self.log
        t.config(state="normal"); t.delete("1.0", "end")
        if not g.logs:
            t.insert("end", "— sem decisões ainda —\n", "note")
        else:
            for e in g.logs[:8]:
                t.insert("end", f"{e['cmd']:<9}", e["cmd"])
                t.insert("end", f"{e['t']:6.1f}s", "t")
                src = f"{e['ms']:5.0f}ms" if e["ms"] is not None else f"{e['src']:>7}"
                t.insert("end", f"  {src}", "ms")
                if e["note"]:
                    t.insert("end", f"  {e['note'][:46]}", "note")
                t.insert("end", "\n")
        t.config(state="disabled")

    def refresh_hist(self):
        c = self.hist_cv; g = self.game
        c.delete("all")
        tot = max(1, sum(g.stats["counts"].values()))
        for i, cmd in enumerate(CMD_LIST):
            n = g.stats["counts"].get(cmd, 0)
            y = 8 + i * 18
            c.create_text(2, y + 6, anchor="w", text=cmd, font=F("ui", 8), fill=DIM)
            c.create_rectangle(80, y + 1, 278, y + 9, fill="#262219", outline="")
            if n:
                c.create_rectangle(80, y + 1, 80 + 198 * n / tot, y + 9,
                                   fill=AMBER, outline="")
            c.create_text(312, y + 5, anchor="e", text=str(n),
                          font=F("mono", 9), fill=DIM)

    def draw_perception(self):
        c = self.perc_cv; g = self.game
        c.delete("all")
        per = g.perceive()
        for i, p in enumerate(per):
            y = 13 + i * 26
            cur = (i == g.target_lane)
            c.create_text(2, y, anchor="w",
                          text=NOMES[i] + (" ●" if cur else ""),
                          font=F("ui", 8), fill=AMBER if cur else DIM)
            c.create_rectangle(86, y - 5, 248, y + 5, fill="#262219", outline="")
            if p["side"] is not None:
                c.create_rectangle(86, y - 5, 248, y + 5, fill=RED, outline="")
                lab, col = "AO LADO", RED
            elif p["ahead"] is None:
                lab, col = "livre", GREEN
            else:
                d = p["ahead"]
                col = RED if d < 22 else (AMBER if d < 45 else GREEN)
                frac = clamp(1 - d / 95.0, 0.0, 1.0)
                c.create_rectangle(86, y - 5, 86 + 162 * frac, y + 5,
                                   fill=col, outline="")
                lab, col = f"{d:.0f} m", DIM
            c.create_text(254, y, anchor="w", text=lab,
                          font=F("mono", 8), fill=col)

    def update(self, now):
        if now - self.last < 0.12:
            return
        self.last = now
        g, a = self.game, self.agent
        self.l_run.config(text=f"{g.dist/1000:.2f} km · {fmt_time(g.t)} · NV {g.level()}")
        if g.agent_mode == "llm":
            self.dot.config(fg={"online": GREEN, "offline": RED, "standby": AMBER}[a.state])
            self.l_conn.config(text={
                "online": "localhost:8000 · respondendo",
                "offline": "sem resposta — piloto local no volante",
                "standby": "aguardando a primeira decisão…"}[a.state])
        elif g.agent_mode == "local":
            self.dot.config(fg=AMBER)
            self.l_conn.config(text="piloto local (sem rede)")
        else:
            self.dot.config(fg=AMBER)
            self.l_conn.config(text="você no volante")
        lat = avg(g.stats["lat"])
        self.l_meta.config(text=f"{('—' if lat is None else f'{lat:.0f} ms')}"
                              f" · {g.stats['decisions']} ações"
                              f" · {g.stats['fails']} falhas"
                              f" · {g.stats['fallback']} fallbacks")
        if g.cmd:
            self.l_cmd.config(text=g.cmd, fg=CMD_COLORS.get(g.cmd, INK))
            age = max(0, int((g.t - g.cmd_at) * 1000))
            self.l_cmdm.config(text=f"há {age} ms · via {g.cmd_src}")
        else:
            self.l_cmd.config(text="—", fg=DIM)
            self.l_cmdm.config(text="aguardando o piloto")
        self.draw_perception()
        if g.log_dirty:
            g.log_dirty = False
            self.refresh_log()
        if g.hist_dirty:
            g.hist_dirty = False
            self.refresh_hist()

# ─────────────────────────── controle ───────────────────────────

class Ctrl:
    def __init__(self, game, agent):
        self.game, self.agent = game, agent
        self.panel = None

    def start(self):
        self.game.start_run()
        self.agent.reset()
        if self.panel:
            self.panel.set_pause(True)

    def on_space(self):
        g = self.game
        if g.mode in ("ready", "over"):
            self.start()
        elif g.mode == "run":
            g.mode = "pause"; self.panel.set_pause(False)
        elif g.mode == "pause":
            g.mode = "run"; self.panel.set_pause(True)

    def set_mode(self, m):
        g = self.game
        g.agent_mode = m
        g.keys_up = g.keys_dn = False
        if m == "llm":
            self.agent.reset()
        if m == "manual":
            g.cmd = None; g.cmd_at = -1e9; g.cmd_src = None
        if self.panel:
            self.panel.refresh_mode()

    def steer(self, d):
        g = self.game
        nl = clamp(g.target_lane + d, 0, 2)
        if nl != g.target_lane:
            g.from_lane = g.target_lane
            g.target_lane = nl
            g.blink = {"dir": d, "until": g.anim + 0.8}

# ─────────────────────────── main ───────────────────────────

def main():
    global SC, FONTS
    ap = argparse.ArgumentParser(description="AUTOPISTA — tkinter + agente Laya")
    ap.add_argument("--url", default=LAYA_URL)
    ap.add_argument("--model", default="systemone")
    ap.add_argument("--zoom", type=float, default=1.25, help="escala da tela (1.0–1.5)")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    SC = clamp(args.zoom, 0.75, 2.0)

    root = tk.Tk()
    root.title("AUTOPISTA — um LLM no volante")
    root.configure(bg=BG)
    fams = set(tkfont.families())

    def pick(cands, fb):
        for c in cands:
            if c in fams:
                return c
        return fb

    FONTS = {
        "ui":    pick(["Segoe UI", "Helvetica Neue", "Ubuntu", "Noto Sans",
                       "Arial", "DejaVu Sans"], "Helvetica"),
        "mono":  pick(["Consolas", "Menlo", "DejaVu Sans Mono",
                       "Liberation Mono", "Courier New"], "Courier New"),
        "title": pick(["Bungee", "Arial Black", "Segoe UI Black", "Impact"],
                      "Helvetica"),
    }

    game = Game(seed=args.seed)
    agent = LayaAgent(args.url, tk.StringVar(value=args.model))
    ctrl = Ctrl(game, agent)

    wrap = tk.Frame(root, bg=BG)
    wrap.pack(fill="both", expand=True)
    left = tk.Frame(wrap, bg="#0c0b09", highlightthickness=1,
                    highlightbackground=LINE)
    left.pack(side="left", fill="both", expand=True, padx=(18, 0), pady=18)
    cv = tk.Canvas(left, width=int(W * SC), height=int(H * SC),
                   bg="#4d5936", highlightthickness=0)
    cv.pack(expand=True)
    view = View(cv, game, agent)
    panel = Panel(wrap, game, agent, ctrl.on_space, ctrl.start, ctrl.set_mode)
    ctrl.panel = panel
    panel.refresh_mode()
    panel.set_pause(False)
    root.minsize(int(W * SC + Panel.PANEL_W + 58), int(H * SC + 40))

    # teclado
    def _focus_entry():
        return isinstance(root.focus_get(), tk.Entry)

    root.bind("<space>", lambda e: (None if _focus_entry() else ctrl.on_space(), "break")[1])
    root.bind("<Key-r>", lambda e: (None if _focus_entry() else ctrl.start(), "break")[1])
    root.bind("<Key-R>", lambda e: (None if _focus_entry() else ctrl.start(), "break")[1])
    for i, m in enumerate(("llm", "local", "manual"), 1):
        root.bind(f"<Key-{i}>",
                  lambda e, m=m: (None if _focus_entry() else ctrl.set_mode(m), "break")[1])

    held = {"Left": False, "Right": False}

    def _press(d):
        def h(e):
            if not held[d]:
                held[d] = True
                if game.agent_mode == "manual" and game.mode == "run":
                    ctrl.steer(1 if d == "Right" else -1)
            return "break"
        return h

    def _rel(d):
        def h(e):
            held[d] = False
            return "break"
        return h

    root.bind("<Left>", _press("Left"));   root.bind("<KeyRelease-Left>", _rel("Left"))
    root.bind("<Right>", _press("Right")); root.bind("<KeyRelease-Right>", _rel("Right"))
    root.bind("<Up>", lambda e: (setattr(game, "keys_up", True), "break")[1])
    root.bind("<KeyRelease-Up>", lambda e: (setattr(game, "keys_up", False), "break")[1])
    root.bind("<Down>", lambda e: (setattr(game, "keys_dn", True), "break")[1])
    root.bind("<KeyRelease-Down>", lambda e: (setattr(game, "keys_dn", False), "break")[1])
    cv.bind("<Button-1>", lambda e: ctrl.on_space() if game.mode != "run" else None)

    # laço principal
    state = {"last": time.perf_counter()}

    def loop():
        now = time.perf_counter()
        dt = min(0.033, now - state["last"])
        state["last"] = now
        g = game
        g.anim += dt
        if g.mode == "ready":
            sc = 46.0 * dt
            g.world_px += sc
            g.update_sides(sc)
        elif g.mode == "run":
            drive(g, agent)
            g.update(dt)
        elif g.mode == "over":
            g.update_over(dt)
        view.draw()
        panel.update(now)
        root.after(12, loop)

    root.after(12, loop)
    root.mainloop()

if __name__ == "__main__":
    main()