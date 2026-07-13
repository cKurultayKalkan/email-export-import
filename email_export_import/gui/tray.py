"""macOS menu-bar (status item) integration.

Only functional in the *packaged* app: there the Flutter engine runs
NSApplication's main loop inside this very process, so a pystray status
item created on the main thread (via AppHelper.callAfter) gets its click
events serviced for free by the existing run loop. In a source run
(`uv run email-export-import-gui`) the Python process has no NSApp loop —
a status item would render but never respond — so this module stays inert
there, and on every other platform.
"""
from __future__ import annotations

import sys
from typing import Callable

MenuSpec = list[tuple[object, Callable[[], None] | None]]


def _envelope_image(size: int = 44):
    """An envelope glyph for the status item.

    Drawn white and marked as a template image after creation (see
    start_status_item) — macOS then recolors it itself to match the menu
    bar (white on dark, black on light); template rendering only uses the
    alpha channel, so the white also stands correct if templating fails."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    m = size // 8
    top, bottom = size // 4, size - size // 4
    d.rounded_rectangle([m, top, size - m, bottom], radius=size // 12,
                        outline=white, width=3)
    mid = (top + bottom) // 2 + 1
    d.line([m + 2, top + 2, size // 2, mid], fill=white, width=3)
    d.line([size // 2, mid, size - m - 2, top + 2], fill=white, width=3)
    return img


def start_status_item(title: str, items: MenuSpec):
    """Create the status item; returns an opaque handle or None if unavailable.

    `items` is a list of (label, handler) tuples — label may be a callable
    (evaluated on every menu open, for live status lines) and handler may be
    None for a disabled, informational row. Handlers run on the AppKit main
    thread: marshal any UI work back onto the Flet event loop yourself.
    """
    if sys.platform != "darwin":
        return None
    try:
        import AppKit
        import pystray
        from PyObjCTools import AppHelper
    except Exception:
        return None
    # isRunning is only true when something (the Flutter engine) already
    # drives the main loop — exactly the packaged-app case. Without it the
    # item would be dead chrome, so don't create it at all.
    if not AppKit.NSApplication.sharedApplication().isRunning():
        return None

    def entry(label, handler):
        if handler is None:
            return pystray.MenuItem(label, None, enabled=False)
        return pystray.MenuItem(label, lambda icon: handler())

    menu = pystray.Menu(*(entry(label, handler) for label, handler in items))
    holder: dict = {}

    def _create_on_main_thread():
        icon = pystray.Icon("email-export-import", _envelope_image(), title, menu=menu)
        icon.run_detached()  # the running NSApp loop services it from here on
        icon.visible = True
        try:
            # Template mode: macOS recolors the glyph to match the menu bar.
            icon._icon_image.setTemplate_(True)  # noqa: SLF001 (pystray has no API)
        except Exception:
            pass  # stays white — correct for dark menu bars
        holder["icon"] = icon

    AppHelper.callAfter(_create_on_main_thread)
    return holder


def hide_app() -> bool:
    """True background mode: hide every window AND drop the Dock icon —
    the menu-bar status item becomes the only trace of the app. Safe to
    call from any thread; no-op (False) where AppKit is unavailable."""
    try:
        import AppKit
        from PyObjCTools import AppHelper
    except Exception:
        return False

    def _hide():
        app = AppKit.NSApplication.sharedApplication()
        app.hide_(None)
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    AppHelper.callAfter(_hide)
    return True


def show_app() -> bool:
    """Undo hide_app: Dock icon back, windows unhidden, app activated."""
    try:
        import AppKit
        from PyObjCTools import AppHelper
    except Exception:
        return False

    def _show():
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        app.unhide_(None)
        app.activateIgnoringOtherApps_(True)

    AppHelper.callAfter(_show)
    return True
