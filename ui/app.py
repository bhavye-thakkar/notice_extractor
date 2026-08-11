"""Application entry point and the Status Log panel.

The rest of the window (gallery, preview, controls) lives in core.py next to
the pipeline it drives; this module owns the log panel and the startup
sequence, and is the import site the launcher uses.

Status Log fixes (v1.36) - it used to be there but unusable:
  * ScrolledText instead of a hand-wired Text + Scrollbar, so the scrollbar
    is always attached and always the right height.
  * The pane keeps a real width.  In a ttk.PanedWindow a pane that is
    forgotten and re-inserted comes back at zero width - the log looked
    "closed" even though it was open - so the sash is put back explicitly.
  * A minimum width, so dragging another pane can never squash it to a
    sliver again.
  * Auto-scroll to the newest line, unless the user has scrolled up to read
    something (then it stays put instead of yanking the view away).
  * Lines are drawn in batches (v1.37), because eight agents log faster than
    Tk can paint and the per-line path made every button feel stuck.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import scrolledtext, ttk
from typing import Callable, List, Optional, Tuple

from .. import config

#: Width the log pane opens at, and the narrowest it may be squeezed to.
LOG_PANE_WIDTH = 360
LOG_PANE_MIN_WIDTH = 240
#: How often queued log lines are drawn.  Long enough to batch a burst,
#: short enough that the log still reads as live.
LOG_FLUSH_MS = 120


class StatusLogPanel(ttk.LabelFrame):
    """Scrolling status log with coloured levels.

    Behaves like a side navbar: the ✕ in its corner collapses it (the
    Application re-opens it from the ☰ Log button next to the progress bar).
    """

    def __init__(self, master, on_close: Optional[Callable[[], None]] = None):
        super().__init__(master, text="Status Log", width=LOG_PANE_WIDTH)
        # Keep the requested width even though the Text inside is smaller:
        # without this the pane collapses to the widget's own request.
        self.grid_propagate(False)
        self.pack_propagate(False)

        self._text = scrolledtext.ScrolledText(
            self, width=42, height=10, wrap="word", state="disabled",
            relief="sunken", borderwidth=1, font=("Consolas", 9),
            background="#ffffff", padx=4, pady=2)
        self._text.pack(fill="both", expand=True, padx=6, pady=(6, 6))

        if on_close is not None:
            close = ttk.Button(self, text="✕", width=2, command=on_close)
            # Inside the frame, clear of the label text - the old placement
            # (y=-6) put half the button above the panel.
            close.place(relx=1.0, rely=0.0, x=-8, y=2, anchor="ne")

        for level, colour in (("info", "#1a1a1a"), ("dim", "#767676"),
                              ("warn", "#a86500"), ("error", "#c00000"),
                              ("success", "#0a6b0a")):
            self._text.tag_configure(level, foreground=colour)

        #: Lines waiting to be drawn, and the pending flush callback.
        self._pending: List[Tuple[str, str]] = []
        self._flush_job = None

    # -- logging --------------------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        """Queue a line.  Drawing is batched - see _flush.

        Eight edition agents log faster than Tk can paint, and the old
        one-insert-per-line path re-measured the whole widget every time
        (`index("end-1c")` walks it), which is what made buttons feel stuck
        during a run."""
        self._pending.append((message, level))
        if self._flush_job is None:
            try:
                self._flush_job = self.after(LOG_FLUSH_MS, self._flush)
            except tk.TclError:               # window closing
                self._pending.clear()

    def _flush(self) -> None:
        """Draw everything queued in as few inserts as possible."""
        self._flush_job = None
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        follow = self._at_bottom()
        try:
            self._text.configure(state="normal")
            # Consecutive lines of the same level share one insert: a burst
            # from a single agent is usually one colour all the way down.
            run_level = pending[0][1]
            chunk: List[str] = []
            for message, level in pending:
                if level != run_level and chunk:
                    self._text.insert("end", "".join(chunk), (run_level,))
                    chunk = []
                run_level = level
                chunk.append(message + "\n")
            if chunk:
                self._text.insert("end", "".join(chunk), (run_level,))

            # Trim once per flush, not once per line.
            line_count = int(self._text.index("end-1c").split(".")[0])
            if line_count > config.LOG_MAX_LINES:
                self._text.delete(
                    "1.0", f"{line_count - config.LOG_MAX_LINES}.0")
            self._text.configure(state="disabled")
            if follow:
                self._text.yview_moveto(1.0)
        except tk.TclError:
            pass                              # window went away mid-flush

    def clear(self) -> None:
        self._pending.clear()
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")

    def _at_bottom(self) -> bool:
        """True while the newest line is in view.  Scrolling up to read an
        error must not be undone by the next log line."""
        try:
            return self._text.yview()[1] > 0.999
        except tk.TclError:
            return True


def run(argv=None) -> int:
    """Start the desktop application (used by main.py)."""
    from .. import core
    from .. import scrapers

    if core.missing_dependencies():
        return core.run_dependency_bootstrap()
    scrapers.start_background_load()
    return core.main(scrapers)
