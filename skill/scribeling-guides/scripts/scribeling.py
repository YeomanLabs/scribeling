#!/usr/bin/env python3
"""
scribeling - click-by-click procedure recorder for Windows.

Run with no arguments for the GUI. The command line still works:

    scribeling.py record --title "Enroll a device"
    scribeling.py rebuild guides/2026-08-15_143002
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# bootstrap - must run before uiautomation is imported
# --------------------------------------------------------------------------

def bootstrap():
    """Per-monitor DPI awareness, or UIA rectangles land in the wrong place on
    any scaled display. Plus the comtypes fix for PyInstaller: a frozen build
    cannot write generated interface code inside its own bundle."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        try:
            import comtypes.client
            comtypes.client.gen_dir = None
        except Exception:
            pass


SIGNAL = (214, 0, 110)
DEFAULT_PAD = 220
MIN_CROP = (900, 420)
MAX_WIDTH = 1400
DEDUPE_WINDOW = 1.6      # seconds; a repeat click on one target is one step
MAX_TARGET_AREA = 0.45   # bigger than this is a container, not a target
RING = 18                # radius of the click-point marker, in screen pixels


@dataclass
class Config:
    title: str = "Untitled procedure"
    description: str = ""
    author: str = ""
    outdir: Path | None = Path("guides")
    pad: int = DEFAULT_PAD
    full_frames: bool = False
    dim: bool = False
    truecolor: bool = False
    mask_typed: bool = False
    capture_typing: bool = True


@dataclass
class Step:
    index: int
    action: str = "click"
    target: str = ""
    control_type: str = ""
    window: str = ""
    image: str = ""
    caption: str = ""
    note: str = ""
    ts: str = ""
    hidden: bool = False


# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------

# Only these control types are worth naming in the instruction. Group, Pane,
# Text, Custom and friends tell the reader nothing.
FRIENDLY = {
    "Edit": "field", "CheckBox": "checkbox", "RadioButton": "option",
    "ComboBox": "dropdown", "TabItem": "tab", "MenuItem": "menu item",
    "Slider": "slider", "Spinner": "spinner", "SplitButton": "button",
}

BROWSERS = re.compile(
    r"\s+[-\u2013\u2014]\s+(Google Chrome|Microsoft Edge|Mozilla Firefox|Brave|"
    r"Opera|Vivaldi)\s*$")
MORE_PAGES = re.compile(r"\s+and \d+ more pages?.*$")
UNREAD = re.compile(r"^\(\d+\)\s*")
WHITESPACE = re.compile(r"\s+")


def clean_window(title: str) -> str:
    """Drop browser chrome, unread counters and tab-overflow noise so the
    context line says where you are, not what you are using."""
    t = (title or "").strip()
    if not t:
        return ""
    if t.lower().endswith(".exe"):
        return re.split(r"[\\/]", t)[-1][:-4]
    t = UNREAD.sub("", t)
    t = MORE_PAGES.sub("", t)
    t = BROWSERS.sub("", t)
    return WHITESPACE.sub(" ", t).strip(" -\u2013\u2014")[:90]


def clean_name(name: str) -> str:
    n = WHITESPACE.sub(" ", (name or "").strip())
    if len(n) > 70:
        n = n[:70].rsplit(" ", 1)[0] + "\u2026"
    return n


def caption_for(step: Step) -> str:
    name = clean_name(step.target)
    kind = FRIENDLY.get(step.control_type, "")

    if step.action == "uac":
        return "Approve the **User Account Control** prompt"

    if step.action == "note":
        return step.note or "Add your note here"

    if step.action == "type":
        where = f"**{name}**" if name else "the highlighted field"
        if step.note == "\x00password":
            return f"In {where}, enter your password"
        if not step.note:
            return f"Fill in {where}"
        return f"In {where}, type `{step.note}`"

    if not name:
        return "Click the highlighted control"
    if kind:
        return f"Click the **{name}** {kind}"
    return f"Click **{name}**"


# --------------------------------------------------------------------------
# UI Automation
# --------------------------------------------------------------------------

class UIAProbe:
    """A hung or missing accessibility tree degrades to a coordinate highlight
    rather than taking the recorder down."""

    def __init__(self):
        self._ready = False

    def _init_thread(self):
        if self._ready:
            return
        import uiautomation as auto
        for call in (auto.InitializeUIAutomationInCurrentThread,
                     lambda: auto.SetGlobalSearchTimeout(1)):
            try:
                call()
            except Exception:
                pass
        self._ready = True

    @staticmethod
    def _read(node, screen_area):
        name = clean_name(node.Name)
        ctype = (node.ControlTypeName or "").replace("Control", "")
        r = node.BoundingRectangle
        box = (r.left, r.top, r.right, r.bottom)
        area = max(0, box[2] - box[0]) * max(0, box[3] - box[1])
        usable = bool(area) and area < screen_area * MAX_TARGET_AREA
        return name, ctype, box, usable

    @staticmethod
    def _is_password(node) -> bool:
        try:
            if getattr(node, "IsPassword", False):
                return True
        except Exception:
            pass
        try:
            return "password" in (node.ClassName or "").lower()
        except Exception:
            return False

    def _window(self) -> str:
        import uiautomation as auto
        try:
            top = auto.GetForegroundControl()
            name = clean_window(top.Name) if top else ""
        except Exception:
            name = ""
        return name or foreground_title()

    def at_point(self, x, y, screen_area) -> dict:
        import uiautomation as auto
        self._init_thread()
        info = {"name": "", "type": "", "rect": None, "window": "", "password": False}
        try:
            node = auto.ControlFromPoint(x, y)
        except Exception:
            node = None

        depth = 0
        while node is not None and depth < 4:
            try:
                name, ctype, box, usable = self._read(node, screen_area)
            except Exception:
                break
            if name and usable:
                info.update(name=name, type=ctype, rect=box,
                            password=self._is_password(node))
                break
            if info["rect"] is None and usable:
                info.update(rect=box, type=ctype)
            try:
                node = node.GetParentControl()
            except Exception:
                break
            depth += 1

        info["window"] = self._window()
        return info

    def focused(self, screen_area) -> dict:
        import uiautomation as auto
        self._init_thread()
        info = {"name": "", "type": "", "rect": None, "window": "", "password": False}
        try:
            node = auto.GetFocusedControl()
            if node is not None:
                name, ctype, box, _ = self._read(node, screen_area)
                info.update(name=name, type=ctype, rect=box,
                            password=self._is_password(node))
        except Exception:
            pass
        info["window"] = self._window()
        return info


# --------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------

def monitor_for(sct, x, y):
    for mon in sct.monitors[1:]:
        if mon["left"] <= x < mon["left"] + mon["width"] and \
           mon["top"] <= y < mon["top"] + mon["height"]:
            return mon
    return sct.monitors[0]


def grab(x, y):
    import mss
    from PIL import Image
    with mss.mss() as sct:
        mon = monitor_for(sct, x, y)
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img, (mon["left"], mon["top"]), mon["width"] * mon["height"]


def fit_span(lo, hi, minimum, limit):
    """Grow a crop span to a minimum, then slide it back inside the frame
    instead of letting the screen edge silently shrink it again."""
    if hi - lo < minimum:
        grow = (minimum - (hi - lo)) / 2
        lo, hi = lo - grow, hi + grow
    if lo < 0:
        hi -= lo
        lo = 0
    if hi > limit:
        lo -= hi - limit
        hi = limit
    return max(0, lo), min(limit, hi)


