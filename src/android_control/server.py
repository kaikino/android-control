"""MCP server exposing a USB-connected Android device to Claude."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Literal

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image as MCPImage
from PIL import Image

from . import imaging
from .adb import AdbError, Device, Selector, UINode

INSTRUCTIONS = """\
Tools for controlling a real Android device attached over USB (via adb). Use them on your own
initiative whenever you are building, debugging or testing an Android app (native, Flutter, React
Native, Compose...) — do not wait for the user to ask you to look at the device.

Typical loop: build -> install_apk -> launch_app -> screenshot / ui_tree -> interact (tap,
type_text, swipe, scroll) -> verify (screenshot, get_pixels, find_elements, logcat) -> fix code.

If the screen is off or locked (black screenshots, device_info locked=true) call wake_and_unlock;
call keep_awake once per session so the device doesn't sleep while you work.

Coordinates: every x/y is a device pixel in the CURRENT orientation (what screencap and
`input tap` use). Screenshots may be downscaled; the text accompanying each screenshot gives the
scale, and grid=<step> overlays gridlines labelled in device coordinates. Prefer tapping by
selector (text / resource_id / desc via `text`) which uses the accessibility tree and is exact;
ui_tree lists elements with their centre @(x,y). Flutter/Compose labels often appear as `desc`.

Check logcat (level='E', or package=...) after crashes or unexpected behaviour; buffer='crash'
shows recent crashes. get_pixels / region_colors read exact RGB values from the live screen.
"""

mcp = MCPServer("android", instructions=INSTRUCTIONS)
_register = mcp.tool


def _tool(**kw):
    """mcp.tool, but any exception reaches the model as a readable ToolError message."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except ToolError:
                raise
            except AdbError as e:
                raise ToolError(str(e)) from e
            except Exception as e:  # unexpected: still tell the model what happened
                raise ToolError(f"{type(e).__name__}: {e}") from e
        _register(**kw)(wrapper)
        return fn
    return deco


mcp.tool = _tool  # type: ignore[method-assign]

_lock = threading.RLock()
_device: Device | None = None
_last_shot: Image.Image | None = None
SHOT_DIR = Path(os.environ.get("ANDROID_CONTROL_SHOTS", "/tmp/android-control"))


def dev() -> Device:
    global _device
    if _device is None:
        _device = Device(serial=os.environ.get("ANDROID_SERIAL") or None)
    _device.ensure_ready()
    return _device


def _selector(text=None, text_contains=None, text_regex=None, resource_id=None, class_name=None,
              clickable=None, package=None) -> Selector:
    return Selector(text=text, text_contains=text_contains, text_regex=text_regex, resource_id=resource_id,
                    class_name=class_name, clickable=clickable, package=package)


