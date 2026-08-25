"""CSS injection and the responsive map/dashboard shell (`CLAUDE.md` §4.1 — UI layer).

Builds the full-viewport map plus a slide-in dashboard panel. The desktop-vs-mobile switch is
driven entirely by one CSS `@media` query (`PROJECT_SPEC.md` §10.1); no JS viewport
measurement is used. This module never touches Streamlit session state directly — any state
the shell needs is read by the caller through `src.ui.state` and passed in as a parameter.
"""

from __future__ import annotations

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

import config

# `st.container(key=...)` renders its wrapping div with CSS class `st-key-<key>` (Streamlit
# 1.29+), which is the only public hook for attaching a custom class to a container's DOM
# node. Naming the key with hyphens keeps the generated selector readable.
_DASHBOARD_PANEL_KEY = "dashboard-panel"
_DASHBOARD_PANEL_CLASS = f"st-key-{_DASHBOARD_PANEL_KEY}"

_CSS = f"""
<style>
:root {{
    --panel-desktop-width: {config.DASHBOARD_FRACTION * 100:.2f}vw;
    --panel-mobile-height: {config.DASHBOARD_FRACTION * 100:.2f}vh;
}}

/* Edge-to-edge map: remove Streamlit's default page padding and max-width. */
div.block-container {{
    padding: 0 !important;
    max-width: 100% !important;
}}

.{_DASHBOARD_PANEL_CLASS} {{
    position: fixed;
    top: 0;
    left: 0;
    width: var(--panel-desktop-width);
    height: 100vh;
    overflow-y: auto;
    background: #ffffff;
    box-shadow: 2px 0 8px rgba(0, 0, 0, 0.25);
    z-index: 900;
    animation: slide-in-left 0.25s ease-out;
}}

@keyframes slide-in-left {{
    from {{ transform: translateX(-100%); }}
    to {{ transform: translateX(0); }}
}}

@keyframes slide-in-bottom {{
    from {{ transform: translateY(100%); }}
    to {{ transform: translateY(0); }}
}}

/* Reserved for later phases (PROJECT_SPEC.md §8.4 layer checkboxes, §8.5 Reset button): map
   overlay widgets anchor to these classes instead of raw `position: fixed`, so they stay
   clear of the dashboard panel on both form factors with no further CSS changes. */
.map-overlay-top-right {{
    position: fixed;
    top: 12px;
    right: 12px;
    z-index: 1000;
}}

/* `.map-overlay-reset` is the reserved hook from the class comment above; `.st-key-reset-view`
   is the actual selector Streamlit emits for `st.container(key="reset-view")` (Phase 11's
   mechanism for pinning the Reset View button — there is no way to attach an arbitrary literal
   class to a native widget). Both carry the same rule so either usage works. */
.map-overlay-reset,
.st-key-reset-view {{
    position: fixed;
    right: 12px;
    bottom: 12px;
    z-index: 1000;
}}

@media (max-width: {config.MOBILE_BREAKPOINT_PX - 1}px), (orientation: portrait) {{
    .{_DASHBOARD_PANEL_CLASS} {{
        top: auto;
        left: 0;
        right: 0;
        bottom: 0;
        width: 100vw;
        height: var(--panel-mobile-height);
        animation-name: slide-in-bottom;
    }}

    .map-overlay-reset,
    .st-key-reset-view {{
        bottom: calc(var(--panel-mobile-height) + 12px);
    }}
}}
</style>
"""


def inject_css() -> None:
    """Inject the app's global stylesheet: edge-to-edge map, responsive dashboard panel.

    Safe to call on every rerun; Streamlit just re-renders the same `<style>` block.
    """
    st.markdown(_CSS, unsafe_allow_html=True)


def render_shell() -> tuple[DeltaGenerator, DeltaGenerator]:
    """Build the two top-level containers the rest of the app renders into.

    Returns:
        `(map_container, dashboard_container)`. The dashboard container carries the CSS class
        `inject_css()` styles into the slide-in panel described in `PROJECT_SPEC.md` §10.1.
    """
    map_container = st.container(key="map-container")
    dashboard_container = st.container(key=_DASHBOARD_PANEL_KEY)
    return map_container, dashboard_container


def viewport_padding(is_mobile: bool) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return Folium `fit_bounds` padding that keeps content clear of the dashboard panel.

    Uses fixed, conservative pixel estimates (`config.DESKTOP_PANEL_PX` /
    `config.MOBILE_PANEL_PX`) rather than live measurement, per `PROJECT_SPEC.md` §10.1.

    Args:
        is_mobile: Whether the dashboard panel is docked to the bottom (mobile) or the left
            (desktop). Callers source this from `src.ui.state.get_is_mobile()` — this module
            never reads Streamlit session state itself.

    Returns:
        `(padding_top_left, padding_bottom_right)` pixel pairs, in the shape
        `folium.Map.fit_bounds` expects.
    """
    if is_mobile:
        return (0, 0), (0, config.MOBILE_PANEL_PX)
    return (config.DESKTOP_PANEL_PX, 0), (0, 0)