def annotate(img, rect, offset, cfg: Config, point=None):
    from PIL import Image, ImageDraw

    ox, oy = offset
    w, h = img.size

    if rect:
        box = [rect[0] - ox, rect[1] - oy, rect[2] - ox, rect[3] - oy]
    elif point:
        px, py = point[0] - ox, point[1] - oy
        box = [px - 45, py - 22, px + 45, py + 22]
    else:
        box = [w // 2 - 60, h // 2 - 30, w // 2 + 60, h // 2 + 30]

    box = [max(0, min(box[0], w - 2)), max(0, min(box[1], h - 2)),
           max(2, min(box[2], w)), max(2, min(box[3], h))]

    if cfg.dim:
        keep = img.crop(tuple(int(v) for v in box))
        img = Image.blend(img, Image.new("RGB", img.size, (8, 10, 16)), 0.5)
        img.paste(keep, (int(box[0]), int(box[1])))

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle([box[0] - 6, box[1] - 6, box[2] + 6, box[3] + 6],
                           radius=9, outline=SIGNAL + (70,), width=6)
    draw.rounded_rectangle(box, radius=5, outline=SIGNAL + (255,), width=3)
    if point:
        # Where the pointer actually landed. The box says which control; the
        # ring says where on it, which matters for wide rows and split buttons.
        cx, cy = point[0] - ox, point[1] - oy
        draw.ellipse([cx - RING, cy - RING, cx + RING, cy + RING],
                     fill=SIGNAL + (40,), outline=SIGNAL + (230,), width=3)
    img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

    if not cfg.full_frames:
        l, r = fit_span(box[0] - cfg.pad, box[2] + cfg.pad, MIN_CROP[0], w)
        t, b = fit_span(box[1] - cfg.pad, box[3] + cfg.pad, MIN_CROP[1], h)
        img = img.crop((int(l), int(t), int(r), int(b)))

    if img.width > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, int(img.height * MAX_WIDTH / img.width)),
                         Image.LANCZOS)

    if not cfg.truecolor:
        img = img.convert("P", palette=Image.ADAPTIVE, colors=256)
    return img


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """UIPI blocks a medium-integrity process from reading the UIA tree of an
    elevated window. Running elevated fixes it: high integrity can read both
    high and medium."""
    try:
        args = " ".join(f'"{a}"' for a in sys.argv[1:])
        if getattr(sys, "frozen", False):
            target, params = sys.executable, args
        else:
            target = sys.executable
            params = f'"{Path(sys.argv[0]).resolve()}" {args}'.strip()
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", target,
                                                params, None, 1)
        return rc > 32
    except Exception:
        return False


def display_name() -> str:
    """The signed-in user's display name ("Jane Smith" rather than
    "jsmith"), for the byline. Falls back to the logon name off-domain."""
    try:
        size = ctypes.c_ulong(0)
        secur32 = ctypes.windll.secur32
        secur32.GetUserNameExW(3, None, ctypes.byref(size))   # NameDisplay
        buf = ctypes.create_unicode_buffer(size.value + 1)
        if size.value and secur32.GetUserNameExW(3, buf, ctypes.byref(size)):
            if buf.value.strip():
                return buf.value.strip()
    except Exception:
        pass
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def foreground_title() -> str:
    """GetWindowTextW reads the cached window text rather than messaging the
    owning process, so it survives UIPI where UIA does not."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return clean_window(buf.value)
    except Exception:
        return ""


def secure_desktop_active() -> bool:
    """The UAC consent prompt runs on an isolated desktop that cannot be
    screenshotted or hooked by anything. OpenInputDesktop failing is the tell."""
    try:
        handle = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0001)
        if handle:
            ctypes.windll.user32.CloseDesktop(handle)
            return False
        return True
    except Exception:
        return False


def is_own_window(x, y) -> bool:
    """The HUD floats on top of whatever you are documenting. Without this,
    every press of Pause or Note becomes a step in the guide."""
    try:
        import ctypes.wintypes as wt
        user32 = ctypes.windll.user32
        user32.WindowFromPoint.argtypes = [wt.POINT]
        user32.WindowFromPoint.restype = wt.HWND
        hwnd = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()
    except Exception:
        return False


def cursor_pos():
    try:
        import ctypes.wintypes as wt
        pt = wt.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y
    except Exception:
        return 0, 0


# --------------------------------------------------------------------------
# recorder
# --------------------------------------------------------------------------

class Recorder:
    """Headless engine. Emits (event, payload) to on_event for whatever UI is
    driving it."""

    def __init__(self, cfg: Config, session: Path, on_event=None):
        self.cfg = cfg
        self.session = session
        self.shots = session / "shots"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.on_event = on_event or (lambda *a: None)
        self.steps: list[Step] = []
        self.jobs: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.paused = False
        self.probe = UIAProbe()
        self.counter = 0
        self.lock = threading.Lock()
        self.typed: list[str] = []
        self.typing_target: dict | None = None
        self.last_sig = None
        self.last_time = 0.0
        self._mouse = None
        self._keys = None

    # -- hooks -------------------------------------------------------------

    def on_click(self, x, y, button, pressed):
        if not pressed or self.paused or is_own_window(x, y):
            return
        try:
            self.flush_typed()
            img, offset, area = grab(x, y)
            info = self.probe.at_point(x, y, area)
            if self.is_repeat(info, (x, y)):
                return
            self.add("click", img, offset, info, (x, y), "")
        except Exception as exc:
            self.on_event("error", f"skipped a click: {exc}")

    def on_press(self, key):
        from pynput import keyboard

        if key == keyboard.Key.f8:
            self.request_stop()
            return
        if key == keyboard.Key.f7:
            self.toggle_pause()
            return
        if key == keyboard.Key.f9:
            self.drop_last()
            return
        if key == keyboard.Key.f10:
            self.add_note()
            return
        if self.paused or not self.cfg.capture_typing:
            return

        if key in (keyboard.Key.enter, keyboard.Key.tab):
            self.flush_typed(", then press Enter"
                             if key == keyboard.Key.enter else "")
            return
        if key == keyboard.Key.backspace:
            if self.typed:
                self.typed.pop()
            return
        char = " " if key == keyboard.Key.space else getattr(key, "char", None)
        if not char or not char.isprintable():
            return
        if not self.typed:
            # Bind to the field NOW. Waiting until flush is what attributed
            # the Name value to the Description box in the first build.
            try:
                _, _, area = grab(*cursor_pos())
                self.typing_target = self.probe.focused(area)
            except Exception:
                self.typing_target = None
        self.typed.append(char)

    def is_repeat(self, info, point=None) -> bool:
        """Fingerprint the target. When UIA is blocked the name and rect are both
        empty, so fall back to the click point - otherwise every nameless click
        in an elevated app looks like a repeat of the last one."""
        rect = info.get("rect")
        name, ctype = info.get("name", ""), info.get("type", "")
        if rect:
            where = tuple(round(v / 4) for v in rect)
        elif point:
            where = tuple(round(v / 8) for v in point)
        else:
            where = None
        if not name and not rect and where is None:
            return False
        sig = (name, ctype, where)
        now = time.time()
        if sig == self.last_sig and now - self.last_time < DEDUPE_WINDOW:
            self.last_time = now
            return True
        self.last_sig, self.last_time = sig, now
        return False

    # -- steps -------------------------------------------------------------

    def add(self, action, img, offset, info, point, note):
        with self.lock:
            self.counter += 1
            idx = self.counter
        step = Step(
            index=idx,
            action=action,
            target=info.get("name", ""),
            control_type=info.get("type", ""),
            window=info.get("window", ""),
            image=f"step-{idx:03d}.png",
            note=note,
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        step.caption = caption_for(step)
        if note == "\x00password":
            step.note = ""
        self.steps.append(step)
        if img is None:
            # Nothing to render - the secure desktop cannot be captured.
            step.image = ""
        else:
            self.jobs.put((step, img, offset, info.get("rect"), point))
        self.on_event("step", step)
        return step

    def flush_typed(self, suffix=""):
        if not self.typed:
            return
        text = "".join(self.typed)
        self.typed.clear()
        info = self.typing_target or {}
        self.typing_target = None
        try:
            # Screenshot now, so the field shows its finished value, but
            # highlight the element bound at the first keystroke.
            img, offset, _ = grab(*cursor_pos())
        except Exception:
            return
        if info.get("password"):
            value = "\x00password"
        elif self.cfg.mask_typed:
            value = ""
        else:
            value = text
        step = self.add("type", img, offset, info, None, value)
        if suffix:
            step.caption += suffix
            self.on_event("amend", step)

    def add_note(self):
        try:
            img, offset, area = grab(*cursor_pos())
            info = self.probe.focused(area)
        except Exception:
            return
        self.add("note", img, offset, info, None, "")

    def drop_last(self):
        if not self.steps:
            return
        gone = self.steps.pop()
        gone.hidden = True
        self.last_sig = None
        self.on_event("drop", gone)

    def toggle_pause(self):
        self.paused = not self.paused
        self.on_event("pause", self.paused)

    def request_stop(self):
        self.stop.set()

    # -- threads -----------------------------------------------------------

    def watch_uac(self):
        """Poll for the secure desktop and write the elevation step ourselves.
        Neither the screenshot nor the input hook can see the consent dialog, so
        without this the guide has a silent gap where the UAC prompt was."""
        was_secure = False
        while not self.stop.is_set():
            time.sleep(0.4)
            if self.paused:
                continue
            now_secure = secure_desktop_active()
            if now_secure and not was_secure:
                self.add("uac", None, (0, 0), {"window": foreground_title()},
                         None, "")
                self.last_sig = None
            was_secure = now_secure

    def worker(self):
        while not (self.stop.is_set() and self.jobs.empty()):
            try:
                step, img, offset, rect, point = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                annotate(img, rect, offset, self.cfg, point).save(
                    self.shots / step.image, optimize=True)
            except Exception as exc:
                step.image = ""
                self.on_event("error", f"step {step.index} render failed: {exc}")
            finally:
                self.jobs.task_done()
        self.finished.set()

    def start(self):
        from pynput import mouse, keyboard
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.watch_uac, daemon=True).start()
        self._mouse = mouse.Listener(on_click=self.on_click)
        self._keys = keyboard.Listener(on_press=self.on_press)
        self._mouse.start()
        self._keys.start()

    def shutdown(self) -> list[Step]:
        for lst in (self._mouse, self._keys):
            try:
                lst.stop()
            except Exception:
                pass
        self.flush_typed()
        self.jobs.join()
        self.stop.set()
        self.finished.wait(timeout=5)
        return [s for s in self.steps if not s.hidden]


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

CSS = """
:root{
  --bg:#f4f5f8; --card:#fff; --ink:#141821; --ink-2:#454d5d; --ink-3:#687083;
  --rule:#e2e5eb; --soft:#eef0f4; --signal:#d6006e; --signal-soft:#fdeaf3;
  --shadow:0 1px 2px rgba(20,24,33,.06),0 4px 16px rgba(20,24,33,.05);
  --display:"Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;
  --body:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
  --mono:"Cascadia Mono",Consolas,"Courier New",monospace;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#111317; --card:#1b1e24; --ink:#eceef2; --ink-2:#b4bac6; --ink-3:#8a92a3;
    --rule:#2c313a; --soft:#262a32; --signal:#ff4fa3; --signal-soft:#3a1a2b;
    --shadow:0 1px 2px rgba(0,0,0,.4); color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --bg:#111317; --card:#1b1e24; --ink:#eceef2; --ink-2:#b4bac6; --ink-3:#8a92a3;
  --rule:#2c313a; --soft:#262a32; --signal:#ff4fa3; --signal-soft:#3a1a2b;
  --shadow:0 1px 2px rgba(0,0,0,.4); color-scheme:dark;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:24px}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body);
  font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:var(--signal)}
