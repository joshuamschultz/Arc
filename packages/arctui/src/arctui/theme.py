"""Theme constants and CSS for ArcTUI.

Loaded at startup; colour values can be overridden via arcagent config
``[tui.theme]`` section.  Falls back to the Arc default palette when no
override is present.

Why separate from app.py:
  The theme module is imported by both ``app.py`` (for the TCSS string) and
  potentially external tooling that wants to interrogate palette colours
  without instantiating a full Textual App.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Colour tokens
# ---------------------------------------------------------------------------

# Base palette — Arc dark-terminal aesthetic
COLOR_BG_DARK = "#0d1117"        # main background
COLOR_BG_PANEL = "#161b22"       # panel / sidebar background
COLOR_BG_BORDER = "#30363d"      # border / separator
COLOR_ACCENT_BLUE = "#58a6ff"    # primary accent (Arc brand blue)
COLOR_ACCENT_GREEN = "#3fb950"   # success / tool complete
COLOR_ACCENT_YELLOW = "#d29922"  # warning / pending
COLOR_ACCENT_RED = "#f85149"     # error
COLOR_TEXT_PRIMARY = "#e6edf3"   # body text
COLOR_TEXT_MUTED = "#8b949e"     # secondary / metadata text
COLOR_CURSOR = "#58a6ff"         # input cursor and selection highlight


def build_tcss() -> str:
    """Return the Textual CSS string for ArcTUI.

    Textual uses a CSS-like language (TCSS).  Keeping it in one place
    avoids the need for a separate ``.tcss`` file and keeps the package
    self-contained.
    """
    return f"""
Screen {{
    background: {COLOR_BG_DARK};
    color: {COLOR_TEXT_PRIMARY};
    layout: grid;
    grid-size: 2 2;
    grid-columns: 3fr 1fr;
    grid-rows: 1fr auto;
}}

/* ---- Transcript panel (left-top) ---- */
#transcript {{
    column-span: 1;
    row-span: 1;
    border: solid {COLOR_BG_BORDER};
    padding: 0 1;
    background: {COLOR_BG_DARK};
    overflow-y: auto;
}}

#transcript-title {{
    background: {COLOR_BG_PANEL};
    color: {COLOR_ACCENT_BLUE};
    padding: 0 1;
    text-style: bold;
}}

/* ---- Activity panel (right-top) ---- */
#activity {{
    column-span: 1;
    row-span: 1;
    border: solid {COLOR_BG_BORDER};
    padding: 0 1;
    background: {COLOR_BG_PANEL};
    overflow-y: auto;
}}

#activity-title {{
    background: {COLOR_BG_PANEL};
    color: {COLOR_ACCENT_YELLOW};
    padding: 0 1;
    text-style: bold;
}}

/* ---- Input composer (bottom, full width) ---- */
#composer {{
    column-span: 2;
    row-span: 1;
    border-top: solid {COLOR_BG_BORDER};
    background: {COLOR_BG_PANEL};
    height: auto;
    min-height: 3;
    max-height: 7;
    padding: 0 1;
}}

/* ---- Message rows ---- */
.msg-user {{
    color: {COLOR_ACCENT_BLUE};
    text-style: bold;
}}

.msg-assistant {{
    color: {COLOR_TEXT_PRIMARY};
}}

.msg-system {{
    color: {COLOR_TEXT_MUTED};
    text-style: italic;
}}

.msg-tool {{
    color: {COLOR_ACCENT_GREEN};
}}

.msg-error {{
    color: {COLOR_ACCENT_RED};
}}

/* ---- Activity entries ---- */
.activity-start {{
    color: {COLOR_ACCENT_YELLOW};
}}

.activity-complete {{
    color: {COLOR_ACCENT_GREEN};
}}

.activity-error {{
    color: {COLOR_ACCENT_RED};
}}

/* ---- Modal overlays ---- */
ApprovalModal {{
    align: center middle;
}}

ApprovalModal > .modal-container {{
    width: 60;
    height: auto;
    background: {COLOR_BG_PANEL};
    border: solid {COLOR_ACCENT_BLUE};
    padding: 1 2;
}}

ApprovalModal .modal-title {{
    text-style: bold;
    color: {COLOR_ACCENT_YELLOW};
    text-align: center;
}}

ApprovalModal .modal-body {{
    color: {COLOR_TEXT_PRIMARY};
    margin: 1 0;
}}

ApprovalModal .modal-buttons {{
    align: center middle;
    height: 3;
}}

/* ---- Completion popup ---- */
#completion-list {{
    background: {COLOR_BG_PANEL};
    border: solid {COLOR_BG_BORDER};
    height: auto;
    max-height: 10;
    display: none;
}}
"""
