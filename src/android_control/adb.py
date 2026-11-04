"""Thin, dependency-light wrapper around `adb` for driving a real Android device.

Everything here is synchronous and talks to the device through the adb binary.
Coordinates are always *device pixels in the current orientation* — the same
space `input tap` uses and the same size `screencap` produces.
"""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image


class AdbError(RuntimeError):
    pass


# --------------------------------------------------------------------------- adb discovery

_CANDIDATES = [
    "{ANDROID_HOME}/platform-tools/adb",
    "{ANDROID_SDK_ROOT}/platform-tools/adb",
    "~/Library/Android/sdk/platform-tools/adb",
    "~/Android/Sdk/platform-tools/adb",
    "/opt/homebrew/share/android-commandlinetools/platform-tools/adb",
    "/usr/local/share/android-commandlinetools/platform-tools/adb",
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
]


def find_adb() -> str:
    env = os.environ.get("ADB")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("adb")
    if on_path:
        return on_path
    for c in _CANDIDATES:
        try:
            p = Path(os.path.expanduser(c.format(**os.environ)))
        except KeyError:
            continue
        if p.exists():
            return str(p)
    raise AdbError(
        "adb not found. Install platform-tools (`brew install --cask android-platform-tools`) "
        "or set the ADB environment variable to the adb binary."
    )


# --------------------------------------------------------------------------- key names

KEY_ALIASES = {
    "back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER",
    "return": "KEYCODE_ENTER", "tab": "KEYCODE_TAB", "space": "KEYCODE_SPACE",
    "delete": "KEYCODE_DEL", "backspace": "KEYCODE_DEL", "del": "KEYCODE_DEL",
    "forward_delete": "KEYCODE_FORWARD_DEL", "escape": "KEYCODE_ESCAPE", "esc": "KEYCODE_ESCAPE",
    "menu": "KEYCODE_MENU", "recents": "KEYCODE_APP_SWITCH", "app_switch": "KEYCODE_APP_SWITCH",
    "power": "KEYCODE_POWER", "wakeup": "KEYCODE_WAKEUP", "sleep": "KEYCODE_SLEEP",
    "volume_up": "KEYCODE_VOLUME_UP", "volume_down": "KEYCODE_VOLUME_DOWN",
    "mute": "KEYCODE_VOLUME_MUTE", "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT", "center": "KEYCODE_DPAD_CENTER",
    "search": "KEYCODE_SEARCH", "camera": "KEYCODE_CAMERA", "notifications": "KEYCODE_NOTIFICATION",
    "move_home": "KEYCODE_MOVE_HOME", "move_end": "KEYCODE_MOVE_END", "page_up": "KEYCODE_PAGE_UP",
    "page_down": "KEYCODE_PAGE_DOWN", "select_all": "KEYCODE_A",  # combine with ctrl
}


def keycode(name: str) -> str:
    n = str(name).strip()
    if n.isdigit():
        return n
    low = n.lower()
    if low in KEY_ALIASES:
        return KEY_ALIASES[low]
    up = n.upper()
    return up if up.startswith("KEYCODE_") else "KEYCODE_" + up