def _shot(max_size: int = 1280, grid: int = 0, region: list[int] | None = None,
          mark: list[list[int]] | None = None, save_path: str | None = None,
          fmt: str = "png", img: Image.Image | None = None) -> list:
    global _last_shot
    d = dev()
    full = img if img is not None else d.screenshot_image()
    _last_shot = full
    W, H = full.size
    origin = (0, 0)
    view = full
    if region:
        x1, y1, x2, y2 = (int(v) for v in region)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(W, x2), min(H, y2)
        if x2 <= x1 or y2 <= y1:
            raise AdbError(f"empty region {region} for screen {W}x{H}")
        view = full.crop((x1, y1, x2, y2))
        origin = (x1, y1)
    if region:
        # zoom small regions up so details are legible
        s = min(4.0, max_size / max(view.size)) if max_size else 1.0
        if s != 1.0:
            view = view.resize((max(1, round(view.width * s)), max(1, round(view.height * s))),
                               Image.NEAREST if s > 1 else Image.LANCZOS)
        scale = s
    else:
        view, scale = imaging.fit(view, max_size)
    if mark:
        view = imaging.mark_points(view, [(int(p[0]), int(p[1])) for p in mark], origin, scale)
    if grid:
        view = imaging.draw_grid(view, int(grid), origin, scale)
    saved = ""
    if save_path:
        p = Path(os.path.expanduser(save_path))
        p.parent.mkdir(parents=True, exist_ok=True)
        full.save(p)  # always save full-res, unannotated
        saved = f" Full-res saved to {p}."
    if fmt == "jpeg":
        data, f = imaging.to_jpeg(view), "jpeg"
    else:
        data, f = imaging.to_png(view), "png"
    desc = f"Screen {W}x{H} (device px)."
    if region:
        desc += f" Showing region [{origin[0]},{origin[1]},{origin[0] + round(view.width / scale)},{origin[1] + round(view.height / scale)}]"
    if scale != 1.0:
        desc += f" Image is scaled x{scale:.3f}: device_x = {origin[0]} + image_x / {scale:.3f}." if region else \
            f" Image is scaled x{scale:.3f} ({view.width}x{view.height}): device = image / {scale:.3f}."
    elif region:
        desc += f": device = image + ({origin[0]},{origin[1]})."
    desc += saved
    if full.convert("L").getextrema()[1] < 8:
        desc += (" WARNING: the screen is entirely black — it is probably off/asleep, locked behind a secure "
                 "keyguard (secure windows capture as black), or showing FLAG_SECURE content. Call wake_and_unlock.")
    return [desc, MCPImage(data=data, format=f)]


def _maybe_after(screenshot_after: bool, wait_ms: int, msg: str) -> list | str:
    if not screenshot_after:
        return msg
    time.sleep(max(0, wait_ms) / 1000)
    return [msg, *_shot()]


def _resolve_target(x, y, sel: Selector, index: int) -> tuple[int, int, str]:
    if x is not None and y is not None:
        return int(x), int(y), f"({int(x)},{int(y)})"
    if sel.empty():
        raise AdbError("give either x and y, or a selector (text / text_contains / text_regex / resource_id / class_name)")
    found = dev().find(sel)
    if not found:
        raise AdbError(f"no element matches {sel}. Use ui_tree to see what's on screen.")
    if index >= len(found):
        raise AdbError(f"only {len(found)} element(s) match {sel}; index {index} out of range")
    n = found[index]
    cx, cy = n.center
    extra = f" ({len(found)} matches, used #{index})" if len(found) > 1 else ""
    return cx, cy, f"{n.short_class} '{n.label or n.resource_id}' @({cx},{cy}){extra}"


def _fmt_nodes(nodes: list[UINode], limit: int) -> str:
    base = min((n.depth for n in nodes[:limit]), default=0)
    lines = [("  " * min(n.depth - base, 12)) + n.describe() for n in nodes[:limit]]
    if len(nodes) > limit:
        lines.append(f"... {len(nodes) - limit} more (raise max_nodes or filter by package)")
    return "\n".join(lines)



# ============================================================================ device

@mcp.tool(structured_output=False)
def list_devices() -> str:
    """List adb devices (serial, state, model). States other than 'device' need user action."""
    devs = Device.list_devices()
    cur = _device.serial if _device else None
    for d in devs:
        d["selected"] = d["serial"] == cur
    return json.dumps(devs, indent=1) if devs else "No devices. Connect via USB with USB debugging enabled."


@mcp.tool(structured_output=False)
def select_device(serial: str) -> str:
    """Choose which device to control when several are connected."""
    global _device
    with _lock:
        _device = Device(serial=serial)
        _device.ensure_ready()
    return f"Selected {serial}."


@mcp.tool(structured_output=False)
def device_info() -> str:
    """Model, Android version, screen size in current orientation, density, rotation, lock/wake state,
    battery and the currently focused window/activity."""
    return json.dumps(dev().info(), indent=1)


