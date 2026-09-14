"""Windows entry point. Run: python server.py [--data-dir PATH]."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required; Python 3.12 is recommended for deployment.")
    parser = argparse.ArgumentParser(description="视觉比赛 TCP 裁判服务端")
    default = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "VisionCompetitionTCP" / "data"
    parser.add_argument("--data-dir", type=Path, default=default, help="本地可写数据目录")
    parser.add_argument("--display-host", default="127.0.0.1", help="展板网页监听IP；局域网用0.0.0.0")
    parser.add_argument("--display-port", type=int, default=9080, help="展板网页端口，默认9080（独立于TCP端口）")
    parser.add_argument("--no-display", action="store_true", help="仅启动裁判端，不启动网页展板")
    args = parser.parse_args()
    # Set Windows DPI awareness before creating any Tk widgets.
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    import tkinter as tk
    from tkinter import messagebox
    from competition.gui import App
    root = tk.Tk()
    try:
        App(root, args.data_dir.resolve(), display_host=args.display_host,
            display_port=args.display_port, display_enabled=not args.no_display)
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("服务端启动失败", str(exc), parent=root)
        root.destroy()
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