# --------------------------------------------------------------------------- UI tree

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass
class UINode:
    index: int  # position in document order (stable within one dump)
    depth: int
    cls: str
    text: str
    resource_id: str
    desc: str
    package: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    long_clickable: bool
    scrollable: bool
    checkable: bool
    checked: bool
    enabled: bool
    focused: bool
    selected: bool
    password: bool
    hint: str = ""
    children: list["UINode"] = field(default_factory=list)

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def short_class(self) -> str:
        return self.cls.rsplit(".", 1)[-1]

    @property
    def label(self) -> str:
        return self.text or self.desc or self.hint

    def is_interesting(self) -> bool:
        return bool(
            self.text or self.desc or self.hint or self.clickable or self.long_clickable
            or self.scrollable or self.checkable or self.focused
            or (self.resource_id and not self.resource_id.startswith("android:id/") and not self.children)
            or "EditText" in self.cls
        )

    def describe(self) -> str:
        parts = [f"#{self.index}", self.short_class or "?"]
        if self.text:
            parts.append(f'text="{_clip(self.text)}"')
        if self.desc and self.desc != self.text:
            parts.append(f'desc="{_clip(self.desc)}"')
        if self.hint and self.hint not in (self.text, self.desc):
            parts.append(f'hint="{_clip(self.hint)}"')
        if self.resource_id:
            parts.append("id=" + self.resource_id.split(":id/", 1)[-1])
        x1, y1, x2, y2 = self.bounds
        cx, cy = self.center
        parts.append(f"[{x1},{y1},{x2},{y2}] @({cx},{cy})")
        flags = []
        for name in ("clickable", "long_clickable", "scrollable", "checkable", "focused", "selected", "password"):
            if getattr(self, name):
                flags.append(name)
        if self.checkable:
            flags.append("checked" if self.checked else "unchecked")
        if not self.enabled:
            flags.append("DISABLED")
        if flags:
            parts.append(" ".join(flags))
        return " ".join(parts)


def _clip(s: str, n: int = 120) -> str:
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_ui_xml(xml: str) -> list[UINode]:
    """Parse a uiautomator dump into a flat list of nodes (document order)."""
    root = ET.fromstring(xml)
    out: list[UINode] = []

    def walk(el: ET.Element, depth: int, parent: UINode | None) -> None:
        for child in el:
            if child.tag != "node":
                continue
            a = child.attrib
            m = _BOUNDS_RE.match(a.get("bounds", ""))
            b = tuple(int(v) for v in m.groups()) if m else (0, 0, 0, 0)
            node = UINode(
                index=len(out), depth=depth, cls=a.get("class", ""), text=a.get("text", ""),
                resource_id=a.get("resource-id", ""), desc=a.get("content-desc", ""),
                package=a.get("package", ""), bounds=b,  # type: ignore[arg-type]
                clickable=a.get("clickable") == "true", long_clickable=a.get("long-clickable") == "true",
                scrollable=a.get("scrollable") == "true", checkable=a.get("checkable") == "true",
                checked=a.get("checked") == "true", enabled=a.get("enabled") != "false",
                focused=a.get("focused") == "true", selected=a.get("selected") == "true",
                password=a.get("password") == "true", hint=a.get("hint", ""),
            )
            out.append(node)
            if parent is not None:
                parent.children.append(node)
            walk(child, depth + 1, node)

    walk(root, 0, None)
    return out


@dataclass
class Selector:
    text: str | None = None          # exact match (case-insensitive) on text or content-desc
    text_contains: str | None = None  # substring (case-insensitive) on text / desc / hint
    text_regex: str | None = None     # regex search on text / desc / hint
    resource_id: str | None = None    # full id or the part after ":id/"
    class_name: str | None = None     # full or short class name
    clickable: bool | None = None
    package: str | None = None

    def empty(self) -> bool:
        return all(v is None for v in vars(self).values())

    def matches(self, n: UINode) -> bool:
        fields = [n.text, n.desc, n.hint]
        if self.text is not None and not any(f.strip().lower() == self.text.strip().lower() for f in fields if f):
            return False
        if self.text_contains is not None and not any(self.text_contains.lower() in f.lower() for f in fields if f):
            return False
        if self.text_regex is not None:
            rx = re.compile(self.text_regex)
            if not any(rx.search(f) for f in fields if f):
                return False
        if self.resource_id is not None:
            rid = self.resource_id
            if not (n.resource_id == rid or n.resource_id.endswith(":id/" + rid)):
                return False
        if self.class_name is not None:
            if not (n.cls == self.class_name or n.short_class == self.class_name):
                return False
        if self.clickable is not None and n.clickable != self.clickable:
            return False
        if self.package is not None and n.package != self.package:
            return False
        return True

    def __str__(self) -> str:
        return ", ".join(f"{k}={v!r}" for k, v in vars(self).items() if v is not None) or "<empty>"


# --------------------------------------------------------------------------- device