@mcp.tool(structured_output=False)
def wake_and_unlock(pin: str | None = None) -> str:
    """Turn the screen on and dismiss the lock screen. For a PIN/password lock pass `pin` (or the
    server reads ANDROID_PIN from its env). Pattern locks can't be entered. Call this whenever
    screenshots are black / show the lock screen, or device_info says locked/asleep.
    Tip: shell('svc power stayon usb') keeps the screen on while plugged in."""
    with _lock:
        return dev().wake_and_unlock(pin)


@mcp.tool(structured_output=False)
def keep_awake(enabled: bool = True) -> str:
    """Keep the screen on while the device is plugged in over USB (Developer option 'Stay awake'),
    so it doesn't sleep/lock between actions. enabled=False restores normal behaviour."""
    d = dev()
    d.shell(f"svc power stayon {'usb' if enabled else 'false'}", check=True)
    if enabled:
        d.wake_and_unlock()
    return f"Stay-awake on USB {'enabled' if enabled else 'disabled'}."


# ============================================================================ seeing

@mcp.tool(structured_output=False)
def screenshot(max_size: int = 1280, grid: int = 0, region: list[int] | None = None,
               mark: list[list[int]] | None = None, save_path: str | None = None,
               format: Literal["png", "jpeg"] = "png") -> list:
    """Capture the device screen and return it as an image.

    max_size: longest side of the returned image (downscaled if larger; 0 = full res).
    grid: if >0, overlay gridlines every `grid` device pixels, labelled in device coordinates
          (e.g. 100) — useful for picking tap coordinates precisely.
    region: [x1, y1, x2, y2] in device px to crop/zoom into (small regions are enlarged up to 4x).
    mark: [[x, y], ...] device points to circle in red (e.g. to check where a tap will land).
    save_path: also save the full-resolution unannotated PNG to this local path.
    """
    with _lock:
        return _shot(max_size=max_size, grid=grid, region=region, mark=mark, save_path=save_path, fmt=format)


@mcp.tool(structured_output=False)
def get_pixels(points: list[list[int]]) -> str:
    """Exact RGB colour of the live screen at device points [[x, y], ...]. Returns hex + rgb per point."""
    img = dev().screenshot_image()
    W, H = img.size
    out = []
    for p in points:
        x, y = int(p[0]), int(p[1])
        if not (0 <= x < W and 0 <= y < H):
            out.append({"x": x, "y": y, "error": f"outside screen {W}x{H}"})
            continue
        rgb = img.getpixel((x, y))
        out.append({"x": x, "y": y, "hex": imaging.hex_of(rgb), "rgb": list(rgb[:3])})
    return json.dumps(out)


@mcp.tool(structured_output=False)
def region_colors(x1: int, y1: int, x2: int, y2: int, top: int = 5) -> str:
    """Colour statistics of a screen rectangle (device px): average colour, number of unique colours,
    dominant colours with their fraction, and whether it is a single uniform colour."""
    img = dev().screenshot_image()
    return json.dumps(imaging.region_stats(img, (int(x1), int(y1), int(x2), int(y2)), top))


@mcp.tool(structured_output=False)
def compare_screen(baseline_path: str | None = None, threshold: int = 16, show_diff: bool = True,
                   update_baseline: bool = True) -> list | str:
    """Compare the live screen with a baseline: by default the last screenshot taken through these
    tools, or a PNG at baseline_path (e.g. one saved via screenshot(save_path=...)).
    Reports changed pixel fraction and bounding box; show_diff returns the current screen with changed
    pixels tinted red. threshold = per-channel tolerance 0-255."""
    global _last_shot
    with _lock:
        if baseline_path:
            base = Image.open(os.path.expanduser(baseline_path)).convert("RGB")
        elif _last_shot is not None:
            base = _last_shot
        else:
            raise AdbError("no baseline: take a screenshot first or pass baseline_path")
        cur = dev().screenshot_image()
        res = imaging.diff(base, cur, threshold)
        mask = res.pop("_mask", None)
        if update_baseline:
            _last_shot = cur
        text = json.dumps(res)
        if show_diff and mask is not None and not res.get("identical"):
            vis, s = imaging.fit(imaging.diff_visual(base, cur, mask), 1280)
            return [text + (f" (diff image scaled x{s:.3f})" if s != 1 else ""), MCPImage(data=imaging.to_png(vis), format="png")]
        return text