.hero{max-width:1120px;margin:0 auto;padding:56px 28px 8px;position:relative}
.hero-inner{max-width:720px;margin-left:272px}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--signal)}
h1{font-family:var(--display);font-weight:650;font-size:clamp(28px,5vw,40px);
  line-height:1.1;letter-spacing:-.02em;margin:10px 0 0}
.lede{font-size:18px;color:var(--ink-2);max-width:64ch;margin:14px 0 0}
.meta{font-size:14px;color:var(--ink-3);margin-top:18px;display:flex;
  flex-wrap:wrap;gap:6px 18px}
.meta b{color:var(--ink-2);font-weight:600}
.tags{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.tag{font-size:13px;font-weight:600;padding:3px 10px;border-radius:6px;
  background:var(--card);border:1px solid var(--rule);color:var(--ink-2)}
.theme{position:absolute;top:24px;right:28px;width:36px;height:36px;
  border-radius:8px;border:1px solid var(--rule);background:var(--card);
  color:var(--ink-2);cursor:pointer;font-size:16px;line-height:1}
.layout{max-width:1120px;margin:0 auto;padding:32px 28px 96px;display:grid;
  grid-template-columns:240px minmax(0,720px);gap:32px;align-items:start}
.layout.solo main{grid-column:2}
.toc{position:sticky;top:20px;background:var(--card);border:1px solid var(--rule);
  border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
.toc-bar{display:flex;align-items:center;justify-content:space-between;
  padding:8px 10px;border-bottom:1px solid var(--rule);font-size:14px;color:var(--ink-3)}
.toc-bar b{color:var(--ink);font-weight:600}
.toc-bar button{width:30px;height:30px;border:0;border-radius:6px;background:none;
  color:var(--ink-2);cursor:pointer;font-size:14px}
.toc-bar button:hover{background:var(--soft)}
.toc ol{list-style:none;margin:0;padding:6px 0}
.toc a{display:block;padding:9px 14px;color:var(--ink-2);text-decoration:none;
  font-size:15px;border-left:3px solid transparent}
.toc a:hover{background:var(--soft)}
.toc a.on{color:var(--ink);font-weight:600;background:var(--soft);
  border-left-color:var(--signal)}
.prereqs{margin:0 0 28px;padding:18px 22px;background:var(--card);
  border:1px solid var(--rule);border-radius:12px;box-shadow:var(--shadow)}
.prereqs h2{font-family:var(--mono);font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--signal);margin:0 0 10px;font-weight:600}
.prereqs ul{margin:0;padding-left:20px}
.prereqs li{margin:0 0 5px;font-size:15px;color:var(--ink-2)}
.phase{display:flex;align-items:center;gap:16px;margin:40px 0 28px}
.phase:first-child{margin-top:8px}
.phase::before,.phase::after{content:"";flex:1;height:1px;background:var(--rule)}
.phase h2{font-family:var(--display);font-size:20px;font-weight:650;margin:0;
  letter-spacing:-.01em;color:var(--ink-2);text-align:center}
.context{margin:28px 0 14px;font-family:var(--mono);font-size:11.5px;
  letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3)}
.step{background:var(--card);border:1px solid var(--rule);border-radius:14px;
  box-shadow:var(--shadow);padding:18px;margin-bottom:24px}
.step:target{outline:2px solid var(--signal);outline-offset:2px}
.head{display:flex;gap:14px;align-items:flex-start}
.num{flex:none;width:34px;height:34px;border-radius:50%;background:var(--soft);
  color:var(--ink);font-weight:650;font-size:15px;display:flex;align-items:center;
  justify-content:center;text-decoration:none}
.say{font-size:17px;line-height:1.45;margin:5px 0 0;flex:1;min-width:0}
.say strong{font-weight:650}
code{font-family:var(--mono);font-size:.86em;background:var(--soft);
  border:1px solid var(--rule);border-radius:4px;padding:1px 5px}
.say code{cursor:copy}
.say code.copied{border-color:var(--signal)}
.url{display:inline-block;margin-left:4px;padding:1px 8px;border-radius:6px;
  border:1px solid var(--rule);background:var(--soft);font-weight:600;
  text-decoration:none;overflow-wrap:anywhere}
