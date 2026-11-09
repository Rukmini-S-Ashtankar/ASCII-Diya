from __future__ import annotations
import argparse
import atexit
import math
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

try:  # optional: only used as a Windows ANSI compatibility fallback
    import colorama
except Exception:  # pragma: no cover
    colorama = None

TAU = 2.0 * math.pi


# =============================================================================
# 1. CONFIGURATION  (everything here can also be set from the command line)
# =============================================================================
@dataclass
class Config:
    fps: float = 30.0          # target frames per second
    speed: float = 1.0         # global animation speed (also speeds up the intro)
    intro: float = 1.0         # multiplies the length of the intro stages (1-4)
    density: float = 1.0       # particle density multiplier
    glow: float = 1.0          # strength of the surrounding ambient glow
    accent: float = 1.0        # amount of cyan / electric-blue accents (0 = none)
    cols: int = 0              # canvas width  in characters (0 = whole terminal)
    rows: int = 0              # canvas height in lines      (0 = whole terminal)
    color_mode: str = "auto"   # auto | truecolor | 256
    seed: Optional[int] = None  # fixed random seed for a repeatable animation
    duration: float = 0.0      # stop automatically after N seconds (0 = never)


# Colour ramps: (heat 0..1, (r, g, b)).  Edit these to re-colour the whole piece.
FIRE_STOPS = [
    (0.00, (24, 2, 2)),
    (0.14, (92, 6, 8)),         # deep red
    (0.30, (160, 22, 10)),      # red
    (0.46, (214, 62, 8)),       # dark orange
    (0.62, (255, 112, 10)),     # orange
    (0.76, (255, 168, 24)),     # golden orange
    (0.88, (255, 218, 70)),     # golden yellow
    (1.00, (255, 248, 214)),    # warm white (flame core)
]
ACCENT_STOPS = [
    (0.00, (0, 24, 80)),
    (0.40, (0, 96, 235)),       # electric blue
    (0.75, (0, 215, 255)),      # cyan
    (1.00, (200, 255, 255)),
]
FIRE_STEPS = 48                 # number of distinct fire colours
ACCENT_STEPS = 20               # number of distinct accent colours

# Glyphs.  Index into GLYPHS is what the frame buffers store (0 = blank).
GLYPHS = " .,:;!|/\\-_=+*xX#%@"
LEVEL_GROUPS = [".,", ":;", "+*", "xX#", "%@"]       # very low .. maximum intensity
LEVEL_EDGES = np.array([0.07, 0.20, 0.36, 0.58, 0.80])  # heat thresholds between groups


# =============================================================================
# 2. SMALL HELPERS
# =============================================================================
def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def flow(a, b, t):
    """Cheap smooth pseudo-noise in [-1, 1] built from travelling sine waves."""
    return (np.sin(a * 1.7 + b * 1.1 + t * 1.9)
            + np.sin(a * 3.3 - b * 2.1 - t * 2.7 + 1.3)
            + np.sin(a * 5.9 + b * 3.7 + t * 3.9 + 2.1)) * (1.0 / 3.0)


def build_ramp(stops, steps):
    pos = np.array([s[0] for s in stops], dtype=np.float64)
    rgb = np.array([s[1] for s in stops], dtype=np.float64)
    xs = np.linspace(0.0, 1.0, steps)
    cols = [np.interp(xs, pos, rgb[:, c]) for c in range(3)]
    return np.stack(cols, axis=1).round().astype(np.uint8)


def rgb_to_xterm256(r: int, g: int, b: int) -> int:
    """Nearest xterm-256 colour (6x6x6 cube or greyscale ramp)."""
    levels = (0, 95, 135, 175, 215, 255)

    def nearest(v):
        return min(range(6), key=lambda i: abs(levels[i] - v))

    ri, gi_, bi = nearest(r), nearest(g), nearest(b)
    cube = (levels[ri], levels[gi_], levels[bi])
    cube_idx = 16 + 36 * ri + 6 * gi_ + bi
    gray_i = min(23, max(0, int(round((((r + g + b) / 3.0) - 8) / 10.0))))
    gv = 8 + 10 * gray_i
    d_cube = sum((a - c) ** 2 for a, c in zip((r, g, b), cube))
    d_gray = sum((a - gv) ** 2 for a in (r, g, b))
    return 232 + gray_i if d_gray < d_cube else cube_idx


FIRE_RAMP = build_ramp(FIRE_STOPS, FIRE_STEPS)
ACCENT_RAMP = build_ramp(ACCENT_STOPS, ACCENT_STEPS)
COLOR_TABLE = np.vstack([np.zeros((1, 3), np.uint8), FIRE_RAMP, ACCENT_RAMP])
ACCENT_BASE = 1 + FIRE_STEPS            # first accent index in COLOR_TABLE


def _glyph(c: str) -> int:
    return GLYPHS.index(c)


GROUP_IDX = np.array(
    [[_glyph(g[i % len(g)]) for i in range(4)] for g in LEVEL_GROUPS], dtype=np.int32
)  # shape (5 intensity levels, 4 interchangeable variants)


def shade(h, k, accent):
    """Heat (float), variant (int 0-3) and accent flag arrays -> (glyph, colour) index arrays."""
    lvl = np.searchsorted(LEVEL_EDGES, h, side="right")          # 0 = blank, 1..5
    glyph = np.where(lvl > 0, GROUP_IDX[np.maximum(lvl - 1, 0), k], 0)
    hc = np.clip(h, 0.0, 1.0)
    fire = 1 + (hc * (FIRE_STEPS - 1) + 0.5).astype(np.int32)
    acc = ACCENT_BASE + (np.clip(h * 1.15, 0.0, 1.0) * (ACCENT_STEPS - 1) + 0.5).astype(np.int32)
    col = np.where(accent, acc, fire)
    return glyph.astype(np.uint8), col.astype(np.uint8)