@mcp.tool(structured_output=False)
def wait_for_stable_screen(timeout: float = 8.0, interval_ms: int = 300, threshold: int = 8,
                           screenshot_after: bool = False) -> list | str:
    """Wait until two consecutive screenshots are (nearly) identical, i.e. animations/loading have settled.
    Use after launching an app or navigating."""
    d = dev()
    deadline = time.monotonic() + timeout
    prev = d.screenshot_image()
    t0 = time.monotonic()
    while True:
        time.sleep(interval_ms / 1000)
        cur = d.screenshot_image()
        r = imaging.diff(prev, cur, threshold)
        if r.get("changed_fraction", 1) < 0.0005:
            msg = f"Screen stable after {time.monotonic() - t0:.1f}s."
            return [msg, *_shot(img=cur)] if screenshot_after else msg
        if time.monotonic() > deadline:
            msg = f"Screen still changing after {timeout}s (last change bbox {r.get('changed_bbox')})."
            return [msg, *_shot(img=cur)] if screenshot_after else msg
        prev = cur


@mcp.tool(structured_output=False)
def ui_tree(all_nodes: bool = False, package: str | None = None, max_nodes: int = 400) -> str:
    """Dump the accessibility/view hierarchy (uiautomator). Each line: #index Class text/desc/hint id
    [x1,y1,x2,y2] @(centre_x,centre_y) flags. By default only 'interesting' nodes (with text, id,
    or clickable/scrollable/checkable/editable) are shown; all_nodes=True shows layout containers too.
    package filters to one app (drops system UI). Takes ~1-4 s."""
    nodes = dev().ui_nodes()
    if package:
        nodes = [n for n in nodes if n.package == package]
    if not all_nodes:
        nodes = [n for n in nodes if n.is_interesting()]
    if not nodes:
        return "No matching nodes (screen may be a game/canvas/WebView-without-accessibility; use screenshot)."
    return _fmt_nodes(nodes, max_nodes)


@mcp.tool(structured_output=False)
def find_elements(text: str | None = None, text_contains: str | None = None, text_regex: str | None = None,
                  resource_id: str | None = None, class_name: str | None = None, clickable: bool | None = None,
                  package: str | None = None) -> str:
    """Find UI elements matching a selector; returns each with bounds and centre @(x,y).
    Selector fields (all given must match): text = exact (case-insensitive) text/desc; text_contains =
    substring of text/desc/hint; text_regex = regex; resource_id = full id or part after ':id/';
    class_name = e.g. 'Button' or 'android.widget.EditText'; clickable; package."""
    sel = _selector(text, text_contains, text_regex, resource_id, class_name, clickable, package)
    found = dev().find(sel)
    return _fmt_nodes(found, 100) if found else f"No element matches {sel}."



@mcp.tool(structured_output=False)
def wait_for_element(text: str | None = None, text_contains: str | None = None, text_regex: str | None = None,
                     resource_id: str | None = None, class_name: str | None = None, gone: bool = False,
                     timeout: float = 10.0) -> str:
    """Poll the UI tree until an element matching the selector appears (or disappears if gone=True)."""
    sel = _selector(text, text_contains, text_regex, resource_id, class_name)
    t0 = time.monotonic()
    found = dev().wait_for(sel, timeout=timeout, gone=gone)
    dt = time.monotonic() - t0
    if gone:
        return f"Element gone after {dt:.1f}s."
    return f"Found after {dt:.1f}s:\n" + _fmt_nodes(found, 20)


# ============================================================================ acting

