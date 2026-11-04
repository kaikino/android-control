# android-control

An MCP server that lets Claude (Claude Code, or any MCP client) drive a **real Android device
over USB**. Claude can take screenshots, read exact pixel colours, inspect the UI hierarchy, tap,
type, swipe, install and launch apps, and read logcat. That's enough for it to build, run, and
verify an Android app on actual hardware without an emulator, and without you relaying what's on
screen.

Tested on Android 15 (a PIN-locked tablet), with native Settings screens and a Flutter app.

## Install

Requirements: `adb` (Android platform-tools), [`uv`](https://docs.astral.sh/uv/), and a device with
**Developer options → USB debugging** enabled (accept the "Allow USB debugging" prompt once).

```sh
git clone <this repo> ~/code/android-control
claude mcp add android -s user -- uv run --directory ~/code/android-control android-mcp
# optional: let Claude unlock a PIN/password-locked device
claude mcp add android -s user -e ANDROID_PIN=1234 -- uv run --directory ~/code/android-control android-mcp
```

Restart Claude Code (or run `/mcp`) and the `android` tools are available in every project. The
server sends instructions telling Claude to use the device on its own initiative while working on
Android apps.

adb is found via `$ADB`, `PATH`, `$ANDROID_HOME`/`$ANDROID_SDK_ROOT`, `~/Library/Android/sdk`,
or Homebrew's `android-commandlinetools`.

## Tools

| Area | Tools |
|---|---|
| Device | `list_devices`, `select_device`, `device_info`, `wake_and_unlock`, `keep_awake` |
| Seeing | `screenshot` (downscale, `grid` overlay labelled in device px, `region` zoom, `mark` points, `save_path`), `get_pixels`, `region_colors`, `compare_screen` (diff vs last/baseline with red overlay), `wait_for_stable_screen` |
| UI tree | `ui_tree`, `find_elements`, `wait_for_element`, `scroll_to_element` |
| Input | `tap` (coords or selector), `long_press`, `swipe` (incl. drag-and-drop), `scroll`, `type_text` (focus/clear/submit), `press_key` (names, keycodes, combos) |
| Apps | `install_apk` (incl. split-APK dirs), `uninstall_app`, `launch_app` (cold start, deep links, extras), `stop_app`, `clear_app_data`, `list_packages`, `current_app` |
| Debug | `logcat` (by package/pid, level, tags, regex, `crash` buffer, `since` a `log_mark`), `shell`, `push_file`, `pull_file` |

All coordinates are **device pixels in the current orientation**. Action tools take
`screenshot_after=true` to return the resulting screen in the same call.

Selectors (for `tap`, `find_elements`, etc.): `text` (exact, case-insensitive, matches text or
content-desc), `text_contains`, `text_regex`, `resource_id` (full or the part after `:id/`),
`class_name`. Flutter and Compose semantics labels show up as content-desc, so `text` matches them.

## How it works

- Screenshots: `adb exec-out screencap -p`, processed with Pillow.
- Input: `adb shell input …`.
- UI hierarchy: a small bundled dex (`dumper/AcDump.java` → `src/android_control/assets/ac-dump.jar`)
  is pushed to `/data/local/tmp` and run with `app_process`. It reuses the device's own
  `uiautomator.jar` dumper classes but waits only briefly for idle. Stock `uiautomator dump` fails
  with "could not get idle state" on screens with continuous animation. The stock command is still
  the fallback. Rebuild with `dumper/build.sh` (needs a JDK and SDK build-tools).
- When an emulator and one physical USB device are both connected, the physical device is picked
  automatically. Otherwise use `select_device` or `ANDROID_SERIAL`.

## CLI

`uv run droid --help` gives quick shell access: `droid shot out.png --grid 100`, `droid tree`,
`droid tap "Sign in"`, `droid key back`, `droid pixel 10 10`, `droid unlock`.

## Limitations

- `type_text` is ASCII-only (an adb `input text` limitation).
- Secure windows (the lock-screen bouncer, `FLAG_SECURE` apps) capture as black.
- Pattern locks can't be entered; PIN and password locks can (via `ANDROID_PIN` or `pin=`).
- The UI tree covers the active window, so the soft keyboard isn't included.

## Tests

```sh
uv run python tests/e2e_readonly.py /tmp/ac-out                      # no screen interaction
ANDROID_PIN=… uv run python tests/e2e_interactive.py /tmp/ac-out [return.package]  # drives Settings
```
