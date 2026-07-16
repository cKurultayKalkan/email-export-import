"""The daemon's tray icon — the persistent handle that stays in the menu bar
(macOS) / system tray (Windows) / app indicator (Linux) even when the GUI is
closed. It runs on the daemon's MAIN thread (pystray owns an event loop); the
HTTP server runs on a background thread.

Cross-platform via pystray. On macOS the process is made an "accessory" so it
shows a menu-bar item without a Dock icon.
"""
from __future__ import annotations

import sys
from typing import Callable


def _envelope_image(size: int = 44):
    """A filled BLUE envelope. A plain white/monochrome glyph was invisible on a
    light Windows tray; a saturated blue body with a white flap and outline
    stands out on both light (Windows) and dark (macOS) menu bars."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    blue = (37, 99, 235, 255)
    white = (255, 255, 255, 255)
    m = size // 8
    top, bottom = size // 4, size - size // 4
    d.rounded_rectangle([m, top, size - m, bottom], radius=size // 12,
                        fill=blue, outline=white, width=2)
    mid = (top + bottom) // 2 + 1
    d.line([m + 2, top + 2, size // 2, mid], fill=white, width=3)
    d.line([size // 2, mid, size - m - 2, top + 2], fill=white, width=3)
    return img


def available() -> bool:
    """True when a tray can plausibly be shown (a GUI session exists)."""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    if sys.platform not in ("darwin", "win32"):
        # Linux needs a display + an app-indicator backend; be conservative.
        import os
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


RUN_SLOTS = 8  # max per-migration lines shown under the tray status header


def run(title: str, status_text: Callable[[], str],
        on_open: Callable[[], None], on_quit: Callable[[], None],
        on_ready: Callable[[object], None] | None = None,
        open_label: str = "Open", quit_label: str = "Quit",
        run_lines: Callable[[], list[str]] | None = None) -> bool:
    """Run the tray icon loop on the CURRENT (main) thread until Quit.

    status_text() is the header line (a live "N running"), refreshed each menu
    open. run_lines() (optional) returns one string per active migration —
    rendered as disabled lines below the header so the user sees progress
    without opening the window. on_open launches/reveals the GUI; on_quit stops
    the daemon. on_ready(icon) is called once the icon exists so the caller can
    stop it from another thread (e.g. an HTTP /shutdown). Returns False
    immediately if no tray backend is usable (caller falls back to a serve loop)."""
    if not available():
        return False
    try:
        import pystray

        if sys.platform == "darwin":
            # Menu-bar item, no Dock icon, don't steal focus.
            try:
                import AppKit

                AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                    AppKit.NSApplicationActivationPolicyAccessory
                )
            except Exception:
                pass

        icon_holder: dict = {}

        def _quit(icon, _item=None):
            try:
                on_quit()
            finally:
                icon.stop()

        # A fixed pool of per-run slots (pystray needs a static item count): each
        # is visible only while there's a run at its index, and its text/visible
        # callables re-evaluate every time the menu opens (live progress).
        def _slot_text(i):
            def f(item):
                lines = run_lines() if run_lines else []
                return lines[i] if i < len(lines) else ""
            return f

        def _slot_visible(i):
            def f(item):
                lines = run_lines() if run_lines else []
                return i < len(lines)
            return f

        items = [pystray.MenuItem(lambda item: status_text(), None, enabled=False)]
        for i in range(RUN_SLOTS):
            items.append(pystray.MenuItem(_slot_text(i), None, enabled=False,
                                          visible=_slot_visible(i)))
        items.append(pystray.MenuItem(open_label, lambda icon, item: on_open()))
        items.append(pystray.MenuItem(quit_label, _quit))
        menu = pystray.Menu(*items)
        icon = pystray.Icon("email-export-import", _envelope_image(), title, menu=menu)
        icon_holder["icon"] = icon
        if on_ready is not None:
            on_ready(icon)
        icon.run()  # blocks until icon.stop()
        return True
    except Exception:
        return False