@mcp.tool(structured_output=False)
def tap(x: int | None = None, y: int | None = None, text: str | None = None, text_contains: str | None = None,
        text_regex: str | None = None, resource_id: str | None = None, class_name: str | None = None,
        index: int = 0, double: bool = False, screenshot_after: bool = False, wait_ms: int = 600) -> list | str:
    """Tap at device coordinates (x, y), or at the centre of the element matching a selector
    (index picks among multiple matches, in document order). double=True double-taps.
    screenshot_after returns a screenshot wait_ms after the tap."""
    with _lock:
        cx, cy, what = _resolve_target(x, y, _selector(text, text_contains, text_regex, resource_id, class_name), index)
        d = dev()
        d.double_tap(cx, cy) if double else d.tap(cx, cy)
        return _maybe_after(screenshot_after, wait_ms, f"{'Double-tapped' if double else 'Tapped'} {what}.")


@mcp.tool(structured_output=False)
def long_press(x: int | None = None, y: int | None = None, text: str | None = None,
               text_contains: str | None = None, resource_id: str | None = None, duration_ms: int = 800,
               index: int = 0, screenshot_after: bool = False, wait_ms: int = 600) -> list | str:
    """Long-press at (x, y) or on the element matching a selector."""
    with _lock:
        cx, cy, what = _resolve_target(x, y, _selector(text, text_contains, None, resource_id), index)
        dev().long_press(cx, cy, duration_ms)
        return _maybe_after(screenshot_after, wait_ms, f"Long-pressed {what} for {duration_ms}ms.")


@mcp.tool(structured_output=False)
def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, drag: bool = False,
          screenshot_after: bool = False, wait_ms: int = 600) -> list | str:
    """Swipe from (x1,y1) to (x2,y2) in device px. drag=True holds first (long-press then move) for
    drag-and-drop/reordering. Use larger duration_ms for slow, non-fling scrolls."""
    with _lock:
        d = dev()
        d.drag(x1, y1, x2, y2, duration_ms) if drag else d.swipe(x1, y1, x2, y2, duration_ms)
        return _maybe_after(screenshot_after, wait_ms, f"{'Dragged' if drag else 'Swiped'} ({x1},{y1})->({x2},{y2}).")


@mcp.tool(structured_output=False)
def scroll(direction: Literal["down", "up", "left", "right"] = "down", amount: float = 0.6,
           x: int | None = None, y: int | None = None, screenshot_after: bool = False,
           wait_ms: int = 500) -> list | str:
    """Scroll content. direction='down' reveals content further down (finger moves up).
    amount = fraction of screen to move (0-1). x,y = point inside the scrollable area (default screen
    centre — on multi-pane layouts pass a point inside the pane you want; ui_tree marks scrollables)."""
    with _lock:
        d = dev()
        w, h = d.screen_size()
        cx = int(x) if x is not None else w // 2
        cy = int(y) if y is not None else h // 2
        dx = int(w * amount / 2)
        dy = int(h * amount / 2)
        vec = {"down": (0, -dy), "up": (0, dy), "right": (-dx, 0), "left": (dx, 0)}[direction]
        x1, y1 = cx - vec[0], cy - vec[1]
        x2, y2 = cx + vec[0], cy + vec[1]
        clamp = lambda v, m: max(1, min(m - 2, v))
        d.swipe(clamp(x1, w), clamp(y1, h), clamp(x2, w), clamp(y2, h), 400)
        return _maybe_after(screenshot_after, wait_ms, f"Scrolled {direction}.")


