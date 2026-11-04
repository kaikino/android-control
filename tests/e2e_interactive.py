"""Interactive end-to-end test via MCP: wakes device, drives Settings, returns to a given app.
Usage: e2e_interactive.py OUT_DIR [return_package]"""
import asyncio, base64, os, sys, time
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
RET = sys.argv[2] if len(sys.argv) > 2 else None
n = 0

async def call(c, tool, **args):
    global n
    t = time.monotonic(); res = await c.call_tool(tool, args); dt = time.monotonic() - t
    n += 1
    print(f"--- {n:02d} {tool} {args} ({dt:.2f}s) error={res.is_error}")
    for ct in res.content:
        if ct.type == "text": print("   " + ct.text[:700].replace("\n", "\n   "))
        elif ct.type == "image":
            p = OUT / f"{n:02d}_{tool}.png"; p.write_bytes(base64.b64decode(ct.data)); print(f"   [image {p}]")
    return res

async def main():
    async with Client(StdioServerParameters(command="uv", args=["run", "--directory", str(ROOT), "android-mcp"],
            env={k: v for k, v in os.environ.items() if k.startswith(("ANDROID", "PATH", "HOME"))})) as c:
        await call(c, "press_key", keys=["sleep"])
        await call(c, "device_info")
        await call(c, "wake_and_unlock")
        await call(c, "device_info")
        await call(c, "press_key", keys=["home"])
        await call(c, "launch_app", package="com.android.settings", restart=True)
        await call(c, "wait_for_stable_screen", screenshot_after=True)
        await call(c, "current_app")
        await call(c, "scroll_to_element", text_regex="(?i)about (tablet|phone|device)")
        await call(c, "tap", text_regex="(?i)about (tablet|phone|device)", screenshot_after=True, wait_ms=1200)
        await call(c, "find_elements", text_contains="Android version")
        await call(c, "scroll", direction="up", amount=0.9, x=230, y=420)
        await call(c, "scroll", direction="up", amount=0.9, x=230, y=420)
        await call(c, "ui_tree", package="com.android.settings")
        await call(c, "tap", text="Search", screenshot_after=True, wait_ms=1200)
        await call(c, "type_text", text="bluetooth & wifi's", screenshot_after=True, wait_ms=1500)
        await call(c, "type_text", text="display", clear_first=True, hide_keyboard=True, screenshot_after=True, wait_ms=1500)
        await call(c, "long_press", x=200, y=200)
        await call(c, "swipe", x1=640, y1=600, x2=640, y2=200)
        await call(c, "compare_screen", show_diff=True)
        await call(c, "logcat", lines=5, level="E")
        await call(c, "logcat", lines=5, package="com.android.settings")
        await call(c, "logcat", lines=3, buffer="crash")
        await call(c, "stop_app", package="com.android.settings")
        await call(c, "list_packages", contains="dance")
        await call(c, "launch_app", package="com.android.settings", activity="nope.Bad")
        if RET:
            await call(c, "launch_app", package=RET, screenshot_after=True)

asyncio.run(main())
