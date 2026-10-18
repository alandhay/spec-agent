"""Short real-keyboard check of the release build (takes the focus for a few seconds)."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import win32gui, win32api, win32con
import specbot
from tests import decode

def piece(state):
    cells = [(x, y) for y, row in enumerate(state["field"]) for x, ch in enumerate(row) if ch.isupper() and ch != "x"]
    return (min(x for x, _ in cells), min(y for _, y in cells), len(cells)) if cells else None

prev = win32gui.GetForegroundWindow()
emu = specbot.open_emulator(str(Path(__file__).resolve().parents[1] / "specbot.toml"))
log = []
try:
    emu.press("ENTER"); emu.wait_frames(40)
    s = decode(emu); log.append(("start", piece(s), s["text"][5].strip()[:6]))
    emu.press("O"); emu.press("O"); emu.wait_frames(3)
    s = decode(emu); log.append(("O O (left 2)", piece(s)))
    emu.press("P", hold_frames=30); emu.wait_frames(3)
    s = decode(emu); log.append(("P held (right, auto-repeat)", piece(s)))
    emu.press("5"); emu.press("5"); emu.wait_frames(3)
    s = decode(emu); log.append(("5 5 (left 2)", piece(s)))
    emu.press("Q"); emu.wait_frames(3)
    s = decode(emu); log.append(("Q (rotate)", piece(s), [r for r in s["field"] if any(c.isupper() for c in r)]))
    emu.press("W"); emu.wait_frames(3)
    s = decode(emu); log.append(("W (rotate back)", piece(s), [r for r in s["field"] if any(c.isupper() for c in r)]))
    emu.press("H"); emu.wait_frames(60)
    s1 = decode(emu); emu.wait_frames(60); s2 = decode(emu)
    log.append(("H (pause)", piece(s1), piece(s2), s1["text"][19][:10]))
    emu.press("H"); emu.wait_frames(3)
    emu.press("A", hold_frames=20); emu.wait_frames(3)
    s = decode(emu); log.append(("A held (soft drop)", piece(s), s["text"][5].strip()[:6]))
    emu.press("SPACE"); emu.wait_frames(10)
    s = decode(emu); log.append(("SPACE (hard drop)", s["text"][5].strip()[:6], s["field"][-3:]))
    emu.press("0"); emu.wait_frames(10)
    s = decode(emu); log.append(("0 (hard drop)", s["text"][5].strip()[:6], s["field"][-4:]))
    s["image"].resize((640, 480), 0).save(Path(__file__).parent / "build" / "play.png")
finally:
    emu.close()
    try:
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0); win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(prev)
    except Exception as exc:
        print("could not restore focus:", exc)
for entry in log:
    print(entry)