@mcp.tool(structured_output=False)
def scroll_to_element(text: str | None = None, text_contains: str | None = None, text_regex: str | None = None,
                      resource_id: str | None = None, direction: Literal["down", "up", "left", "right"] = "down",
                      max_scrolls: int = 10, x: int | None = None, y: int | None = None,
                      container_id: str | None = None) -> str:
    """Scroll repeatedly until an element matching the selector is visible; returns it.
    Scrolls inside the largest scrollable container (or the one whose resource id ends with
    container_id, or at point x,y). Stops early if the screen stops changing (end of list)."""
    sel = _selector(text, text_contains, text_regex, resource_id)
    d = dev()
    for i in range(max_scrolls + 1):
        nodes = d.ui_nodes()
        found = d.find(sel, nodes)
        if found:
            return f"Found after {i} scroll(s):\n" + _fmt_nodes(found, 10)
        if x is None or y is None:
            scrollables = [n for n in nodes if n.scrollable]
            if container_id:
                scrollables = [n for n in scrollables if n.resource_id.endswith(container_id)]
            if scrollables:
                big = max(scrollables, key=lambda n: (n.bounds[2] - n.bounds[0]) * (n.bounds[3] - n.bounds[1]))
                x, y = big.center
        before = d.screenshot_image()
        scroll(direction=direction, x=x, y=y)
        time.sleep(0.5)
        if imaging.diff(before, d.screenshot_image(), 8).get("changed_fraction", 1) < 0.0005:
            raise AdbError(f"reached end of scrollable content after {i + 1} scroll(s); {sel} not found")
    raise AdbError(f"{sel} not found after {max_scrolls} scrolls")


@mcp.tool(structured_output=False)
def type_text(text: str, x: int | None = None, y: int | None = None, field_text: str | None = None,
              resource_id: str | None = None, clear_first: bool = False, submit: bool = False,
              hide_keyboard: bool = False, screenshot_after: bool = False, wait_ms: int = 500) -> list | str:
    """Type text into the focused field. Optionally first tap a field: by (x, y), by its current
    text/hint (field_text, substring match), or resource_id. clear_first empties it; submit presses
    ENTER afterwards; hide_keyboard presses BACK if the IME is showing. Newlines are sent as ENTER.
    ASCII only (adb `input text` limitation)."""
    with _lock:
        d = dev()
        sel = _selector(text_contains=field_text, resource_id=resource_id)
        if (x is not None and y is not None) or not sel.empty():
            cx, cy, _ = _resolve_target(x, y, sel, 0)
            d.tap(cx, cy)
            time.sleep(0.35)
        if clear_first:
            d.clear_text()
        d.type_text(text)
        if submit:
            d.key("ENTER")
        if hide_keyboard:
            if "mInputShown=true" in d.shell("dumpsys input_method | grep -m1 mInputShown"):
                d.key("BACK")
        return _maybe_after(screenshot_after, wait_ms, f"Typed {len(text)} chars.")


@mcp.tool(structured_output=False)
def press_key(keys: list[str], longpress: bool = False, combo: bool = False,
              screenshot_after: bool = False, wait_ms: int = 500) -> list | str:
    """Press one or more keys in sequence: names like back, home, enter, delete, tab, recents, menu,
    power, wakeup, volume_up, up/down/left/right, or any KEYCODE_* (e.g. KEYCODE_MEDIA_PLAY) or number.
    combo=True presses them together (e.g. ['KEYCODE_CTRL_LEFT','KEYCODE_A'])."""
    with _lock:
        d = dev()
        d.key_combo(*keys) if combo else d.key(*keys, longpress=longpress)
        return _maybe_after(screenshot_after, wait_ms, "Pressed " + (" + " if combo else ", ").join(keys) + ".")


# ============================================================================ apps

@mcp.tool(structured_output=False)
def install_apk(path: str, grant_permissions: bool = True, allow_downgrade: bool = False) -> str:
    """Install (or replace) an APK from a local path, e.g. build/app/outputs/flutter-apk/app-debug.apk or
    app/build/outputs/apk/debug/app-debug.apk. A directory installs all split APKs in it.
    grant_permissions auto-grants runtime permissions."""
    return dev().install(path, grant_permissions=grant_permissions, allow_downgrade=allow_downgrade)


@mcp.tool(structured_output=False)
def uninstall_app(package: str, keep_data: bool = False) -> str:
    """Uninstall a package."""
    return dev().uninstall(package, keep_data)


