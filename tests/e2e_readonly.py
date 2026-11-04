"""End-to-end smoke test through a real MCP stdio session. Read-only: does not touch the screen."""
import asyncio, base64, sys, time
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/android-control-test")
OUT.mkdir(parents=True, exist_ok=True)

def show(name, res, t):
    texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
    imgs = [c for c in res.content if getattr(c, "type", "") == "image"]
    print(f"--- {name} ({t:.2f}s) error={res.is_error if hasattr(res,'is_error') else getattr(res,'isError',None)}")
    for tx in texts: print(tx[:1500])
    for i, im in enumerate(imgs):
        p = OUT / f"{name}_{i}.{im.mime_type.split('/')[1]}"
        p.write_bytes(base64.b64decode(im.data)); print(f"[image -> {p}]")

async def main():
    params = StdioServerParameters(command="uv", args=["run", "--directory", str(ROOT), "android-mcp"])
    async with Client(params) as c:
        tools = await c.list_tools()
        print("tools:", len(tools.tools), [t.name for t in tools.tools])
        calls = [
            ("list_devices", {}), ("device_info", {}), ("current_app", {}),
            ("screenshot", {}), ("screenshot_grid", {"grid": 100, "max_size": 0}),
            ("screenshot_region", {"region": [1150, 660, 1265, 730], "mark": [[1205, 696]]}),
            ("get_pixels", {"points": [[1205, 696], [10, 10], [5000, 5000]]}),
            ("region_colors", {"x1": 18, "y1": 410, "x2": 1262, "y2": 455}),
            ("ui_tree", {}), ("find_elements", {"text_contains": "ankle"}),
            ("find_elements_none", {"text": "zzzz-nope"}),
            ("compare_screen", {}),
            ("logcat", {"lines": 5, "level": "E"}), ("log_mark", {}),
            ("shell", {"command": "echo hi; getprop ro.product.model"}),
            ("tap_bad_selector", {"text": "zzzz-nope"}),
        ]
        for name, args in calls:
            tool = {"screenshot_grid": "screenshot", "screenshot_region": "screenshot",
                    "find_elements_none": "find_elements", "tap_bad_selector": "tap"}.get(name, name)
            t = time.monotonic()
            res = await c.call_tool(tool, args)
            show(name, res, time.monotonic() - t)

asyncio.run(main())
