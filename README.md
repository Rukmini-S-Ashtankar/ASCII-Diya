# Animated ASCII Diya (terminal)

A Diwali diya that condenses out of thousands of glowing ASCII characters, then keeps
burning: flickering flame, rising sparks, glowing bursts. Pure Python, drawn directly in
the terminal with ANSI escape codes (no GUI, browser, video or image).

## Install and run

**Linux / macOS**
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 diya.py
```

**Windows** (use Windows Terminal or the VS Code terminal)
```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
py diya.py
```

Quit with **Ctrl+C** or **q**. The terminal, cursor and colours are restored on exit.
Best with a dark theme and a large window (120x40 or more). Minimum is 30x12.

No curses is needed: the renderer writes ANSI sequences itself, which gives 24-bit colour
and works the same on every platform (so `windows-curses` is not required).

## Options

| Goal | Option | Notes |
|---|---|---|
| Animation speed | `--speed 1.5` | Global time scale, also speeds up the intro. |
| Intro length | `--intro 0.4` / `--intro 2` | Multiplies the length of stages 1-4 (default about 15 s). |
| Particle density | `--density 1.5` | 0.5 = sparse and fast, 2 = very dense. |
| Ambient glow | `--glow 0.5` | Strength of the red-orange halo. |
| Cyan/blue accents | `--accent 0` / `--accent 2` | 0 removes them. |
| Frame rate | `--fps 24` | Default 30. Lower it on slow terminals. |
| Canvas size | `--cols 100 --rows 30` | Centred inside the terminal. Default is the whole terminal. |
| Colour mode | `--color truecolor` / `--color 256` | Default auto-detects. |
| Repeatable run | `--seed 42` | Same randomness every time. |
| Auto exit | `--duration 20` | Seconds. |

Example: `python diya.py --density 1.3 --intro 0.6 --accent 0.5`

### Changing the colours

Open `diya.py` and edit `FIRE_STOPS` and `ACCENT_STOPS` near the top. Each entry is
`(heat 0..1, (r, g, b))`; the program blends between them. Heat 0 is the dimmest particle,
1 is the flame core. `FIRE_STEPS` / `ACCENT_STEPS` set how many distinct colours are used.
The glyph groups per intensity are in `LEVEL_GROUPS`.

### Changing the stage timing

`AnimationController.STAGE_ENDS = (1.0, 4.0, 8.5, 15.0)` are the end times in seconds of
darkness, first sparks, expansion and formation.

## Troubleshooting

* **Colours look banded or wrong (older macOS Terminal.app):** it has no 24-bit colour; run with `--color 256` (auto-detect usually does this already).
* **Flicker or lag in a very large window:** try `--fps 24` or `--density 0.7`.
* **"Please enlarge the terminal":** the window is smaller than 30x12.
* **Garbage characters on very old Windows consoles:** use Windows Terminal.

## How it works

* `TerminalRenderer`: alternate screen, hidden cursor, black background, and diff-based
  output (only changed cells are sent, grouped into runs). Resize is handled every frame.
* `DiyaShape`: the lamp (bowl, rim ellipse with glowing oil, pearl band, foot) as maths,
  plus outline glyphs (`/ \ | - _`) that follow the curve.
* `FireSimulation`: teardrop flame whose height, width, sway and heat change every frame.
* `Particle` / `ParticleSystem`: a spark cloud whose particles drift to their place in the
  lamp, flame and glow, plus free-flying sparks. Fields are stored as numpy columns.
* `AnimationController`: the five stages and the final composition of every cell's glyph and colour.