@mcp.tool(structured_output=False)
def launch_app(package: str, activity: str | None = None, restart: bool = False, data_uri: str | None = None,
               action: str | None = None, extras: dict | None = None, screenshot_after: bool = False,
               wait_ms: int = 1500) -> list | str:
    """Start an app's launcher activity (or a specific activity, '.MainActivity' or 'pkg/.Act').
    restart=True force-stops first for a cold start. data_uri/action send an intent (e.g. deep links,
    action='android.intent.action.VIEW'); extras = {key: str|int|bool|float} intent extras."""
    with _lock:
        out = dev().launch(package, activity, stop_first=restart, data_uri=data_uri, action=action, extras=extras)
        return _maybe_after(screenshot_after, wait_ms, out)


@mcp.tool(structured_output=False)
def stop_app(package: str) -> str:
    """Force-stop an app."""
    dev().force_stop(package)
    return f"Stopped {package}."


@mcp.tool(structured_output=False)
def clear_app_data(package: str) -> str:
    """Wipe an app's data and cache (like a fresh install; also revokes permissions)."""
    return dev().clear_data(package)


@mcp.tool(structured_output=False)
def list_packages(contains: str | None = None, include_system: bool = False) -> str:
    """List installed package names (third-party only unless include_system)."""
    pk = dev().packages(contains, third_party=not include_system)
    return "\n".join(pk) or "No packages match."


@mcp.tool(structured_output=False)
def current_app() -> str:
    """The focused window (package/activity) — tells you what's in the foreground."""
    d = dev()
    focus = d.current_focus()
    pkg = focus.split("/", 1)[0]
    pids = d.pid_of(pkg) if pkg and "." in pkg else []
    return f"{focus}" + (f" (pid {pids[0]})" if pids else "")


# ============================================================================ logs / shell / files

@mcp.tool(structured_output=False)
def logcat(lines: int = 200, package: str | None = None, level: Literal["V", "D", "I", "W", "E", "F"] | None = None,
           grep: str | None = None, tags: list[str] | None = None, buffer: str | None = None,
           since: str | None = None, clear: bool = False) -> str:
    """Read device logs (non-blocking, last `lines` lines).
    package: only that app's process (by pid). level: minimum priority (E = errors only).
    grep: case-insensitive regex filter. tags: only these log tags. buffer: 'main', 'crash', 'system',
    'events', or 'all' (crash shows FATAL EXCEPTION stack traces even after the process died).
    since: device timestamp from log_mark() to only see logs after that point.
    clear: clear the log buffer AFTER reading (start fresh for the next check)."""
    d = dev()
    out = d.logcat(lines=lines, package=package, level=level, grep=grep, buffer=buffer, since=since, tags=tags)
    if clear:
        d.shell("logcat -c")
    if package and not d.pid_of(package):
        out = f"(note: {package} is not running; filtered by text instead of pid)\n" + out
    return out.strip() or "(no matching log lines)"


@mcp.tool(structured_output=False)
def log_mark() -> str:
    """Return the current device time to pass as logcat(since=...) so you only see logs produced
    after this point (e.g. mark, then tap a button, then read logs)."""
    return dev().device_time()


@mcp.tool(structured_output=False)
def shell(command: str, timeout: float = 30) -> str:
    """Run an arbitrary `adb shell` command on the device and return its output (stdout+stderr).
    Examples: 'dumpsys activity top', 'am broadcast ...', 'settings put system ...', 'pm grant ...',
    'run-as <pkg> ls files', 'svc power stayon usb', 'cmd uimode night yes'."""
    return dev().shell(command, timeout=timeout) or "(no output)"


@mcp.tool(structured_output=False)
def push_file(local_path: str, device_path: str) -> str:
    """Copy a local file to the device (e.g. /sdcard/Download/x.png)."""
    return dev().push(local_path, device_path)


@mcp.tool(structured_output=False)
def pull_file(device_path: str, local_path: str) -> str:
    """Copy a file from the device to the local machine."""
    return dev().pull(device_path, local_path)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
