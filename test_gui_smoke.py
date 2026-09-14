"""Construct the GUI, toggle themes, resize, then destroy — no mainloop."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="iq_gui_smoke_"))
    os.environ["IQ_GUI_SETTINGS"] = str(tmp / ".iq_gui_settings.json")

    import iq_data  # noqa: F401
    import iq_gui
    from iq_gui import IQConverterGUI
    from iq_theme import DARK_MODE, LIGHT_MODE, current_mode, current_palette

    app = IQConverterGUI()
    app.update_idletasks()
    app.update()

    start = current_mode()
    assert start in (LIGHT_MODE, DARK_MODE), start
    assert app.status_var.get() == iq_gui.STATUS_READY
    assert "Convert" in app.convert_button.cget("text")

    app.apply_theme(DARK_MODE)
    app.update()
    assert current_mode() == DARK_MODE
    assert current_palette().bg == "#0B1220"
    assert app.theme_mode() == DARK_MODE

    app.apply_theme(LIGHT_MODE)
    app.update()
    assert current_mode() == LIGHT_MODE
    assert current_palette().bg == "#F4F6F8"

    app.geometry("720x540")
    app.update_idletasks()
    app.update()

    app._toggle_display()
    app.update()
    app._toggle_display()
    app.update()

    app.destroy()
    print("smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