# =============================================================================
# 3. TERMINAL RENDERER
# =============================================================================
def _enable_windows_vt() -> bool:
    """Turn on ANSI escape processing on Windows consoles."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            if kernel32.SetConsoleMode(handle, mode.value | 0x0001 | 0x0004):
                return True
    except Exception:
        pass
    if colorama is not None:
        try:
            colorama.just_fix_windows_console()
        except AttributeError:
            colorama.init()
        return True
    os.system("")  # classic trick that flips VT processing on in Windows 10+
    return True


class TerminalRenderer:
    """Owns the terminal: setup/teardown, colour output and diff-based screen updates."""

    MIN_COLS, MIN_ROWS = 30, 12

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.truecolor = self._detect_truecolor(cfg.color_mode)
        self._sgr = self._build_sgr()
        self.size = (0, 0)            # terminal size (cols, rows)
        self.W = self.H = 0           # canvas size
        self.off_x = self.off_y = 0   # canvas offset inside the terminal
        self._prev_ch = None
        self._prev_col = None
        self._opened = False
        self._old_tty = None
        self._tty_fd = None
        self._msvcrt = None

    # ---- capability detection / palette --------------------------------
    @staticmethod
    def _detect_truecolor(mode: str) -> bool:
        if mode == "truecolor":
            return True
        if mode == "256":
            return False
        env = os.environ
        if env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
            return True
        if env.get("WT_SESSION") or env.get("WT_PROFILE_ID"):        # Windows Terminal
            return True
        if env.get("TERM_PROGRAM") in ("vscode", "iTerm.app", "WezTerm", "Hyper", "ghostty"):
            return True
        term = env.get("TERM", "").lower()
        return any(s in term for s in ("truecolor", "24bit", "direct", "kitty", "alacritty"))

    def _build_sgr(self):
        out = [""]
        for r, g, b in COLOR_TABLE[1:].tolist():
            if self.truecolor:
                out.append("\x1b[38;2;%d;%d;%dm" % (r, g, b))
            else:
                out.append("\x1b[38;5;%dm" % rgb_to_xterm256(r, g, b))
        return out

    # ---- lifecycle -----------------------------------------------------
    def open(self):
        if self._opened:
            return
        self._opened = True
        _enable_windows_vt()
        self._open_input()
        bg = "\x1b[48;2;0;0;0m" if self.truecolor else "\x1b[48;5;16m"
        # alternate screen, hide cursor, no auto-wrap, pure black background, clear
        self._write("\x1b[?1049h\x1b[?25l\x1b[?7l" + bg + "\x1b[2J\x1b[H")

    def close(self):
        if not self._opened:
            return
        self._opened = False
        try:
            self.out_write("\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l")
        finally:
            self._close_input()

    def out_write(self, s: str):
        try:
            sys.stdout.write(s)
            sys.stdout.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _write(self, s: str):
        try:
            sys.stdout.write(s)
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            raise SystemExit(0)

    # ---- keyboard (only used for the optional 'q' to quit) ------------
    def _open_input(self):
        try:
            if os.name == "nt":
                import msvcrt

                self._msvcrt = msvcrt
                return
            import termios

            fd = sys.stdin.fileno()
            if not os.isatty(fd):
                return
            self._old_tty = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[3] &= ~(termios.ECHO | termios.ICANON)   # keep ISIG so Ctrl+C still works
            new[6][termios.VMIN] = 0
            new[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
            self._tty_fd = fd
        except Exception:
            self._old_tty = None
            self._tty_fd = None

    def _close_input(self):
        if self._old_tty is not None and self._tty_fd is not None:
            try:
                import termios

                termios.tcsetattr(self._tty_fd, termios.TCSAFLUSH, self._old_tty)
            except Exception:
                pass
        self._old_tty = None
        self._tty_fd = None

    def poll_quit(self) -> bool:
        try:
            if self._msvcrt is not None:
                quit_ = False
                while self._msvcrt.kbhit():
                    if self._msvcrt.getwch() in ("q", "Q"):
                        quit_ = True
                return quit_
            if self._tty_fd is not None:
                import select

                r, _, _ = select.select([self._tty_fd], [], [], 0)
                if r:
                    data = os.read(self._tty_fd, 256)
                    return b"q" in data.lower()
        except Exception:
            pass
        return False

    # ---- geometry ------------------------------------------------------
    @staticmethod
    def terminal_size() -> Tuple[int, int]:
        try:
            s = os.get_terminal_size(sys.stdout.fileno())
        except (OSError, ValueError):
            s = shutil.get_terminal_size((100, 30))
        return s.columns, s.lines

    def too_small(self, cols: int, rows: int) -> bool:
        return cols < self.MIN_COLS or rows < self.MIN_ROWS

    def set_size(self, cols: int, rows: int) -> Tuple[int, int]:
        """Adopt a new terminal size, clear the screen, return the canvas size (W, H)."""
        self.size = (cols, rows)
        W = min(cols, self.cfg.cols) if self.cfg.cols > 0 else cols
        H = min(rows, self.cfg.rows) if self.cfg.rows > 0 else rows
        W, H = max(W, 1), max(H, 1)
        self.W, self.H = W, H
        self.off_x = (cols - W) // 2
        self.off_y = (rows - H) // 2
        self._prev_ch = np.zeros((H, W), np.uint8)
        self._prev_col = np.zeros((H, W), np.uint8)
        self._write("\x1b[2J")
        return W, H

    def show_message(self, text: str):
        self._write("\x1b[2J\x1b[H\x1b[0m" + text[: max(1, self.size[0] - 1)])

    # ---- drawing -------------------------------------------------------
    def present(self, ch: np.ndarray, col: np.ndarray):
        """Send only the cells that changed since the previous frame."""
        prev_ch, prev_col = self._prev_ch, self._prev_col
        changed = (ch != prev_ch) | ((ch != 0) & (col != prev_col))
        if self.off_x + self.W >= self.size[0] and self.off_y + self.H >= self.size[1]:
            changed[-1, -1] = False          # never touch the bottom-right cell
        rows_i, cols_i = np.nonzero(changed)
        if rows_i.size == 0:
            return
        gl = ch[rows_i, cols_i].tolist()
        co = col[rows_i, cols_i].tolist()
        ox, oy = self.off_x + 1, self.off_y + 1
        sgr = self._sgr
        glyphs = GLYPHS
        parts = []
        append = parts.append
        last_r = last_c = -5
        cur = -1
        for r, c, g, k in zip(rows_i.tolist(), cols_i.tolist(), gl, co):
            if r != last_r or c != last_c + 1:
                append("\x1b[%d;%dH" % (r + oy, c + ox))
            if g and k != cur:
                append(sgr[k])
                cur = k
            append(glyphs[g])
            last_r, last_c = r, c
        self._write("".join(parts))
        np.copyto(prev_ch, ch)
        np.copyto(prev_col, col)


# =============================================================================
# 4. DIYA GEOMETRY
# =============================================================================
class DiyaShape:
    """
    Static geometry of the lamp, evaluated on the character grid.

    Coordinates are "normalised": X right, Y up, 1.0 = half the canvas height, and
    one terminal column counts as half a row so shapes keep their true proportions.
    """

    Y_RIM = -0.20        # height of the rim ellipse centre
    R = 0.78             # half width of the bowl
    D = 0.46             # depth of the bowl below the rim
    P = 2.10             # super-ellipse power of the bowl profile
    RIM_B = 0.10         # half height of the rim ellipse (seen slightly from above)
    FOOT_TOP = -0.64
    FOOT_BOT = -0.77
    Y_WICK = Y_RIM + 0.02

    def __init__(self, W: int, H: int, rng: np.random.Generator):
        self.W, self.H = W, H
        self.unit = max(4.0, min(H / 2.0, W / 4.0)) * 0.97
        self.cx = (W - 1) / 2.0
        self.cy = (H - 1) / 2.0
        xs = (np.arange(W) - self.cx) * 0.5 / self.unit
        ys = (self.cy - np.arange(H)) / self.unit
        self.X, self.Y = np.meshgrid(xs, ys)

        # per-cell random constants (visibility, glyph variants, twinkle phases ...)
        self.r1 = rng.random((H, W))
        self.r2 = rng.random((H, W))
        self.r3 = rng.random((H, W))
        self.phase = rng.uniform(0.0, TAU, (H, W))
        self.rate = rng.uniform(1.5, 7.0, (H, W))       # glyph substitution speed
        self.rate2 = rng.uniform(0.7, 3.2, (H, W))      # twinkle speed

        inside, heat, oil, _ = self.evaluate(self.X, self.Y)
        self.bowl_in, self.oil = inside, oil
        self._build_edges(inside)
        # outline cells are drawn dark red / dark orange, the rest keeps its lighting
        self.bowl_heat = np.where(self.edge, 0.24 + 0.36 * heat, heat) * inside

        # soft radial illumination around the flame
        self.glow = np.exp(-((self.X / 0.80) ** 2 + ((self.Y - 0.10) / 0.85) ** 2))
        # how late each cell is revealed while the diya forms (centre first, ragged edge)
        dist = np.hypot(self.X * 0.85, self.Y + 0.15) / 1.05
        self.rv = 0.65 * np.clip(dist, 0.0, 1.3) + 0.35 * self.r1

    # ---- analytic description of the lamp ------------------------------
    def evaluate(self, X, Y):
        """Return (inside, static heat, oil mask, sampling weight) for arbitrary coordinates."""
        R, yr = self.R, self.Y_RIM
        ax = np.abs(X)
        below = Y <= yr
        g = (ax / R) ** self.P + (np.maximum(yr - Y, 0.0) / self.D) ** 2
        body = below & (g <= 1.0)
        ell = (ax / (0.98 * R)) ** 1.8 + ((Y - yr) / self.RIM_B) ** 2
        rim = ell <= 1.0
        foot_t = np.clip((self.FOOT_TOP - Y) / (self.FOOT_TOP - self.FOOT_BOT), 0.0, 1.0)
        foot_hw = 0.22 + 0.15 * foot_t ** 1.6
        foot = (Y <= self.FOOT_TOP) & (Y >= self.FOOT_BOT) & (ax <= foot_hw)
        inside = body | rim | foot

        # lighting of the bowl body: brighter near the rim, highlights on the shoulders
        ybot = yr - self.D
        v = np.clip((Y - ybot) / self.D, 0.0, 1.0)
        heat = 0.40 + 0.34 * v ** 1.2 + 0.12 * v * np.exp(-(X / 0.5) ** 2)
        hl1 = np.exp(-(((X + 0.42 * R) / 0.22) ** 2 + ((Y - (yr - 0.14)) / 0.06) ** 2))
        hl2 = np.exp(-(((X - 0.50 * R) / 0.15) ** 2 + ((Y - (yr - 0.20)) / 0.045) ** 2))
        heat = heat + 0.40 * hl1 + 0.18 * hl2
        # decorative band: a row of glowing pearls and a darker line above it
        yb = yr - 0.21
        pearl = (np.abs(Y - yb) < 0.026) & (np.abs((X / 0.105) % 1.0 - 0.5) < 0.21) & (g < 0.72)
        heat = np.where(pearl, 0.92, heat)
        line = (np.abs(Y - (yb + 0.075)) < 0.011) & (g < 0.85)
        heat = np.where(line, heat * 0.55, heat)
        # foot
        foot_h = 0.30 + 0.28 * (1.0 - foot_t)
        heat = np.where(foot & ~body, foot_h, heat)
        # rim ellipse: bright front lip, softer back lip, glowing oil inside
        ring = rim & (ell > 0.60)
        heat = np.where(ring, np.where(below, 0.90, 0.55), heat)
        oil = rim & (ell <= 0.60)
        heat = np.where(oil, 0.80 + 0.18 * np.exp(-(X / 0.2) ** 2), heat)

        edge_b = np.exp(-((g - 0.93) / 0.14) ** 2) * below
        edge_e = np.exp(-((ell - 0.85) / 0.20) ** 2)
        weight = inside * (0.30 + 0.70 * np.maximum(np.maximum(edge_b, edge_e), foot * 0.6))
        return inside, heat * inside, oil, weight

    def _build_edges(self, inside):
        """Outline cells and the glyph ( / \\ | - _ ) that follows the outline direction."""
        pad = np.pad(inside, 1)
        eroded = inside & pad[:-2, 1:-1] & pad[2:, 1:-1] & pad[1:-1, :-2] & pad[1:-1, 2:]
        self.edge = inside & ~eroded

        def blur(a):
            p = np.pad(a, 1)
            return (4 * a + 2 * (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:])
                    + (p[:-2, :-2] + p[:-2, 2:] + p[2:, :-2] + p[2:, 2:])) / 16.0

        s = blur(blur(inside.astype(np.float64)))
        gy, gx = np.gradient(s)                    # d/drow, d/dcol
        th = np.arctan2(2.0 * gx, gy) % math.pi    # direction of the outline in screen space
        glyph = np.full(inside.shape, _glyph("|"), np.uint8)
        top = gy > 0                                # inside lies below -> upper outline
        horiz = (th < math.pi / 8) | (th >= 7 * math.pi / 8)
        slash = (th >= math.pi / 8) & (th < 3 * math.pi / 8)
        back = (th >= 5 * math.pi / 8) & (th < 7 * math.pi / 8)
        glyph[slash] = _glyph("/")
        glyph[back] = _glyph("\\")
        glyph[horiz & top] = _glyph("-")
        glyph[horiz & ~top] = _glyph("_")
        self.edge_glyph = glyph
        self.edge_sel = self.edge & (self.r3 < 0.88)    # a few gaps keep it organic

    # ---- sampling / mapping --------------------------------------------
    def to_cell(self, X, Y):
        c = np.rint(self.cx + 2.0 * self.unit * X).astype(np.int64)
        r = np.rint(self.cy - self.unit * Y).astype(np.int64)
        return r, c

    def sample_bowl(self, n: int, rng: np.random.Generator):
        """n home positions on the lamp (outline weighted) plus their static heat."""
        xs, ys, hs = [], [], []
        got = 0
        while got < n:
            m = (n - got) * 4 + 64
            X = rng.uniform(-self.R * 1.02, self.R * 1.02, m)
            Y = rng.uniform(self.FOOT_BOT, self.Y_RIM + self.RIM_B, m)
            _, heat, _, w = self.evaluate(X, Y)
            keep = rng.random(m) < w
            xs.append(X[keep])
            ys.append(Y[keep])
            hs.append(heat[keep])
            got += int(keep.sum())
        return np.concatenate(xs)[:n], np.concatenate(ys)[:n], np.concatenate(hs)[:n]


# =============================================================================
# 5. FLAME
# =============================================================================
class FireSimulation:
    """Procedural, continuously changing flame (shape, sway, flicker, heat)."""

    H0 = 0.98            # full flame height (normalised units)
    W0 = 0.20           # full flame half width

    def __init__(self, shape: DiyaShape):
        self.shape = shape
        self.y0 = shape.Y_WICK
        self.burst = 0.0          # set by the controller for "glowing bursts"

    def height(self, t, grow):
        f = (1.0 + 0.055 * math.sin(TAU * 1.9 * t + 0.5)
             + 0.040 * math.sin(TAU * 3.7 * t + 1.7)
             + 0.025 * math.sin(TAU * 7.3 * t + 0.2))
        return self.H0 * grow * f * (1.0 + 0.10 * self.burst)

    def sway(self, s, t, grow):
        sc = np.clip(s, 0.0, 1.0)
        return grow * sc ** 1.5 * (0.050 * np.sin(TAU * 0.55 * t + 2.4 * sc)
                                   + 0.032 * np.sin(TAU * 1.35 * t + 4.3 * sc + 1.0)
                                   + 0.016 * np.sin(TAU * 3.10 * t + 7.0 * sc + 2.0))

    def half_width(self, s, t, grow):
        sc = np.clip(s, 0.0, 1.0)
        body = np.maximum(np.sin(math.pi * sc ** 0.62), 0.0) ** 0.9
        wob = 1.0 + 0.14 * flow(sc * 3.0, 0.0, t * 2.3)
        return np.maximum(self.W0 * grow ** 0.8 * body * wob, 0.010)

    @staticmethod
    def profile(u, s):
        """Flame heat for a point at |u| half-widths from the centre line, s along its height."""
        sc = np.clip(s, 0.0, 1.0)
        body = np.exp(-(u / 0.98) ** 4)                    # defined flame edge
        halo = np.exp(-(u / 1.7) ** 2)                      # soft red-orange fringe
        core = np.exp(-(u / 0.50) ** 2) * (1.0 - 0.75 * sc)
        return (0.74 * body + 0.20 * halo + 0.34 * core) * (1.0 - 0.25 * sc ** 2)

    def place(self, s, u, t, grow):
        """Map flame coordinates (s along height, u across width) to normalised X, Y."""
        Y = self.y0 + s * self.height(t, grow)
        X = self.sway(s, t, grow) + u * self.half_width(s, t, grow)
        return X, Y

    def field(self, t, grow, energy):
        """Flame heat (0..1) for every character cell."""
        sh = self.shape
        X, Y = sh.X, sh.Y
        hf = max(self.height(t, grow), 1e-3)
        s = (Y - self.y0) / hf
        sc = np.clip(s, 0.0, 1.0)
        w = self.half_width(sc, t, grow)
        u = np.abs(X - self.sway(sc, t, grow)) / w
        u = u * (1.0 + 0.22 * flow(X * 6.0, Y * 6.5 - t * 2.6, t * 1.3))
        heat = self.profile(u, sc)
        env = smoothstep(-0.03, 0.10, s) * (1.0 - smoothstep(0.93, 1.12, s))
        bands = 0.88 + 0.12 * flow(X * 4.5, Y * 9.0 - t * 4.2, t * 0.7)
        return np.clip(heat * env * bands * energy, 0.0, 1.0)


# =============================================================================
# 6. PARTICLES
# =============================================================================
@dataclass
class Particle:
    """Snapshot of one particle.  ParticleSystem stores the same fields as numpy
    columns (struct-of-arrays) so thousands of them can be updated per frame."""
    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    intensity: float = 1.0
    life: float = 1.0
    max_life: float = 1.0
    flicker: float = 4.0                 # Hz
    accent: bool = False
    char: str = " "
    color: Tuple[int, int, int] = (0, 0, 0)


class Controls:
    """Everything the animation stages drive (see AnimationController.controls)."""
    stage = 1
    gain = 0.0            # fraction of particles that are visible
    radius = 0.05         # size of the initial spark cloud
    structure = 0.0       # how far particles have moved to their place in the diya
    flame_grow = 0.12     # size of the flame (0..1)
    field_gain = 0.0      # how much of the solid, dense diya texture is revealed
    glow_gain = 0.0       # ambient radial glow
    accent_gain = 0.0     # cyan / blue accents allowed
    core_heat = 0.55      # temperature of the middle of the spark cloud
    flick_depth = 0.95    # how strongly particles blink on/off
    alive = False


class ParticleSystem:
    """
    Three kinds of "former" particles (bowl, flame, ambient) start as a spark cloud and
    drift to their place in the diya, plus a pool of free-flying sparks that rise
    from the flame.
    """

    C0 = (0.0, 0.0)      # centre of the first sparks (centre of the terminal)

    def __init__(self, shape: DiyaShape, fire: FireSimulation, cfg: Config,
                 rng: np.random.Generator):
        self.shape, self.fire, self.cfg, self.rng = shape, fire, cfg, rng
        cells = shape.W * shape.H
        d = max(cfg.density, 0.05)
        nA, nB, nC = int(0.14 * cells * d), int(0.10 * cells * d), int(0.30 * cells * d)
        nS = int(0.045 * cells * d) + 24
        n = nA + nB + nC
        self.n, self.nS = n, nS
        self.sl_bowl = slice(0, nA)
        self.sl_flame = slice(nA, nA + nB)
        self.sl_amb = slice(nA + nB, n)

        # --- common properties of the formers
        scale = np.array([0.75, 0.55])
        self.sc_dir = np.clip(rng.standard_normal((n, 2)), -2.4, 2.4) * scale
        self.appear = rng.random(n)
        self.delay = rng.random(n)
        self.intensity = 0.55 + 0.45 * rng.random(n)
        self.flick = rng.uniform(1.5, 9.0, n)
        self.phase = rng.uniform(0.0, TAU, n)
        self.accent = np.zeros(n, bool)

        # --- bowl particles: sampled on the lamp
        self.bowl_hx, self.bowl_hy, self.bowl_heat = shape.sample_bowl(nA, rng)
        self.accent[self.sl_bowl] = rng.random(nA) < 0.02 * cfg.accent

        # --- flame particles: coordinates inside the (moving) flame
        s = np.empty(0)
        while s.size < nB:
            c = rng.random(nB * 3 + 32)
            keep = rng.random(c.size) < np.sqrt(np.maximum(np.sin(math.pi * c ** 0.62), 0.0))
            s = np.concatenate([s, c[keep]])
        self.fs = s[:nB]
        self.fu = np.clip(rng.normal(0.0, 0.55, nB), -1.5, 1.5)
        self.accent[self.sl_flame] = (np.abs(self.fu) > 1.0) & (rng.random(nB) < 0.35 * cfg.accent)

        # --- ambient particles: a halo around the flame with different behaviours
        r = np.abs(rng.standard_normal(nC)) * 0.44
        a = rng.uniform(0.0, TAU, nC)
        self.amb_hx = r * np.cos(a) * 1.35
        self.amb_hy = 0.06 + r * np.sin(a) * 0.95
        self.beh = rng.integers(0, 4, nC)       # 0 still, 1 drifts sideways, 2 rises, 3 pulses
        self.beh_rate = rng.uniform(0.08, 0.35, nC)
        self.accent[self.sl_amb] = rng.random(nC) < 0.09 * cfg.accent

        self._hx = np.empty(n)
        self._hy = np.empty(n)
        self._home = np.empty(n)
        self._hx[self.sl_bowl] = self.bowl_hx
        self._hy[self.sl_bowl] = self.bowl_hy

        # --- free-flying sparks
        self.s_x = np.zeros(nS)
        self.s_y = np.zeros(nS)
        self.s_vx = np.zeros(nS)
        self.s_vy = np.zeros(nS)
        self.s_life = np.zeros(nS)
        self.s_max = np.ones(nS)
        self.s_int = np.ones(nS)
        self.s_acc = np.zeros(nS, bool)
        self.s_rate = rng.uniform(2.0, 8.0, nS)
        self.s_ph = rng.uniform(0.0, TAU, nS)
        self.spark_rate = nS / 1.8            # emissions per second at full flame

    # ---- sparks ---------------------------------------------------------
    def emit_sparks(self, count: int, t: float, grow: float, strength: float = 1.0):
        free = np.flatnonzero(self.s_life <= 0.0)[:count]
        m = free.size
        if m == 0:
            return
        rng = self.rng
        s = rng.uniform(0.05, 0.70, m)
        u = rng.normal(0.0, 0.55, m)
        x, y = self.fire.place(s, u, t, grow)
        life = rng.uniform(0.9, 2.8, m) * (0.8 + 0.4 * strength)
        self.s_x[free], self.s_y[free] = x, y
        self.s_vx[free] = rng.normal(0.0, 0.035 * strength, m)
        self.s_vy[free] = rng.uniform(0.10, 0.50, m) * strength
        self.s_life[free] = life
        self.s_max[free] = life
        self.s_int[free] = rng.uniform(0.75, 1.0, m)
        self.s_acc[free] = rng.random(m) < 0.07 * self.cfg.accent

    def _update_sparks(self, t, dt, ctl):
        self.s_life -= dt
        np.maximum(self.s_life, -1.0, out=self.s_life)
        self.s_x += (self.s_vx + 0.045 * np.sin(TAU * self.s_rate * t + self.s_ph)) * dt
        self.s_y += self.s_vy * dt
        self.s_vx *= max(0.0, 1.0 - 0.25 * dt)
        if ctl.stage >= 4 and ctl.flame_grow > 0.3:
            n = self.rng.poisson(self.spark_rate * ctl.flame_grow ** 2 * dt)
            if n:
                self.emit_sparks(int(n), t, ctl.flame_grow)

    def snapshot(self, i: int) -> Particle:
        """Return spark i as a Particle record (handy for debugging / extensions)."""
        life = float(self.s_life[i])
        h = float(self.s_int[i] * max(life, 0.0) / self.s_max[i])
        ch, col = shade(np.array([h]), np.zeros(1, np.int64), np.array([bool(self.s_acc[i])]))
        r, g, b = COLOR_TABLE[int(col[0])].tolist()
        return Particle(float(self.s_x[i]), float(self.s_y[i]), float(self.s_vx[i]),
                        float(self.s_vy[i]), float(self.s_int[i]), life, float(self.s_max[i]),
                        float(self.s_rate[i]), bool(self.s_acc[i]), GLYPHS[int(ch[0])], (r, g, b))

    # ---- per-frame state -------------------------------------------------
    def render(self, t, dt, ctl: Controls):
        """Advance the simulation and return per-cell (heat, glyph variant, accent) layers."""
        sh = self.shape
        H, W = sh.H, sh.W
        heat_buf = np.zeros((H, W))
        var_buf = np.zeros((H, W), np.int64)
        acc_buf = np.zeros((H, W), bool)
        if ctl.stage < 2:
            return heat_buf, var_buf, acc_buf

        self._update_sparks(t, dt, ctl)

        n = self.n
        k = smoothstep(0.0, 1.0, np.clip((ctl.structure - 0.6 * self.delay) / 0.4, 0.0, 1.0))
        ph = self.phase

        # cloud ("scatter") positions and heat
        sx = self.C0[0] + self.sc_dir[:, 0] * ctl.radius + 0.006 * np.sin(TAU * 0.7 * t + ph)
        sy = self.C0[1] + self.sc_dir[:, 1] * ctl.radius + 0.006 * np.cos(TAU * 0.6 * t + ph)
        d = np.hypot(sx - self.C0[0], (sy - self.C0[1]) * 1.3)
        scat = (0.10 + ctl.core_heat * np.exp(-(d / (0.22 + 0.55 * ctl.radius)) ** 2)) * self.intensity

        # home positions / heat
        hx, hy, home = self._hx, self._hy, self._home
        b, f, a = self.sl_bowl, self.sl_flame, self.sl_amb
        wave_x = 0.010 * np.sin(TAU * 0.31 * t + 3.1 * self.bowl_hy + ph[b])
        hx[b] = self.bowl_hx + wave_x
        hy[b] = self.bowl_hy + 0.006 * np.cos(TAU * 0.27 * t + ph[b])
        home[b] = self.bowl_heat * (0.80 + 0.20 * np.sin(TAU * self.flick[b] * 0.4 * t + ph[b]))

        fx, fy = self.fire.place(self.fs, self.fu, t, ctl.flame_grow)
        hx[f], hy[f] = fx, fy
        home[f] = self.fire.profile(np.abs(self.fu), self.fs) * (0.80 + 0.20 * self.intensity[f])

        pa, beh, br = ph[a], self.beh, self.beh_rate
        rise_ph = (t * br * 0.5 + pa / TAU) % 1.0
        drift = np.where(beh == 1, 0.07 * np.sin(TAU * br * t + pa), 0.0)
        rise = np.where(beh == 2, rise_ph * 0.55, 0.0)
        ax = self.amb_hx + 0.014 * np.sin(TAU * 0.31 * t + 3.1 * self.amb_hy + pa) + drift
        ay = self.amb_hy + 0.011 * np.cos(TAU * 0.27 * t + 2.3 * self.amb_hx + pa) + rise
        hx[a], hy[a] = ax, ay
        illum = np.exp(-((ax / 0.95) ** 2 + ((ay - 0.12) / 0.90) ** 2))
        mod = np.ones(a.stop - a.start)
        mod = np.where(beh == 2, (1.0 - rise_ph) ** 0.7 * smoothstep(0.0, 0.06, rise_ph), mod)
        mod = np.where(beh == 3, 0.25 + 0.75 * np.maximum(0.0, np.sin(TAU * br * t + pa)) ** 3, mod)
        home[a] = (0.14 + 0.52 * illum) * self.intensity[a] * mod * (0.4 + 0.6 * ctl.glow_gain)

        # blend cloud -> home, then apply appear-over-time and on/off flickering
        x = sx * (1.0 - k) + hx * k
        y = sy * (1.0 - k) + hy * k
        heat = scat * (1.0 - k) + home * k
        vis = np.clip((ctl.gain - self.appear) * 20.0, 0.0, 1.0)
        raw = 0.5 + 0.5 * np.sin(TAU * self.flick * t + ph)
        fl = (1.0 - ctl.flick_depth) + ctl.flick_depth * smoothstep(0.15, 0.40, raw)
        heat = heat * vis * fl
        accent = self.accent & (ctl.accent_gain > 0.5)
        var = ((t * self.flick * 0.35 + ph).astype(np.int64)) & 3

        # sparks
        alive = self.s_life > 0.0
        shp = np.clip(self.s_life / self.s_max, 0.0, 1.0) ** 0.85
        sheat = self.s_int * shp * (0.82 + 0.18 * np.sin(TAU * self.s_rate * t + self.s_ph)) * alive
        svar = ((t * self.s_rate * 0.5 + self.s_ph).astype(np.int64)) & 3

        X = np.concatenate([x, self.s_x])
        Y = np.concatenate([y, self.s_y])
        Hh = np.concatenate([heat, sheat])
        V = np.concatenate([var, svar])
        A = np.concatenate([accent, self.s_acc])

        r, c = sh.to_cell(X, Y)
        ok = (Hh > LEVEL_EDGES[0]) & (r >= 0) & (r < H) & (c >= 0) & (c < W)
        r, c, Hh, V, A = r[ok], c[ok], Hh[ok], V[ok], A[ok]
        order = np.argsort(Hh)           # hottest particle written last wins the cell
        r, c = r[order], c[order]
        heat_buf[r, c] = Hh[order]
        var_buf[r, c] = V[order]
        acc_buf[r, c] = A[order]
        return heat_buf, var_buf, acc_buf


# =============================================================================
# 7. ANIMATION CONTROLLER
# =============================================================================
class AnimationController:
    """Runs the five stages and composes flame, lamp, glow and particles into one frame."""

    # end of each stage in seconds at speed 1 / intro 1:
    # 1 darkness, 2 first sparks, 3 expansion, 4 formation, then 5 living artwork
    STAGE_ENDS = (1.0, 4.0, 8.5, 15.0)

    def __init__(self, cfg: Config, W: int, H: int):
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.seed)
        self.bounds = [e * max(cfg.intro, 0.05) for e in self.STAGE_ENDS]
        self.burst = 0.0
        self.resize(W, H)

    def resize(self, W: int, H: int):
        self.W, self.H = W, H
        self.shape = DiyaShape(W, H, self.rng)
        self.fire = FireSimulation(self.shape)
        self.particles = ParticleSystem(self.shape, self.fire, self.cfg, self.rng)

    # ---- stage logic ----------------------------------------------------
    def controls(self, t: float) -> Controls:
        b0, b1, b2, b3 = self.bounds
        c = Controls()
        if t < b0:                                    # stage 1: darkness
            return c
        if t < b1:                                    # stage 2: first sparks
            e = smoothstep(0.0, 1.0, (t - b0) / (b1 - b0))
            c.stage, c.gain = 2, 0.002 + 0.028 * e
            c.radius, c.core_heat = 0.05 + 0.10 * e, 0.50 + 0.12 * e
        elif t < b2:                                  # stage 3: particle expansion
            p = (t - b1) / (b2 - b1)
            e = smoothstep(0.0, 1.0, p)
            c.stage, c.gain = 3, 0.03 + 0.72 * p ** 1.5
            c.radius, c.structure = 0.15 + 0.85 * e, 0.34 * e
            c.core_heat = 0.62 + 0.30 * e
            c.glow_gain = 0.30 * smoothstep(0.4, 1.0, p)
            c.accent_gain, c.flick_depth = p, 0.95 - 0.10 * p
        elif t < b3:                                  # stage 4: diya formation
            p = (t - b2) / (b3 - b2)
            e = smoothstep(0.0, 1.0, p)
            c.stage, c.gain = 4, 0.75 + 0.25 * e
            c.radius, c.structure = 1.0, 0.34 + 0.80 * e
            c.flame_grow = 0.12 + 0.88 * smoothstep(0.05, 0.95, p)
            c.field_gain = smoothstep(0.25, 1.0, p)
            c.glow_gain, c.accent_gain = 0.30 + 0.70 * e, 1.0
            c.core_heat, c.flick_depth = 0.95, 0.85 - 0.50 * e
        else:                                         # stage 5: living artwork
            c.stage, c.gain, c.radius, c.structure = 5, 1.0, 1.0, 1.14
            c.flame_grow, c.field_gain, c.glow_gain, c.accent_gain = 1.0, 1.0, 1.0, 1.0
            c.core_heat, c.flick_depth, c.alive = 0.95, 0.35, True
        return c

    def _tick_bursts(self, t, dt, ctl):
        self.burst *= math.exp(-dt / 0.55)
        if ctl.alive and self.rng.random() < 0.30 * dt:          # occasional glowing burst
            self.burst = 1.0
            self.particles.emit_sparks(max(8, int(0.10 * self.particles.nS)), t,
                                       ctl.flame_grow, strength=1.5)
        self.fire.burst = self.burst

    # ---- composition ------------------------------------------------------
    def frame(self, t: float, dt: float) -> Tuple[np.ndarray, np.ndarray]:
        ctl = self.controls(t)
        sh = self.shape
        if ctl.stage < 2:
            z = np.zeros((sh.H, sh.W), np.uint8)
            return z, z
        self._tick_bursts(t, dt, ctl)
        energy = 1.0 + 0.16 * self.burst
        flick = (0.90 + 0.05 * math.sin(TAU * 3.3 * t + 1.0)
                 + 0.03 * math.sin(TAU * 7.9 * t) + 0.02 * math.sin(TAU * 13.1 * t + 2.0))

        # solid layers: flame + lamp, revealed from the centre outwards
        flame = self.fire.field(t, ctl.flame_grow, energy)
        shimmer = 0.84 + 0.16 * np.sin(t * sh.rate + sh.phase)
        light = float(np.clip(flick + 0.10 * self.burst, 0.8, 1.15))
        bowl = sh.bowl_heat * light * shimmer
        rev = np.clip((ctl.field_gain * 1.35 - sh.rv) / 0.22, 0.0, 1.0)
        field = np.maximum(flame, bowl) * rev
        field = field * (0.92 + 0.08 * np.sin(t * sh.rate * 1.3 + sh.phase * 2.0))

        # ambient glow: a dithered, twinkling halo
        g = sh.glow * ctl.glow_gain * self.cfg.glow * (0.82 + 0.18 * flick + 0.25 * self.burst)
        tw = 0.5 + 0.5 * np.sin(t * sh.rate2 + sh.phase)
        glow_h = np.where(sh.r2 < np.clip(g * 1.25, 0.0, 1.0), g * 0.40 * (0.40 + 0.75 * tw), 0.0)
        field = np.maximum(field, glow_h)

        # digital accents: a few cyan/blue cells twinkling in the glow
        acc_f = (sh.r3 > 1.0 - 0.020 * self.cfg.accent) & (g > 0.10) & (tw > 0.80) & (field < 0.30)
        field = np.where(acc_f, 0.30 + 0.5 * tw, field)

        var_f = ((t * sh.rate + sh.phase * 2.0).astype(np.int64)) & 3

        # particle layer on top
        hp, vp, ap = self.particles.render(t, dt, ctl)
        use_p = hp > field
        heat = np.where(use_p, hp, field)
        var = np.where(use_p, vp, var_f)
        acc = np.where(use_p, ap, acc_f)

        ch, col = shade(heat, var, acc)
        # outline glyphs that follow the curve of the lamp
        edge = sh.edge_sel & ~use_p & (rev > 0.5) & (ch != 0)
        ch = np.where(edge, sh.edge_glyph, ch).astype(np.uint8)
        return ch, col


# =============================================================================
# 8. MAIN LOOP
# =============================================================================
def _signal_exit(signum, frame):  # pragma: no cover
    raise SystemExit(128 + signum)


def run(cfg: Config) -> None:
    renderer = TerminalRenderer(cfg)
    atexit.register(renderer.close)
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _signal_exit)
            except (ValueError, OSError):
                pass

    controller: Optional[AnimationController] = None
    frame_dt = 1.0 / max(1.0, cfg.fps)
    try:
        renderer.open()
        t0 = last = next_frame = time.perf_counter()
        while True:
            now = time.perf_counter()
            if cfg.duration > 0 and now - t0 >= cfg.duration:
                break
            if renderer.poll_quit():
                break

            cols, rows = renderer.terminal_size()
            if (cols, rows) != renderer.size:                      # first frame or resized
                if renderer.too_small(cols, rows):
                    renderer.size = (cols, rows)
                    renderer.show_message("Please enlarge the terminal (min %dx%d)"
                                          % (renderer.MIN_COLS, renderer.MIN_ROWS))
                    controller = None
                else:
                    W, H = renderer.set_size(cols, rows)
                    if controller is None:
                        controller = AnimationController(cfg, W, H)
                    else:
                        controller.resize(W, H)

            if controller is not None:
                dt = min(now - last, 0.1)
                ch, col = controller.frame((now - t0) * cfg.speed, dt * cfg.speed)
                renderer.present(ch, col)
            last = now

            # frame pacing: sleep only for what is left of this frame's time budget
            next_frame += frame_dt
            delay = next_frame - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_frame = time.perf_counter()      # fell behind: don't try to catch up
    except KeyboardInterrupt:
        pass
    finally:
        renderer.close()
    if sys.stdout.isatty():
        print("Shubh Deepavali!")


def parse_args(argv=None) -> Config:
    p = argparse.ArgumentParser(
        description="Animated ASCII Diwali diya for your terminal. Ctrl+C or q to quit.")
    d = Config()
    p.add_argument("--fps", type=float, default=d.fps, help="target frame rate (default 30)")
    p.add_argument("--speed", type=float, default=d.speed, help="animation speed multiplier (default 1.0)")
    p.add_argument("--intro", type=float, default=d.intro,
                   help="length multiplier of the intro stages, 0.3 = quick, 2 = slow (default 1.0)")
    p.add_argument("--density", type=float, default=d.density, help="particle density multiplier (default 1.0)")
    p.add_argument("--glow", type=float, default=d.glow, help="ambient glow strength (default 1.0)")
    p.add_argument("--accent", type=float, default=d.accent,
                   help="amount of cyan/blue accent particles, 0 disables (default 1.0)")
    p.add_argument("--cols", type=int, default=d.cols, help="canvas width in characters (default: terminal width)")
    p.add_argument("--rows", type=int, default=d.rows, help="canvas height in lines (default: terminal height)")
    p.add_argument("--color", dest="color_mode", choices=["auto", "truecolor", "256"], default=d.color_mode,
                   help="colour mode (default: auto-detect)")
    p.add_argument("--seed", type=int, default=None, help="random seed for a repeatable animation")
    p.add_argument("--duration", type=float, default=d.duration, help="exit automatically after N seconds")
    a = p.parse_args(argv)
    return Config(fps=a.fps, speed=a.speed, intro=a.intro, density=a.density, glow=a.glow,
                  accent=a.accent, cols=a.cols, rows=a.rows, color_mode=a.color_mode,
                  seed=a.seed, duration=a.duration)


def main(argv=None) -> int:
    cfg = parse_args(argv)
    if not sys.stdout.isatty():
        sys.stderr.write("diya.py must be run in an interactive terminal.\n")
        return 1
    run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