class Device:
    def __init__(self, serial: str | None = None, adb: str | None = None):
        self.adb = adb or find_adb()
        self.serial = serial

    # ---- low level
    def _base(self) -> list[str]:
        return [self.adb] + (["-s", self.serial] if self.serial else [])

    def run(self, *args: str, timeout: float = 30, check: bool = True, binary: bool = False,
            input: bytes | None = None):
        cmd = self._base() + list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input)
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb timed out after {timeout}s: {' '.join(args)[:200]}") from e
        if check and p.returncode != 0:
            err = (p.stderr or b"").decode(errors="replace").strip() or (p.stdout or b"").decode(errors="replace").strip()
            raise AdbError(f"adb {' '.join(args)[:200]} failed ({p.returncode}): {err[:2000]}")
        if binary:
            return p.stdout
        return (p.stdout or b"").decode(errors="replace")

    def shell(self, command: str, timeout: float = 30, check: bool = False) -> str:
        """Run a shell command string on the device; returns combined stdout+stderr."""
        cmd = self._base() + ["shell", command]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"shell command timed out after {timeout}s: {command[:200]}") from e
        out = p.stdout.decode(errors="replace")
        if p.returncode != 0 and (check or _looks_like_adb_failure(out)):
            raise AdbError(f"`{command[:200]}` failed ({p.returncode}): {out.strip()[:2000]}")
        return out

    # ---- device selection
    @classmethod
    def list_devices(cls, adb: str | None = None) -> list[dict]:
        adb = adb or find_adb()
        out = subprocess.run([adb, "devices", "-l"], capture_output=True, timeout=15).stdout.decode()
        devs = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            d = {"serial": parts[0], "state": parts[1] if len(parts) > 1 else "?"}
            for kv in parts[2:]:
                if ":" in kv:
                    k, v = kv.split(":", 1)
                    d[k] = v
            devs.append(d)
        return devs

    def ensure_ready(self) -> None:
        devs = Device.list_devices(self.adb)
        if self.serial:
            match = [d for d in devs if d["serial"] == self.serial]
            if not match:
                raise AdbError(f"device {self.serial} not connected. Connected: {_fmt_devs(devs)}")
            st = match[0]["state"]
        else:
            ready = [d for d in devs if d["state"] == "device"]
            if not devs:
                raise AdbError(
                    "No Android device connected. Plug in via USB, enable Developer options > USB debugging, "
                    "and accept the 'Allow USB debugging' prompt on the device."
                )
            if len(ready) > 1:
                # Prefer real hardware: if exactly one physical (USB) device is ready, use it.
                physical = [d for d in ready if not d["serial"].startswith("emulator-") and "usb" in d]
                if len(physical) == 1:
                    self.serial = physical[0]["serial"]
                    return
                raise AdbError(f"Multiple devices connected; pick one with select_device. {_fmt_devs(devs)}")
            if not ready:
                st = devs[0]["state"]
            else:
                self.serial = ready[0]["serial"]
                return
        if st == "unauthorized":
            raise AdbError("Device is unauthorized: unlock it and accept the 'Allow USB debugging' prompt.")
        if st != "device":
            raise AdbError(f"Device state is '{st}', expected 'device'.")

    # ---- info
    def screen_size(self) -> tuple[int, int]:
        """Size in the *current orientation* (matches screencap / input coordinates)."""
        out = self.shell("wm size; dumpsys input | grep -m1 -E 'SurfaceOrientation|orientation='")
        if lf := re.search(r"logicalFrame=\[-?\d+, -?\d+, (\d+), (\d+)\]", out):
            return int(lf[1]), int(lf[2])
        m =re.search(r"Override size: (\d+)x(\d+)", out) or re.search(r"Physical size: (\d+)x(\d+)", out)
        if not m:
            return self.screenshot_image().size
        w, h = int(m[1]), int(m[2])
        r = re.search(r"(?:SurfaceOrientation|orientation)[:=]\s*(\d)", out)
        if r and int(r[1]) in (1, 3):
            w, h = h, w
        return w, h

    def info(self) -> dict:
        props = self.shell(
            "getprop ro.product.manufacturer; getprop ro.product.model; getprop ro.build.version.release; "
            "getprop ro.build.version.sdk; getprop ro.product.cpu.abi; wm size; wm density; "
            "dumpsys power | grep -m1 -E 'mWakefulness='; "
            "dumpsys window | grep -m1 -E 'mCurrentFocus'; "
            "dumpsys window | grep -m1 -oE 'mRotation=[0-9]|mDisplayRotation=ROTATION_[0-9]+'; "
            "dumpsys battery | grep -m1 -E ' level:'; "
            "dumpsys window | grep -m1 -E 'mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen'"
        ).splitlines()
        props = [p.strip() for p in props]
        info = {
            "serial": self.serial,
            "manufacturer": props[0] if len(props) > 0 else "",
            "model": props[1] if len(props) > 1 else "",
            "android": props[2] if len(props) > 2 else "",
            "sdk": props[3] if len(props) > 3 else "",
            "abi": props[4] if len(props) > 4 else "",
        }
        rest = "\n".join(props[5:])
        if m := re.search(r"Physical size: (\d+)x(\d+)", rest):
            info["physical_size"] = f"{m[1]}x{m[2]}"
        if m := re.search(r"Override size: (\d+)x(\d+)", rest):
            info["override_size"] = f"{m[1]}x{m[2]}"
        if m := re.search(r"Physical density: (\d+)", rest):
            info["density"] = int(m[1])
        if m := re.search(r"Override density: (\d+)", rest):
            info["override_density"] = int(m[1])
        if m := re.search(r"mWakefulness=(\w+)", rest):
            info["wakefulness"] = m[1]
        if m := re.search(r"level: (\d+)", rest):
            info["battery"] = int(m[1])
        if m := re.search(r"mRotation=(\d)|ROTATION_(\d+)", rest):
            info["rotation"] = int(m[1]) if m[1] else int(m[2]) // 90
        info["locked"] = self.is_locked()
        info["focus"] = self.current_focus()
        w, h = self.screen_size()
        info["screen_size_current_orientation"] = f"{w}x{h}"
        return info

    def current_focus(self) -> str:
        out = self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -2")
        m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^}\s]+)\}", out)
        if m:
            return m[1]
        m = re.search(r"ActivityRecord\{\S+ \S+ ([^\s}]+)", out)
        return m[1] if m else out.strip()

    # ---- screen
    def screenshot_png(self) -> bytes:
        data = self.run("exec-out", "screencap", "-p", binary=True, timeout=20)
        if not data.startswith(b"\x89PNG"):
            # some devices (old adb) mangle \n -> \r\n; fall back to file transfer
            self.shell("screencap -p /data/local/tmp/_ac_screen.png")
            data = self.run("exec-out", "cat", "/data/local/tmp/_ac_screen.png", binary=True)
            if not data.startswith(b"\x89PNG"):
                raise AdbError("screencap did not return a PNG (screen may be secure/DRM-protected)")
        return data

    def screenshot_image(self) -> Image.Image:
        return Image.open(io.BytesIO(self.screenshot_png())).convert("RGB")

    # ---- UI hierarchy
    _DUMPER_LOCAL = Path(__file__).with_name("assets") / "ac-dump.jar"
    _DUMPER_REMOTE = "/data/local/tmp/ac-dump.jar"

    def _ensure_dumper(self) -> bool:
        if getattr(self, "_dumper_ok", None) is not None:
            return self._dumper_ok
        ok = False
        if self._DUMPER_LOCAL.exists():
            size = self._DUMPER_LOCAL.stat().st_size
            remote = self.shell(f"stat -c %s {self._DUMPER_REMOTE} 2>/dev/null").strip()
            if remote != str(size):
                self.run("push", str(self._DUMPER_LOCAL), self._DUMPER_REMOTE, timeout=30, check=False)
            ok = self.shell(f"stat -c %s {self._DUMPER_REMOTE} 2>/dev/null").strip() == str(size)
        self._dumper_ok = ok
        return ok

    @staticmethod
    def _extract_xml(out: str) -> str | None:
        start = out.find("<?xml")
        if start < 0:
            start = out.find("<hierarchy")
        end = out.rfind("</hierarchy>")
        if start >= 0 and end > start:
            return out[start: end + len("</hierarchy>")]
        return None

    def ui_xml(self, idle_timeout_ms: int = 1500) -> str:
        """Dump the UI hierarchy. Uses a bundled dumper that tolerates never-idle screens
        (animations), falling back to stock `uiautomator dump`."""
        errors = []
        if self._ensure_dumper():
            f = "/data/local/tmp/_ac_ui.xml"
            for _ in range(2):
                out = self.shell(
                    f"rm -f {f}; CLASSPATH={self._DUMPER_REMOTE}:/system/framework/uiautomator.jar "
                    f"app_process /system/bin AcDump {int(idle_timeout_ms)} {f} 2>&1 >/dev/null | grep -v WARN; "
                    f"cat {f} 2>/dev/null", timeout=30)
                xml = self._extract_xml(out)
                if xml:
                    return xml
                errors.append(out.strip()[:300])
                time.sleep(0.3)
        out = self.run("exec-out", "uiautomator", "dump", "/dev/tty", timeout=40, check=False)
        xml = self._extract_xml(out)
        if xml:
            return xml
        errors.append(out.strip()[:300])
        raise AdbError(
            "UI hierarchy dump failed (another UiAutomation client such as Appium/uiautomator2 may hold the "
            "connection, or the window is secure). Use screenshot instead. Details: " + " | ".join(errors)
        )

    def ui_nodes(self) -> list[UINode]:
        return parse_ui_xml(self.ui_xml())

    def find(self, sel: Selector, nodes: list[UINode] | None = None) -> list[UINode]:
        if sel.empty():
            raise AdbError("empty selector: give text, text_contains, text_regex, resource_id or class_name")
        nodes = self.ui_nodes() if nodes is None else nodes
        return [n for n in nodes if sel.matches(n)]

    def wait_for(self, sel: Selector, timeout: float = 10, gone: bool = False, interval: float = 0.3) -> list[UINode]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                found = self.find(sel)
            except AdbError:
                found = None
            if found is not None and (bool(found) != gone):
                return found
            if time.monotonic() >= deadline:
                state = "still present" if gone else "not found"
                raise AdbError(f"timeout after {timeout}s: element {state} ({sel})")
            time.sleep(interval)

    # ---- input
    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}", check=True)

    def double_tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}; input tap {int(x)} {int(y)}", check=True)

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.shell(f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(duration_ms)}", check=True)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}", check=True)

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 1000) -> None:
        # `input draganddrop` exists on API 24+, holds before moving so long-press drag works
        out = self.shell(f"input draganddrop {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")
        if "Error" in out or "Unknown" in out:
            self.swipe(x1, y1, x2, y2, duration_ms)

    def key(self, *names: str, longpress: bool = False) -> None:
        codes = [keycode(n) for n in names]
        flag = "--longpress " if longpress else ""
        self.shell(f"input keyevent {flag}{' '.join(codes)}", check=True)

    def key_combo(self, *names: str) -> None:
        """Press keys simultaneously, e.g. ('ctrl_left', 'a'). API 31+ (`input keycombination`)."""
        codes = [keycode(n) for n in names]
        self.shell(f"input keycombination {' '.join(codes)}", check=True)

    def type_text(self, text: str) -> None:
        """Type text into the focused field. Newlines are sent as ENTER."""
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                if not line.isascii():
                    raise AdbError(
                        "`input text` only supports ASCII. For non-ASCII text set it via the app "
                        "(e.g. a debug intent) or an IME such as ADBKeyboard."
                    )
                # `input text` treats %s as space; escape literal % first is not supported by
                # input, so split around spaces instead and send KEYCODE_SPACE between words.
                chunks = line.split(" ")
                cmds = []
                for j, chunk in enumerate(chunks):
                    if chunk:
                        cmds.append("input text " + shlex.quote(chunk))
                    if j < len(chunks) - 1:
                        cmds.append("input keyevent KEYCODE_SPACE")
                # batch to keep it fast; device shell runs them sequentially
                for k in range(0, len(cmds), 40):
                    self.shell("; ".join(cmds[k:k + 40]), check=True)
            if i < len(lines) - 1:
                self.key("ENTER")

    def clear_text(self, max_chars: int = 200) -> None:
        """Clear the focused text field (select-all+delete, falling back to many DELs)."""
        try:
            self.key_combo("KEYCODE_CTRL_LEFT", "KEYCODE_A")
            self.key("KEYCODE_DEL")
        except AdbError:
            pass
        self.shell("input keyevent KEYCODE_MOVE_END; input keyevent " + " ".join(["KEYCODE_DEL"] * max_chars))

    _LOCK_RE = re.compile(r"(mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen|mIsShowing)=true")

    def is_locked(self) -> bool:
        out = self.shell("dumpsys window | grep -E 'mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen'; "
                         "dumpsys trust | grep -m1 -E 'deviceLocked'")
        if re.search(r"deviceLocked=1|deviceLocked=true", out):
            return True
        return bool(self._LOCK_RE.search(out))

    def is_awake(self) -> bool:
        return "mWakefulness=Awake" in self.shell("dumpsys power | grep -m1 mWakefulness=")

    def wake_and_unlock(self, pin: str | None = None) -> str:
        """Wake the screen and dismiss the keyguard. If a PIN/password is set, `pin`
        (or env ANDROID_PIN) is typed into the bouncer."""
        pin = pin or os.environ.get("ANDROID_PIN") or None
        if not self.is_awake():
            self.key("WAKEUP")
            time.sleep(0.5)
        if not self.is_locked():
            return "Screen awake and unlocked."
        self.shell("wm dismiss-keyguard")  # brings up the bouncer when secure
        time.sleep(0.6)
        if self.is_locked():
            w, h = self.screen_size()
            self.swipe(w // 2, int(h * 0.9), w // 2, int(h * 0.3), 200)
            time.sleep(0.6)
        if self.is_locked() and pin:
            self.shell("input text " + shlex.quote(pin), check=True)
            time.sleep(0.2)
            self.key("ENTER")
            for _ in range(25):
                time.sleep(0.4)
                if not self.is_locked():
                    break
        if self.is_locked():
            if pin:
                return "Typed the PIN but the device is still locked (wrong PIN, or a pattern lock)."
            return ("Screen is on but a secure lock is set. Call wake_and_unlock(pin=...) or set ANDROID_PIN "
                    "in the MCP server env; otherwise the user must unlock it.")
        return "Screen awake and unlocked."

    # ---- apps
    def install(self, apk_path: str, replace: bool = True, grant_permissions: bool = True,
                allow_downgrade: bool = False, allow_test: bool = True) -> str:
        p = Path(os.path.expanduser(apk_path))
        if not p.exists():
            raise AdbError(f"APK not found: {p}")
        flags = []
        if replace:
            flags.append("-r")
        if grant_permissions:
            flags.append("-g")
        if allow_downgrade:
            flags.append("-d")
        if allow_test:
            flags.append("-t")
        cmd = ["install-multiple" if p.is_dir() else "install", *flags]
        if p.is_dir():
            apks = sorted(str(a) for a in p.glob("*.apk"))
            if not apks:
                raise AdbError(f"no .apk files in {p}")
            cmd += apks
        else:
            cmd.append(str(p))
        out = self.run(*cmd, timeout=300, check=False)
        if "Success" not in out:
            raise AdbError(f"install failed: {out.strip()[-2000:]}")
        return out.strip()

    def uninstall(self, package: str, keep_data: bool = False) -> str:
        return self.run("uninstall", *(["-k"] if keep_data else []), package, check=False, timeout=60).strip()

    def launcher_activity(self, package: str) -> str | None:
        out = self.shell(f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {shlex.quote(package)}")
        lines = [l.strip() for l in out.splitlines() if "/" in l]
        return lines[-1] if lines else None

    def launch(self, package: str, activity: str | None = None, stop_first: bool = False, wait: bool = True,
               extras: dict | None = None, data_uri: str | None = None, action: str | None = None) -> str:
        if stop_first:
            self.force_stop(package)
        comp = None
        if activity:
            comp = activity if "/" in activity else f"{package}/{activity}"
        elif not action and not data_uri:
            comp = self.launcher_activity(package)
            if not comp:
                raise AdbError(f"no launcher activity for {package} (is it installed? try list_packages)")
        cmd = ["am", "start"]
        if wait:
            cmd.append("-W")
        if action:
            cmd += ["-a", action]
        if data_uri:
            cmd += ["-d", data_uri]
        if comp:
            cmd += ["-n", comp]
        elif package:
            cmd += ["-p", package]
        for k, v in (extras or {}).items():
            if isinstance(v, bool):
                cmd += ["--ez", k, str(v).lower()]
            elif isinstance(v, int):
                cmd += ["--ei", k, str(v)]
            elif isinstance(v, float):
                cmd += ["--ef", k, str(v)]
            else:
                cmd += ["--es", k, str(v)]
        out = self.shell(" ".join(shlex.quote(c) for c in cmd), timeout=60)
        if re.search(r"^Error|Exception|does not exist", out, re.M):
            raise AdbError(out.strip())
        return out.strip()

    def force_stop(self, package: str) -> None:
        self.shell(f"am force-stop {shlex.quote(package)}", check=True)

    def clear_data(self, package: str) -> str:
        return self.shell(f"pm clear {shlex.quote(package)}").strip()

    def packages(self, contains: str | None = None, third_party: bool = True) -> list[str]:
        out = self.shell("pm list packages" + (" -3" if third_party else ""))
        pk = sorted(l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:"))
        if contains:
            pk = [p for p in pk if contains.lower() in p.lower()]
        return pk

    def pid_of(self, package: str) -> list[str]:
        return self.shell(f"pidof {shlex.quote(package)}").split()

    # ---- logs
    def logcat(self, lines: int = 200, package: str | None = None, level: str | None = None,
               grep: str | None = None, buffer: str | None = None, since: str | None = None,
               tags: list[str] | None = None) -> str:
        cmd = ["logcat", "-d", "-v", "threadtime"]
        if buffer:
            for b in buffer.split(","):
                cmd += ["-b", b.strip()]
        filtering = bool(level or tags or grep or package)
        if since:
            cmd += ["-T", since]  # 'MM-DD HH:MM:SS.mmm' or epoch 'sss.mmm'
        elif not filtering:
            cmd += ["-t", str(int(lines))]  # -t applies before filters, so only use it unfiltered
        if package:
            pids = self.pid_of(package)
            if pids:
                cmd += [f"--pid={pids[0]}"]
            else:
                # app not running: fall back to grepping the package name / crash lines
                grep = grep or package
        filt = []
        if tags:
            filt += [f"{t}:{(level or 'V').upper()}" for t in tags] + ["*:S"]
        elif level:
            filt += [f"*:{level.upper()}"]
        shell_cmd = " ".join(shlex.quote(c) for c in cmd + filt)
        if grep:
            shell_cmd += " | grep -i -E " + shlex.quote(grep)
        out = self.shell(shell_cmd, timeout=60)
        out_lines = out.splitlines()
        if len(out_lines) > lines:
            out_lines = out_lines[-lines:]
        return "\n".join(out_lines)

    def device_time(self) -> str:
        """Timestamp usable as logcat `since` (marks 'now' on the device clock)."""
        return self.shell("date +%s.%3N").strip()

    # ---- files
    def push(self, local: str, remote: str) -> str:
        return self.run("push", os.path.expanduser(local), remote, timeout=600).strip()

    def pull(self, remote: str, local: str) -> str:
        return self.run("pull", remote, os.path.expanduser(local), timeout=600).strip()


def _looks_like_adb_failure(out: str) -> bool:
    return bool(re.search(r"^(error: |adb: |Error: )|no devices/emulators found|device offline", out, re.M))


def _fmt_devs(devs: list[dict]) -> str:
    return "; ".join(f"{d['serial']} ({d['state']}{', ' + d['model'] if 'model' in d else ''})" for d in devs) or "none"