.url:hover{border-color:var(--signal)}
.detail{font-size:15px;color:var(--ink-2);margin:6px 0 0 48px;max-width:62ch}
figure{margin:14px 0 0;position:relative}
figure img{display:block;width:100%;height:auto;border:1px solid var(--rule);
  border-radius:8px;cursor:zoom-in;background:#fff}
.note{margin:12px 0 0 48px;border-left:3px solid var(--signal);
  background:var(--signal-soft);padding:12px 16px;font-size:15px;border-radius:0 8px 8px 0}
dialog{border:0;padding:0;background:transparent;max-width:96vw;max-height:94vh;
  overflow:auto}
dialog::backdrop{background:rgba(8,10,14,.82)}
dialog img{display:block;max-width:96vw;max-height:94vh;cursor:zoom-in;border-radius:6px}
dialog.big img{max-width:none;max-height:none;cursor:zoom-out}
footer{max-width:1120px;margin:0 auto;padding:18px 28px 48px;
  font-family:var(--mono);font-size:11px;color:var(--ink-3);line-height:1.7}
footer div{margin-left:272px;max-width:720px;border-top:1px solid var(--rule);
  padding-top:18px}
@media (max-width:960px){
  .hero-inner,footer div{margin-left:0}
  .layout{grid-template-columns:minmax(0,1fr)}
  .layout.solo main{grid-column:auto}
  .toc{position:static}
  .toc-bar{display:none}
}
@media (max-width:640px){
  .hero{padding:40px 16px 4px}
  .theme{right:16px;top:12px}
  .layout{padding:24px 16px 64px}
  footer{padding:18px 16px 40px}
  .step{padding:14px}
  .detail,.note{margin-left:0}
}
@media print{
  :root{--bg:#fff;--card:#fff;--ink:#141821;--ink-2:#454d5d;--ink-3:#687083;
    --rule:#e2e5eb;--soft:#eef0f4;--signal:#d6006e;--shadow:none;color-scheme:light}
  .toc,.theme{display:none}
  .hero-inner,footer div{margin-left:0}
  .layout{display:block;padding:0}
  .hero,footer{padding-left:0;padding-right:0}
  .step{break-inside:avoid;page-break-inside:avoid}
}
"""

# Everything stays inline so the guide is one file you can email or drop on a
# share. No script is needed to read it; the script only adds conveniences.
JS = """
(function(){
  var root=document.documentElement, KEY="scribeling-theme";
  try{var saved=localStorage.getItem(KEY);if(saved)root.dataset.theme=saved;}catch(e){}
  var toggle=document.querySelector(".theme");
  function dark(){return root.dataset.theme?root.dataset.theme==="dark":
    matchMedia("(prefers-color-scheme: dark)").matches;}
  function paint(){toggle.textContent=dark()?"\\u2600":"\\u263E";
    toggle.title=dark()?"Light mode":"Dark mode";}
  paint();
  toggle.onclick=function(){root.dataset.theme=dark()?"light":"dark";
    try{localStorage.setItem(KEY,root.dataset.theme);}catch(e){} paint();};

  var dlg=document.getElementById("zoom"), big=dlg.querySelector("img");
  document.querySelectorAll("figure img").forEach(function(img){
    img.onclick=function(){big.src=img.src;big.alt=img.alt;
      dlg.classList.remove("big");dlg.showModal();};
  });
  big.onclick=function(e){e.stopPropagation();dlg.classList.toggle("big");};
  dlg.onclick=function(){dlg.close();};

  document.querySelectorAll(".say code").forEach(function(c){
    c.title="Click to copy";
    c.onclick=function(){if(!navigator.clipboard)return;
      navigator.clipboard.writeText(c.textContent).then(function(){
        c.classList.add("copied");setTimeout(function(){c.classList.remove("copied");},900);});};
  });

  var links=[].slice.call(document.querySelectorAll(".toc a"));
  if(!links.length)return;
  var phases=links.map(function(a){return document.getElementById(a.hash.slice(1));});
  var pos=document.querySelector(".toc-pos"), cur=0;
  function mark(i){cur=i;links.forEach(function(a,j){a.classList.toggle("on",j===i);});
    pos.textContent=i+1;}
  function spy(){var line=innerHeight*0.35, i=0;
    phases.forEach(function(p,j){if(p.getBoundingClientRect().top<line)i=j;});
    mark(i);}
  addEventListener("scroll",spy,{passive:true});spy();
  function go(d){var i=Math.max(0,Math.min(phases.length-1,cur+d));
    phases[i].scrollIntoView();mark(i);}
  document.querySelector(".toc-prev").onclick=function(){go(-1);};
  document.querySelector(".toc-next").onclick=function(){go(1);};
})();
"""

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{css}</style>
<header class="hero">
  <button class="theme" type="button" aria-label="Toggle dark mode"></button>
  <div class="hero-inner">
  <div class="eyebrow">Step-by-step guide</div>
  <h1>{title}</h1>
  {lede}
  <div class="meta">{meta}</div>
  {tags}
  </div>
</header>
<div class="layout{solo}">
{toc}
<main>
{prereqs}
{steps}
</main>
</div>
<footer><div>Recorded with scribeling. Screenshots come from a live session &mdash;
check them for anything that should not leave the room before you share this.</div></footer>
<dialog id="zoom" aria-label="Enlarged screenshot"><img alt=""></dialog>
<script>{js}</script>
</html>
"""


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def render_caption(text: str) -> str:
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith("**", i):
            end = text.find("**", i + 2)
            if end != -1:
                out.append("<strong>" + esc(text[i + 2:end]) + "</strong>")
                i = end + 2
                continue
        if text[i] == "`":
            end = text.find("`", i + 1)
            if end != -1:
                out.append("<code>" + esc(text[i + 1:end]) + "</code>")
                i = end + 1
                continue
        out.append(esc(text[i]))
        i += 1
    return "".join(out)


def render_url(url: str) -> str:
    """Only http(s) becomes a link - steps.json is hand-edited, and a
    javascript: URL in a guide that gets emailed around is a gift to someone."""
    url = (url or "").strip()
    if not re.match(r"https?://", url, re.I):
        return f"<code>{esc(url)}</code>" if url else ""
    return (f'<a class="url" href="{esc(url)}" target="_blank" '
            f'rel="noopener">{esc(url)} &#8599;</a>')


def parse_ts(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def duration_label(visible) -> str:
    """Wall-clock time from first to last recorded step, which is what a reader
    will actually spend. Falls back to a few seconds a step when timestamps are
    missing or the recording was paused for a coffee."""
    stamps = [t for t in (parse_ts(s.get("ts")) for s in visible) if t]
    seconds = (stamps[-1] - stamps[0]).total_seconds() if len(stamps) > 1 else 0
    if seconds <= 0 or seconds > len(visible) * 120:
        seconds = len(visible) * 6
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def build_html(session: Path, payload: dict) -> Path:
    """Render a session to a single self-contained HTML file.

    Recognised keys: title, description, author, tags (list of strings),
    prerequisites (list of strings), and steps. Each step may carry `phase` (a
    heading introducing the steps that follow), `detail` (one clarifying sentence
    under the instruction), `url` (rendered as a link after the caption),
    `caption`, `note` and `hidden`. Phases, when present, replace the automatic
    window-title context lines and drive the contents sidebar - a human-written
    grouping beats a guessed one.
    """
    shots = session / "shots"
    title = payload.get("title", "Untitled procedure")
    description = payload.get("description", "") or ""
    author = (payload.get("author") or "").strip()
    tags = [str(t).strip() for t in payload.get("tags", []) if str(t).strip()]
    prerequisites = [p for p in payload.get("prerequisites", []) if str(p).strip()]
    steps = payload.get("steps", [])

    visible = [s for s in steps if not s.get("hidden")]
    use_phases = any(s.get("phase") for s in visible)
    blocks, phases, seen_phase, seen_window = [], [], None, None

    for n, s in enumerate(visible, 1):
        if use_phases:
            phase = s.get("phase", "")
            if phase and phase != seen_phase:
                seen_phase = phase
                phases.append(phase)
                blocks.append(f'<div class="phase" id="stage-{len(phases)}">'
                              f'<h2>{esc(phase)}</h2></div>')
        else:
            window = s.get("window", "")
            if window and window != seen_window:
                seen_window = window
                blocks.append(f'<div class="context">{esc(window)}</div>')

        img_html = ""
        path = shots / s.get("image", "")
        if s.get("image") and path.exists():
            data = base64.b64encode(path.read_bytes()).decode()
            img_html = (f'<figure><img alt="Step {n} screenshot" loading="lazy" '
                        f'src="data:image/png;base64,{data}"></figure>')

        caption = s.get("caption", "").strip()
        if not caption and s.get("action") == "navigate":
            caption = "Navigate to"
        link = render_url(s.get("url", ""))
        say = " ".join(x for x in (render_caption(caption), link) if x)
        say_html = f'<p class="say">{say}</p>' if say else '<p class="say"></p>'
        detail = s.get("detail", "").strip()
        detail_html = (f'<p class="detail">{render_caption(detail)}</p>'
                       if detail else "")
        note = s.get("note", "") if s.get("action") == "note" else ""
        note_html = f'<div class="note">{render_caption(note)}</div>' if note else ""
        blocks.append(
            f'<section class="step" id="step-{n}"><div class="head">'
            f'<a class="num" href="#step-{n}">{n}</a>{say_html}</div>'
            f'{detail_html}{img_html}{note_html}</section>')

    toc = ""
    if len(phases) > 1:
        items = "".join(f'<li><a href="#stage-{i}">{esc(p)}</a></li>'
                        for i, p in enumerate(phases, 1))
        toc = ('<nav class="toc" aria-label="Stages"><div class="toc-bar">'
               '<button class="toc-prev" type="button" aria-label="Previous stage">'
               '&#9650;</button><span><b class="toc-pos">1</b> of '
               f'{len(phases)}</span><button class="toc-next" type="button" '
               f'aria-label="Next stage">&#9660;</button></div><ol>{items}</ol></nav>')

    # The capture date, not the rebuild date - a guide rebuilt next month was
    # still recorded when it was recorded.
    stamps = [t for t in (parse_ts(s.get("ts")) for s in steps) if t]
    when = (stamps[0] if stamps else datetime.now()).strftime("%d %b %Y")
    meta = []
    if author:
        meta.append(f"<b>{esc(author)}</b>")
    meta += [f"<span>{len(visible)} steps</span>",
             f"<span>{duration_label(visible)}</span>",
             f"<span>Captured {when}</span>"]

    lede = f'<p class="lede">{esc(description)}</p>' if description.strip() else ""
    tags_html = ("<div class=\"tags\">" + "".join(
        f'<span class="tag">{esc(t)}</span>' for t in tags) + "</div>") if tags else ""
    prereqs = ""
    if prerequisites:
        items = "".join(f"<li>{render_caption(str(p))}</li>" for p in prerequisites)
        prereqs = f'<div class="prereqs"><h2>Before you start</h2><ul>{items}</ul></div>'

    out = session / "guide.html"
    out.write_text(PAGE.format(
        title=esc(title), css=CSS, js=JS, lede=lede, meta="".join(meta),
        tags=tags_html, solo="" if toc else " solo", toc=toc, prereqs=prereqs,
        steps="\n".join(blocks),
    ), encoding="utf-8")
    return out


def save_session(session: Path, cfg: Config, steps):
    payload = {"title": cfg.title, "description": cfg.description,
               "author": cfg.author,
               "steps": [asdict(s) if isinstance(s, Step) else s for s in steps]}
    (session / "steps.json").write_text(json.dumps(payload, indent=2),
                                        encoding="utf-8")
    return payload


def reveal(path: Path):
    try:
        os.startfile(str(path))
    except Exception:
        subprocess.Popen(["explorer", str(path)])


def strip_marks(text: str) -> str:
    return text.replace("**", "").replace("`", "")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def run_gui(prefill: Config | None = None, edit: Path | None = None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    BG, FG, MUTED, ACCENT, SOFT = "#fbfbfc", "#141821", "#7b8496", "#d6006e", "#eef0f4"
    CARD = "#ffffff"

    root = tk.Tk()
    root.withdraw()
    try:
        root.tk.call("tk", "scaling",
                     ctypes.windll.user32.GetDpiForSystem() / 72.0)
    except Exception:
        pass

    state = {"cfg": None, "session": None}

    def shell(title, size):
        win = tk.Toplevel(root)
        win.title(title)
        win.configure(bg=BG)
        win.geometry(size)
        return win

    def primary(parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, relief="flat", bg=ACCENT,
                         fg="white", activebackground=ACCENT, activeforeground="white",
                         font=("Segoe UI", 11, "bold"), cursor="hand2", bd=0)

    def secondary(parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, relief="flat", bg=SOFT,
                         fg=FG, activebackground="#e2e5eb", font=("Segoe UI", 10), bd=0)

    # -- setup -------------------------------------------------------------

    def setup():
        win = shell("scribeling", "540x790")
        win.protocol("WM_DELETE_WINDOW", root.destroy)

        tk.Label(win, text="NEW RECORDING", bg=BG, fg=ACCENT,
                 font=("Consolas", 9)).pack(anchor="w", padx=28, pady=(26, 0))
        tk.Label(win, text="What are you documenting?", bg=BG, fg=FG,
                 font=("Segoe UI", 19, "bold")).pack(anchor="w", padx=28, pady=(4, 12))

        if not is_elevated():
            warn = tk.Frame(win, bg="#fdf0f6", highlightbackground=ACCENT,
                            highlightthickness=1)
            warn.pack(fill="x", padx=28, pady=(0, 14))
            tk.Label(warn, text="Not running as administrator", bg="#fdf0f6", fg=FG,
                     font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12,
                                                         pady=(9, 0))
            tk.Label(warn, text="Elevated apps will capture as screenshots with no "
                                "element names, so captions come out blank.",
                     bg="#fdf0f6", fg=MUTED, font=("Segoe UI", 9), wraplength=420,
                     justify="left").pack(anchor="w", padx=12)

            def elevate():
                if relaunch_as_admin():
                    root.destroy()
                else:
                    messagebox.showwarning("scribeling", "Elevation was declined.")
            tk.Button(warn, text="Restart as administrator", command=elevate,
                      relief="flat", bg=ACCENT, fg="white", bd=0, cursor="hand2",
                      font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=12,
                                                         pady=(6, 10), ipadx=10,
                                                         ipady=3)

        tk.Label(win, text="Title", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=28)
        title_var = tk.StringVar(value=(prefill.title if prefill else ""))
        entry = tk.Entry(win, textvariable=title_var, font=("Segoe UI", 11),
                         relief="solid", bd=1)
        entry.pack(fill="x", padx=28, pady=(4, 16), ipady=5)

        tk.Label(win, text="Author", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=28)
        author_var = tk.StringVar(value=(prefill.author if prefill and prefill.author
                                         else display_name()))
        tk.Entry(win, textvariable=author_var, font=("Segoe UI", 11),
                 relief="solid", bd=1).pack(fill="x", padx=28, pady=(4, 16), ipady=5)

        tk.Label(win, text="Description", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=28)
        tk.Label(win, text="Sits under the heading. Who it is for, when to use it.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=28)
        desc = tk.Text(win, height=5, font=("Segoe UI", 10), relief="solid", bd=1,
                       wrap="word")
        desc.pack(fill="x", padx=28, pady=(6, 16))
        if prefill and prefill.description:
            desc.insert("1.0", prefill.description)

        typing = tk.BooleanVar(value=True)
        mask = tk.BooleanVar(value=False)
        dim = tk.BooleanVar(value=False)
        box = tk.Frame(win, bg=BG)
        box.pack(fill="x", padx=26)
        for var, text in ((typing, "Record what I type"),
                          (mask, "Record that I typed, not the values"),
                          (dim, "Dim everything except the target")):
            tk.Checkbutton(box, text=text, variable=var, bg=BG, fg=FG, bd=0,
                           activebackground=BG, highlightthickness=0,
                           font=("Segoe UI", 10), anchor="w").pack(fill="x")

        outdir = tk.StringVar(value=str(prefill.outdir if prefill and prefill.outdir
                                        else Path.home() / "Documents" / "scribeling"))
        row = tk.Frame(win, bg=BG)
        row.pack(fill="x", padx=28, pady=(14, 0))
        tk.Label(row, textvariable=outdir, bg=BG, fg=MUTED, font=("Consolas", 8),
                 anchor="w").pack(side="left", fill="x", expand=True)

        def pick():
            chosen = filedialog.askdirectory(initialdir=outdir.get())
            if chosen:
                outdir.set(chosen)
        secondary(row, "Change", pick).pack(side="right", ipadx=8, ipady=2)

        def start():
            if not title_var.get().strip():
                messagebox.showwarning("scribeling", "Give it a title first.")
                return
            cfg = Config(title=title_var.get().strip(),
                         description=desc.get("1.0", "end").strip(),
                         author=author_var.get().strip(),
                         outdir=Path(outdir.get()), dim=dim.get(),
                         mask_typed=mask.get(), capture_typing=typing.get())
            session = cfg.outdir / datetime.now().strftime("%Y-%m-%d_%H%M%S")
            session.mkdir(parents=True, exist_ok=True)
            state["cfg"], state["session"] = cfg, session
            win.destroy()
            hud()

        primary(win, "Start recording", start).pack(fill="x", padx=28, pady=(20, 0),
                                                    ipady=9)
        tk.Label(win, text="F7 pause    F8 finish    F9 undo    F10 note",
                 bg=BG, fg=MUTED, font=("Consolas", 9)).pack(pady=(12, 0))
        tk.Button(win, text="Edit an existing recording…",
                  command=lambda: open_existing(win), relief="flat", bd=0, bg=BG,
                  fg=ACCENT, activebackground=BG, cursor="hand2",
                  font=("Segoe UI", 10, "underline")).pack(pady=(10, 0))
        entry.focus_set()

    # -- recording HUD -----------------------------------------------------

    def hud():
        cfg, session = state["cfg"], state["session"]
        win = shell("Recording", "400x460")
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", lambda: None)

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=20, pady=(18, 10))
        dot = tk.Label(head, text="\u25cf", bg=BG, fg=ACCENT, font=("Segoe UI", 13))
        dot.pack(side="left")
        status = tk.Label(head, text="Recording", bg=BG, fg=FG,
                          font=("Segoe UI", 12, "bold"))
        status.pack(side="left", padx=6)
        count = tk.Label(head, text="0 steps", bg=BG, fg=MUTED, font=("Consolas", 9))
        count.pack(side="right")

        listbox = tk.Listbox(win, font=("Segoe UI", 9), relief="solid", bd=1,
                             activestyle="none", highlightthickness=0)
        listbox.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        events: queue.Queue = queue.Queue()
        rec = Recorder(cfg, session, on_event=lambda k, p: events.put((k, p)))

        def line(step):
            return f" {step.index:>3}  {strip_marks(step.caption)}"

        def finish():
            win.destroy()
            payload = save_session(session, cfg, rec.shutdown())
            editor(session, payload)

        def pump():
            while True:
                try:
                    kind, payload = events.get_nowait()
                except queue.Empty:
                    break
                if kind == "step":
                    listbox.insert("end", line(payload))
                    listbox.see("end")
                elif kind == "amend":
                    if listbox.size():
                        listbox.delete("end")
                    listbox.insert("end", line(payload))
                elif kind == "drop":
                    if listbox.size():
                        listbox.delete("end")
                elif kind == "pause":
                    status.config(text="Paused" if payload else "Recording")
                    dot.config(fg=MUTED if payload else ACCENT)
                    pause_btn.config(text="Resume" if payload else "Pause")
                elif kind == "error":
                    listbox.insert("end", f"  !  {payload}")
                count.config(text=f"{listbox.size()} steps")
            if rec.stop.is_set():
                finish()
                return
            win.after(120, pump)

        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", padx=20, pady=(0, 18))
        pause_btn = secondary(bar, "Pause", rec.toggle_pause)
        pause_btn.pack(side="left", padx=(0, 6), ipadx=8, ipady=3)
        secondary(bar, "Undo", rec.drop_last).pack(side="left", padx=(0, 6),
                                                   ipadx=8, ipady=3)
        secondary(bar, "Note", rec.add_note).pack(side="left", ipadx=8, ipady=3)
        tk.Button(bar, text="Finish", command=rec.request_stop, relief="flat",
                  bg=ACCENT, fg="white", font=("Segoe UI", 10, "bold"), bd=0,
                  cursor="hand2").pack(side="right", ipadx=12, ipady=3)

        rec.start()
        win.after(200, pump)

    # -- editor ------------------------------------------------------------

    def editor(session: Path, payload: dict):
        """Every edit lands in payload in place. Deleting a step hides it rather
        than removing it, so screenshots and indexes never drift apart, and undo
        is just restoring a snapshot of the step list."""
        from PIL import Image, ImageTk

        steps = payload.setdefault("steps", [])
        history: list[str] = []
        rows: list[tuple] = []
        thumbs: dict = {}
        dirty = {"on": False, "quiet": False}

        win = shell("scribeling - edit guide", "1000x860")

        # -- header --------------------------------------------------------
        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=24, pady=(18, 0))
        tk.Label(head, text="EDIT GUIDE", bg=BG, fg=ACCENT,
                 font=("Consolas", 9)).pack(anchor="w")
        title_var = tk.StringVar(value=payload.get("title", ""))
        tk.Entry(head, textvariable=title_var, font=("Segoe UI", 17, "bold"),
                 relief="flat", bg=BG, fg=FG, bd=0).pack(fill="x", pady=(2, 4))
        meta = tk.Frame(head, bg=BG)
        meta.pack(fill="x")
        desc_var = tk.StringVar(value=payload.get("description", ""))
        author_var = tk.StringVar(value=payload.get("author", ""))
        for label, var, weight in (("Description", desc_var, 3),
                                   ("Author", author_var, 1)):
            cell = tk.Frame(meta, bg=BG)
            cell.pack(side="left", fill="x", expand=True, padx=(0, 10))
            tk.Label(cell, text=label, bg=BG, fg=MUTED,
                     font=("Segoe UI", 9)).pack(anchor="w")
            tk.Entry(cell, textvariable=var, font=("Segoe UI", 10), relief="solid",
                     bd=1, width=12 * weight).pack(fill="x", ipady=3)
        for var in (title_var, desc_var, author_var):
            var.trace_add("write", lambda *_: touch())

        # -- toolbar -------------------------------------------------------
        tools = tk.Frame(win, bg=BG)
        tools.pack(fill="x", padx=24, pady=(14, 8))
        count = tk.Label(tools, bg=BG, fg=MUTED, font=("Consolas", 9))
        count.pack(side="left")
        del_sel = tk.Button(tools, text="Delete selected", relief="flat", bd=0,
                            bg=SOFT, fg=ACCENT, font=("Segoe UI", 10, "bold"),
                            command=lambda: delete_selected(), cursor="hand2")
        del_sel.pack(side="right", ipadx=10, ipady=3)
        undo_btn = secondary(tools, "Undo", lambda: undo())
        undo_btn.pack(side="right", padx=6, ipadx=10, ipady=3)
        secondary(tools, "Select none",
                  lambda: [r[4].set(False) for r in rows]).pack(
                      side="right", ipadx=8, ipady=3)
        secondary(tools, "Select all",
                  lambda: [r[4].set(True) for r in rows]).pack(
                      side="right", padx=6, ipadx=8, ipady=3)
        tk.Label(win, text="Captions take **bold** and `code`. Stage groups steps "
                           "into sections; a step with no stage stays in the one "
                           "above it. Deleted steps can be brought back with Undo.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9), wraplength=940,
                 justify="left").pack(anchor="w", padx=24, pady=(0, 8))

        # -- footer --------------------------------------------------------
        bar = tk.Frame(win, bg=BG)
        bar.pack(side="bottom", fill="x", padx=24, pady=14)
        saved = tk.Label(bar, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        saved.pack(side="left")
        primary(bar, "Export guide", lambda: export()).pack(side="right",
                                                            ipadx=18, ipady=7)
        secondary(bar, "Save", lambda: save()).pack(side="right", padx=8,
                                                    ipadx=14, ipady=7)

        # -- scrolling list ------------------------------------------------
        holder = tk.Frame(win, bg=BG)
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(window_id, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(24, 0))
        scroll.pack(side="right", fill="y", padx=(0, 8))

        def wheel(e):
            delta = -1 if getattr(e, "num", 0) == 4 else 1 if getattr(e, "num", 0) == 5 \
                else int(-e.delta / 120)
            canvas.yview_scroll(delta, "units")
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind_all(seq, wheel)

        # -- state ---------------------------------------------------------

        def visible():
            return [s for s in steps if not s.get("hidden")]

        def touch():
            if dirty["quiet"]:
                return
            dirty["on"] = True
            saved.config(text="Unsaved changes")

        def sync():
            """Pull whatever is typed in the boxes back into the step dicts."""
            payload["title"] = title_var.get().strip() or "Untitled procedure"
            payload["description"] = desc_var.get().strip()
            payload["author"] = author_var.get().strip()
            for s, cap, det, stage, _ in rows:
                s["caption"] = cap.get("1.0", "end-1c").strip()
                for key, value in (("detail", det.get("1.0", "end-1c").strip()),
                                   ("phase", stage.get().strip())):
                    if value:
                        s[key] = value
                    else:
                        s.pop(key, None)

        def change(fn):
            sync()
            history.append(json.dumps(steps))
            del history[:-60]
            fn()
            touch()
            render()

        def undo():
            if not history:
                return
            steps[:] = json.loads(history.pop())
            touch()
            render()

        def delete(s):
            change(lambda: s.update(hidden=True))

        def delete_selected():
            picked = [r[0] for r in rows if r[4].get()]
            if picked:
                change(lambda: [s.update(hidden=True) for s in picked])

        def move(s, d):
            vis = visible()
            i = vis.index(s)
            if not 0 <= i + d < len(vis):
                return
            other = vis[i + d]

            def swap():
                a, b = steps.index(s), steps.index(other)
                steps[a], steps[b] = steps[b], steps[a]
            change(swap)

        def merge_up(s):
            """Fold this step into the one above: captions join, and the merged
            step keeps the later screenshot, since that shows the end state."""
            vis = visible()
            i = vis.index(s)
            if i == 0:
                return
            prev = vis[i - 1]

            def merge():
                a, b = prev.get("caption", "").strip(), s.get("caption", "").strip()
                if a and b:
                    b = b[0].lower() + b[1:]
                    prev["caption"] = f"{a.rstrip('.')}, then {b}"
                else:
                    prev["caption"] = a or b
                details = " ".join(x for x in (prev.get("detail", ""),
                                               s.get("detail", "")) if x.strip())
                if details:
                    prev["detail"] = details
                if s.get("image"):
                    prev["image"] = s["image"]
                s["hidden"] = True
            change(merge)

        def add_after(s):
            def add():
                top = max([x.get("index", 0) for x in steps] + [0]) + 1
                new = {"index": top, "action": "note", "caption": "",
                       "image": "", "phase": s.get("phase", "")}
                steps.insert(steps.index(s) + 1, new)
            change(add)

        def replace_image(s):
            chosen = filedialog.askopenfilename(
                parent=win, title="Choose a screenshot",
                filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif"),
                           ("All files", "*.*")])
            if not chosen:
                return
            try:
                im = Image.open(chosen).convert("RGB")
                if im.width > MAX_WIDTH:
                    im = im.resize((MAX_WIDTH, int(im.height * MAX_WIDTH / im.width)),
                                   Image.LANCZOS)
                shots = session / "shots"
                shots.mkdir(exist_ok=True)
                n = 1
                while (shots / f"step-{s.get('index', 0):03d}-alt{n}.png").exists():
                    n += 1
                name = f"step-{s.get('index', 0):03d}-alt{n}.png"
                im.save(shots / name, optimize=True)
            except Exception as exc:
                messagebox.showerror("scribeling", f"Could not use that image:\n{exc}",
                                     parent=win)
                return
            change(lambda: s.update(image=name))

        def remove_image(s):
            change(lambda: s.update(image=""))

        def preview(path: Path):
            top = tk.Toplevel(win)
            top.title(path.name)
            top.configure(bg="#111317")
            im = Image.open(path)
            im.thumbnail((int(win.winfo_screenwidth() * .85),
                          int(win.winfo_screenheight() * .8)))
            ph = ImageTk.PhotoImage(im)
            lbl = tk.Label(top, image=ph, bg="#111317", cursor="hand2")
            lbl.image = ph
            lbl.pack(padx=10, pady=10)
            lbl.bind("<Button-1>", lambda e: top.destroy())
            top.bind("<Escape>", lambda e: top.destroy())
            top.focus_set()

        def thumb(path: Path):
            key = (str(path), path.stat().st_mtime)
            if key not in thumbs:
                im = Image.open(path)
                im.thumbnail((360, 200))
                thumbs[key] = ImageTk.PhotoImage(im)
            return thumbs[key]

        # -- rendering -----------------------------------------------------

        def small(parent, text, cmd, fg=FG, state="normal"):
            b = tk.Button(parent, text=text, command=cmd, relief="flat", bd=0,
                          bg=SOFT, fg=fg, activebackground="#e2e5eb",
                          font=("Segoe UI", 9), cursor="hand2", state=state)
            b.pack(side="left", padx=(4, 0), ipadx=7, ipady=1)
            return b

        def labelled(parent, label, row):
            tk.Label(parent, text=label, bg=CARD, fg=MUTED, font=("Segoe UI", 9),
                     anchor="nw", width=7).grid(row=row, column=0, sticky="nw",
                                                pady=(4, 0))

        def render():
            dirty["quiet"] = True
            y = canvas.yview()[0]
            for child in inner.winfo_children():
                child.destroy()
            rows.clear()
            vis = visible()
            stages = []
            for s in vis:
                if s.get("phase") and s["phase"] not in stages:
                    stages.append(s["phase"])

            for n, s in enumerate(vis, 1):
                card = tk.Frame(inner, bg=CARD, highlightbackground="#e2e5eb",
                                highlightthickness=1)
                card.pack(fill="x", pady=(0, 12), padx=(0, 12))

                top = tk.Frame(card, bg=CARD)
                top.pack(fill="x", padx=12, pady=(10, 4))
                pick = tk.BooleanVar(value=False)
                tk.Checkbutton(top, variable=pick, bg=CARD, activebackground=CARD,
                               highlightthickness=0, bd=0).pack(side="left")
                tk.Label(top, text=str(n), bg=CARD, fg=ACCENT,
                         font=("Segoe UI", 12, "bold")).pack(side="left", padx=(4, 8))
                tk.Label(top, text=s.get("action", "click"), bg=CARD, fg=MUTED,
                         font=("Consolas", 9)).pack(side="left")
                tk.Button(top, text="✕ Delete", command=lambda s=s: delete(s),
                          relief="flat", bd=0, bg=CARD, fg=ACCENT, cursor="hand2",
                          activebackground="#fdf0f6",
                          font=("Segoe UI", 9, "bold")).pack(side="right",
                                                             padx=(8, 0), ipadx=6)
                tools = tk.Frame(top, bg=CARD)
                tools.pack(side="right")
                small(tools, "↑", lambda s=s: move(s, -1),
                      state="normal" if n > 1 else "disabled")
                small(tools, "↓", lambda s=s: move(s, 1),
                      state="normal" if n < len(vis) else "disabled")
                small(tools, "Merge up", lambda s=s: merge_up(s),
                      state="normal" if n > 1 else "disabled")
                small(tools, "Image…", lambda s=s: replace_image(s))
                if s.get("image"):
                    small(tools, "No image", lambda s=s: remove_image(s))
                small(tools, "+ Step below", lambda s=s: add_after(s))

                body = tk.Frame(card, bg=CARD)
                body.pack(fill="x", padx=(40, 14), pady=(0, 10))
                body.columnconfigure(1, weight=1)

                labelled(body, "Step", 0)
                cap = tk.Text(body, height=2, wrap="word", font=("Segoe UI", 10),
                              relief="solid", bd=1, undo=True)
                cap.insert("1.0", s.get("caption", ""))
                cap.grid(row=0, column=1, sticky="ew", pady=2)

                labelled(body, "Detail", 1)
                det = tk.Text(body, height=2, wrap="word", font=("Segoe UI", 10),
                              relief="solid", bd=1, undo=True)
                det.insert("1.0", s.get("detail", ""))
                det.grid(row=1, column=1, sticky="ew", pady=2)

                labelled(body, "Stage", 2)
                stage = ttk.Combobox(body, values=stages, font=("Segoe UI", 10))
                stage.set(s.get("phase", ""))
                stage.grid(row=2, column=1, sticky="ew", pady=2)
                for w in (cap, det):
                    w.bind("<<Modified>>",
                           lambda e: (touch(), e.widget.edit_modified(False)))
                stage.bind("<KeyRelease>", lambda e: touch())
                stage.bind("<<ComboboxSelected>>", lambda e: touch())

                where = "   ".join(x for x in (s.get("window", ""), s.get("target", ""))
                                   if x)
                if s.get("url"):
                    where = (where + "   " if where else "") + s["url"]
                if where:
                    tk.Label(body, text=where, bg=CARD, fg="#4a6fa5",
                             font=("Consolas", 9), anchor="w").grid(
                                 row=3, column=1, sticky="w", pady=(6, 0))

                path = session / "shots" / s.get("image", "")
                if s.get("image") and path.is_file():
                    try:
                        ph = thumb(path)
                        lbl = tk.Label(body, image=ph, bg=CARD, cursor="hand2",
                                       highlightbackground="#e2e5eb",
                                       highlightthickness=1)
                        lbl.grid(row=4, column=1, sticky="w", pady=(8, 0))
                        lbl.bind("<Button-1>", lambda e, p=path: preview(p))
                    except Exception:
                        pass

                rows.append((s, cap, det, stage, pick))

            if not vis:
                tk.Label(inner, text="No steps left. Undo brings deleted ones back.",
                         bg=BG, fg=MUTED, font=("Segoe UI", 11)).pack(pady=40)
            hidden = len(steps) - len(vis)
            count.config(text=f"{len(vis)} steps" +
                         (f"  ·  {hidden} deleted" if hidden else ""))
            undo_btn.config(state="normal" if history else "disabled")
            win.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.yview_moveto(y)
            win.after_idle(lambda: dirty.update(quiet=False))

        # -- saving --------------------------------------------------------

        def save():
            sync()
            (session / "steps.json").write_text(json.dumps(payload, indent=2),
                                                encoding="utf-8")
            dirty["on"] = False
            saved.config(text=f"Saved {datetime.now():%H:%M}")

        def export():
            save()
            out = build_html(session, payload)
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                win.unbind_all(seq)
            win.destroy()
            done(out, session, payload)

        def close():
            if dirty["on"]:
                answer = messagebox.askyesnocancel(
                    "scribeling", "Save your changes before closing?", parent=win)
                if answer is None:
                    return
                if answer:
                    save()
            root.destroy()

        def key_undo(e):
            # Inside a text box Ctrl+Z undoes typing; everywhere else it undoes
            # the last step edit.
            if isinstance(win.focus_get(), (tk.Text, tk.Entry, ttk.Entry)):
                return
            undo()

        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Control-z>", key_undo)
        render()

    def open_existing(parent):
        chosen = filedialog.askdirectory(
            parent=parent, title="Choose a recording folder",
            initialdir=str(Path.home() / "Documents" / "scribeling"))
        if not chosen:
            return False
        session = Path(chosen)
        if not (session / "steps.json").is_file():
            messagebox.showwarning("scribeling", "That folder has no steps.json. "
                                   "Pick the folder for one recording, the one "
                                   "with a shots folder inside.", parent=parent)
            return False
        payload = json.loads((session / "steps.json").read_text(encoding="utf-8"))
        parent.destroy()
        editor(session, payload)
        return True

    # -- done --------------------------------------------------------------

    def done(out: Path, session: Path, payload: dict):
        win = shell("Done", "520x240")
        win.protocol("WM_DELETE_WINDOW", root.destroy)
        tk.Label(win, text="EXPORTED", bg=BG, fg=ACCENT,
                 font=("Consolas", 9)).pack(pady=(36, 0))
        tk.Label(win, text=out.name, bg=BG, fg=FG,
                 font=("Segoe UI", 16, "bold")).pack(pady=(4, 2))
        tk.Label(win, text=str(out.parent), bg=BG, fg=MUTED, font=("Consolas", 8),
                 wraplength=460).pack()
        bar = tk.Frame(win, bg=BG)
        bar.pack(pady=24)
        tk.Button(bar, text="Open guide", command=lambda: reveal(out), relief="flat",
                  bg=ACCENT, fg="white", font=("Segoe UI", 10, "bold"), bd=0,
                  cursor="hand2").pack(side="left", padx=4, ipadx=14, ipady=6)
        secondary(bar, "Keep editing",
                  lambda: (win.destroy(), editor(session, payload))).pack(
                      side="left", padx=4, ipadx=10, ipady=6)
        secondary(bar, "Open folder",
                  lambda: reveal(out.parent)).pack(side="left", padx=4,
                                                   ipadx=10, ipady=6)
        secondary(bar, "Record another",
                  lambda: (win.destroy(), setup())).pack(side="left", padx=4,
                                                         ipadx=10, ipady=6)

    if edit is not None:
        editor(edit, json.loads((edit / "steps.json").read_text(encoding="utf-8")))
    else:
        setup()
    root.mainloop()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_record(args):
    cfg = Config(title=args.title, description=args.description or "",
                 author=args.author if args.author is not None else display_name(),
                 outdir=Path(args.outdir), pad=args.pad, full_frames=args.full,
                 dim=args.dim, truecolor=args.truecolor,
                 mask_typed=args.mask_typed, capture_typing=not args.no_typing)
    session = cfg.outdir / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    session.mkdir(parents=True, exist_ok=True)

    def show(kind, payload):
        if kind == "step":
            print(f"  {payload.index:>3}. {strip_marks(payload.caption)}")
        elif kind == "error":
            print(f"  ! {payload}")

    rec = Recorder(cfg, session, on_event=show)
    print(f'Recording "{cfg.title}".  F7 pause  F8 finish  F9 undo  F10 note\n')
    rec.start()
    while not rec.stop.is_set():
        time.sleep(0.2)
    steps = rec.shutdown()
    payload = save_session(session, cfg, steps)
    out = build_html(session, payload)
    print(f"\n{len(steps)} steps -> {out}")


def cmd_rebuild(args):
    session = Path(args.session)
    payload = json.loads((session / "steps.json").read_text(encoding="utf-8"))
    if args.title:
        payload["title"] = args.title
    if args.author is not None:
        payload["author"] = args.author
    out = build_html(session, payload)
    print(f"Rebuilt -> {out}")


def cmd_edit(args):
    session = Path(args.session)
    if not (session / "steps.json").is_file():
        sys.exit(f"No steps.json in {session}")
    run_gui(edit=session)


def cmd_gui(args):
    run_gui(Config(title=args.title or "", description=args.description or "",
                   author=args.author or "",
                   outdir=Path(args.outdir) if args.outdir else None))


def main():
    bootstrap()
    if len(sys.argv) == 1:
        run_gui()
        return

    p = argparse.ArgumentParser(prog="scribeling")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--title", default="Untitled procedure")
    r.add_argument("--description", default="")
    r.add_argument("--author", default=None)
    r.add_argument("--outdir", default="guides")
    r.add_argument("--pad", type=int, default=DEFAULT_PAD)
    r.add_argument("--full", action="store_true")
    r.add_argument("--dim", action="store_true")
    r.add_argument("--truecolor", action="store_true")
    r.add_argument("--mask-typed", action="store_true")
    r.add_argument("--no-typing", action="store_true")
    r.set_defaults(func=cmd_record)

    g = sub.add_parser("gui", help="open the GUI with fields prefilled")
    g.add_argument("--title", default="")
    g.add_argument("--description", default="")
    g.add_argument("--author", default="")
    g.add_argument("--outdir", default="")
    g.set_defaults(func=cmd_gui)

    e = sub.add_parser("edit", help="open a recording in the step editor")
    e.add_argument("session")
    e.set_defaults(func=cmd_edit)

    b = sub.add_parser("rebuild")
    b.add_argument("session")
    b.add_argument("--title", default=None)
    b.add_argument("--author", default=None)
    b.set_defaults(func=cmd_rebuild)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
