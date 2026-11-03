"""Hold an image selection in a GTK event loop until the sender releases it."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def main() -> None:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

    image_path, ready_path = map(Path, sys.argv[1:])
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    image = GdkPixbuf.Pixbuf.new_from_file(str(image_path))
    clipboard.set_image(image)

    def ready() -> bool:
        # The private, per-send marker is published from the running event
        # loop. The parent must not mistake an old image selection for ours.
        temporary = ready_path.with_suffix(".tmp")
        temporary.write_text("ready", encoding="ascii")
        os.replace(temporary, ready_path)
        return False

    GLib.idle_add(ready)
    Gtk.main()


if __name__ == "__main__":
    main()
