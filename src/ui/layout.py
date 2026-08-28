"""CSS injection and the responsive map/dashboard shell (`CLAUDE.md` §4.1 — UI layer).

Builds the full-viewport map plus a slide-in dashboard panel. The desktop-vs-mobile switch is
driven entirely by one CSS `@media` query (`PROJECT_SPEC.md` §10.1); no JS viewport
measurement is used. This module never touches Streamlit session state directly — any state
the shell needs is read by the caller through `src.ui.state` and passed in as a parameter.
"""

from __future__ import annotations

import streamlit as st
from streamlit.components.v1 import html as _components_html
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
    --panel-min-width: {config.DASHBOARD_MIN_FRACTION * 100:.2f}vw;
    --panel-max-width: {config.DASHBOARD_MAX_FRACTION * 100:.2f}vw;
    --panel-mobile-height: {config.DASHBOARD_FRACTION * 100:.2f}vh;
    --metric-value-min: {config.DASHBOARD_METRIC_VALUE_MIN_REM}rem;
    --metric-value-max: {config.DASHBOARD_METRIC_VALUE_MAX_REM}rem;
    --dash-pad-left: {config.DASHBOARD_PANEL_PAD_LEFT_REM}rem;
    --dash-pad-right: {config.DASHBOARD_PANEL_PAD_RIGHT_REM}rem;
}}

/* Edge-to-edge map: remove Streamlit's default page padding and max-width. */
div.block-container {{
    padding: 0 !important;
    max-width: 100% !important;
}}

/* Hide Streamlit's default top chrome (the white header bar with the running-man / menu).
   The map is full-viewport, so the bar just occludes the top of the tiles and the layer
   controls. The toolbar keeps its own hook in case a future phase needs the "Rerun" menu. */
[data-testid="stHeader"] {{
    display: none !important;
}}

[data-testid="stAppViewContainer"] > .main {{
    top: 0 !important;
}}

.{_DASHBOARD_PANEL_CLASS} {{
    position: fixed;
    top: 0;
    left: 0;
    /* `--panel-desktop-width` is driven live by the drag handle (inject_dashboard_resize);
       clamp() is the belt-and-suspenders bound in case the handle's JS never runs. */
    width: clamp(var(--panel-min-width), var(--panel-desktop-width), var(--panel-max-width));
    height: 100vh;
    overflow-y: auto;
    /* Small inner buffer so dashboard text and full-width plots don't crowd the panel edges
       (config.DASHBOARD_PANEL_PAD_*). border-box keeps the padding inside the clamp() width,
       so the panel's left edge and the drag handle's anchor are unchanged. */
    padding-left: var(--dash-pad-left);
    padding-right: var(--dash-pad-right);
    box-sizing: border-box;
    background: #ffffff;
    box-shadow: 2px 0 8px rgba(0, 0, 0, 0.25);
    z-index: 900;
    animation: slide-in-left 0.25s ease-out;
    /* Query container for the metric-row reflow below: the panel width tracks the drag handle,
       which no viewport media query can observe. `inline-size` contains layout/style only, not
       block size, so the panel still grows with its content and scrolls. */
    container: wfm-dash / inline-size;
}}

/* Dashboard text at narrow panel widths (PROJECT_SPEC.md §10; config.DASHBOARD_METRIC_*).
   `st.metric` normally truncates its label to a hover-only ellipsis ("Total Ener…") and holds
   all four Fleet metrics on one row however narrow the panel is dragged. Instead: let the
   label wrap, scale the value down to `--metric-value-min` via a container-relative clamp
   (top of the range is Streamlit's 2.25rem default, so the wide layout is untouched), and
   reflow the row 4-up -> 2-up -> 1-up. The `:has()` scope keeps this to metric rows, leaving
   the Turbine Dashboard's status-count columns and the scatter dropdowns alone. */
.{_DASHBOARD_PANEL_CLASS} [data-testid="stMetricLabel"],
.{_DASHBOARD_PANEL_CLASS} [data-testid="stMetricLabel"] > div,
.{_DASHBOARD_PANEL_CLASS} [data-testid="stMetricLabel"] p {{
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: clip !important;
    -webkit-line-clamp: unset !important;
    overflow-wrap: anywhere;
    max-width: 100%;
}}

.{_DASHBOARD_PANEL_CLASS} [data-testid="stMetricValue"] {{
    font-size: clamp(var(--metric-value-min), 7cqw, var(--metric-value-max));
    line-height: 1.2;
    overflow-wrap: anywhere;
}}

@container wfm-dash (max-width: {config.DASHBOARD_METRIC_2UP_MAX_PX}px) {{
    .{_DASHBOARD_PANEL_CLASS}
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"])
        > [data-testid="stColumn"] {{
        flex: 1 1 40% !important;
        width: auto !important;
        min-width: 0 !important;
    }}
}}

@container wfm-dash (max-width: {config.DASHBOARD_METRIC_1UP_MAX_PX}px) {{
    .{_DASHBOARD_PANEL_CLASS}
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"])
        > [data-testid="stColumn"] {{
        flex-basis: 100% !important;
        width: 100% !important;
    }}
}}

@keyframes slide-in-left {{
    from {{ transform: translateX(-100%); }}
    to {{ transform: translateX(0); }}
}}

@keyframes slide-in-bottom {{
    from {{ transform: translateY(100%); }}
    to {{ transform: translateY(0); }}
}}

/* Drag-to-resize handle pinned to the dashboard panel's right edge. It is a standalone fixed
   element (Streamlit gives no hook to inject a child into the panel's own DOM); its listeners
   are wired by inject_dashboard_resize(). Hidden on the mobile layout below. */
#wfm-resize-handle {{
    position: fixed;
    top: 0;
    left: clamp(var(--panel-min-width), var(--panel-desktop-width), var(--panel-max-width));
    width: 12px;
    height: 100vh;
    margin-left: -6px;
    cursor: col-resize;
    z-index: 950;
}}

#wfm-resize-handle::after {{
    content: "";
    position: absolute;
    left: 5px;
    top: 50%;
    transform: translateY(-50%);
    width: 3px;
    height: 44px;
    border-radius: 3px;
    background: rgba(0, 0, 0, 0.28);
}}

#wfm-resize-handle:hover::after {{
    background: rgba(0, 0, 0, 0.5);
}}

/* Full-viewport shield added only while dragging, so the pointer stream isn't swallowed by
   the Folium map iframe as the cursor passes over it. */
#wfm-resize-shield {{
    position: fixed;
    inset: 0;
    z-index: 9998;
    cursor: col-resize;
}}

/* Zero-size island that hosts inject_dashboard_resize()'s <script>; keep it out of the flow. */
.st-key-wfm-resize-helper {{
    position: fixed;
    width: 0;
    height: 0;
    overflow: hidden;
}}

/* `.map-overlay-top-right` is the reserved hook from Phase 9; `.st-key-map-controls` is the
   actual selector Streamlit emits for `st.container(key="map-controls")` (the Wind /
   Temperature / Forecast toggles, PROJECT_SPEC.md §8.4). Both carry the same rule so either
   usage works, mirroring `.map-overlay-reset` / `.st-key-reset-view` below. The toggles sit
   in one horizontal row pinned to the dashboard panel's top-right corner and floating just
   past its right edge — the same anchor as the Reset View button, so they track the panel as
   it is resized; a white text halo keeps the labels legible straight over the map tiles. */
.map-overlay-top-right,
.st-key-map-controls {{
    position: fixed;
    top: 8px;
    left: clamp(var(--panel-min-width), var(--panel-desktop-width), var(--panel-max-width));
    transform: translateX(12px);
    width: max-content;
    max-width: 96vw;
    z-index: 1000;
}}

.st-key-map-controls [data-testid="stVerticalBlock"] {{
    flex-direction: row;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-start;
    gap: 0.25rem 1.5rem;
}}

.st-key-map-controls [data-testid="stVerticalBlock"] > div,
.st-key-map-controls [data-testid="stElementContainer"] {{
    width: auto;
}}

.st-key-map-controls [data-testid="stElementContainer"]:has(.stCaption) {{
    flex-basis: 100%;
}}

.st-key-map-controls .stCheckbox p {{
    font-weight: 600;
    text-shadow:
        0 0 3px rgba(255, 255, 255, 0.95),
        0 1px 3px rgba(255, 255, 255, 0.95);
}}

.st-key-map-controls .stCaption {{
    text-align: center;
    text-shadow: 0 1px 3px rgba(255, 255, 255, 0.95);
}}

/* `.map-overlay-reset` is the reserved hook from the class comment above; `.st-key-reset-view`
   is the actual selector Streamlit emits for `st.container(key="reset-view")` (Phase 11's
   mechanism for pinning the Reset View button — there is no way to attach an arbitrary literal
   class to a native widget). Anchored to the dashboard panel's bottom-right corner and
   floating just past its right edge, over the map; tracks the panel as it is resized. */
.map-overlay-reset,
.st-key-reset-view {{
    position: fixed;
    left: clamp(var(--panel-min-width), var(--panel-desktop-width), var(--panel-max-width));
    bottom: 12px;
    transform: translateX(12px);
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

    #wfm-resize-handle {{
        display: none;
    }}

    /* The dashboard panel docks to the bottom on mobile, so its right edge is no longer an
       anchor — return the layer controls to the map's own top-left corner. */
    .map-overlay-top-right,
    .st-key-map-controls {{
        left: 8px;
        transform: none;
    }}

    .map-overlay-reset,
    .st-key-reset-view {{
        left: auto;
        right: 12px;
        transform: none;
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


_RESIZE_JS = f"""
<script>
(function () {{
  try {{
    var doc = window.parent.document;
    var root = doc.documentElement;
    var store = window.parent.localStorage;
    var KEY = "wfm-dashboard-fraction";
    var MIN = {config.DASHBOARD_MIN_FRACTION};
    var MAX = {config.DASHBOARD_MAX_FRACTION};

    function clamp(f) {{ return Math.min(MAX, Math.max(MIN, f)); }}
    function apply(f) {{
      root.style.setProperty("--panel-desktop-width", (f * 100).toFixed(3) + "vw");
    }}

    // Re-applied on every rerun so the panel keeps its width after a map click / drill-down.
    var saved = parseFloat(store.getItem(KEY));
    if (!isNaN(saved)) {{ apply(clamp(saved)); }}

    // Streamlit re-executes this island on every rerun; wire the handle's listeners only once.
    if (doc.getElementById("wfm-resize-handle")) {{ return; }}

    var handle = doc.createElement("div");
    handle.id = "wfm-resize-handle";
    handle.title = "Drag to resize the dashboard";
    doc.body.appendChild(handle);

    var dragging = false;
    var lastFraction = NaN;
    var shield = null;

    handle.addEventListener("mousedown", function (e) {{
      dragging = true;
      e.preventDefault();
      shield = doc.createElement("div");
      shield.id = "wfm-resize-shield";
      doc.body.appendChild(shield);
    }});

    doc.addEventListener("mousemove", function (e) {{
      if (!dragging) {{ return; }}
      lastFraction = clamp(e.clientX / window.parent.innerWidth);
      apply(lastFraction);
    }});

    doc.addEventListener("mouseup", function () {{
      if (!dragging) {{ return; }}
      dragging = false;
      if (shield) {{ shield.remove(); shield = null; }}
      if (!isNaN(lastFraction)) {{ store.setItem(KEY, lastFraction.toFixed(4)); }}
    }});
  }} catch (err) {{
    /* Cross-origin parent document (non-default component hosting): resize just stays off. */
  }}
}})();
</script>
"""


def inject_dashboard_resize() -> None:
    """Wire the desktop dashboard panel's drag-to-resize handle (`PROJECT_SPEC.md` §10.1).

    Dragging the handle at the panel's right edge updates the `--panel-desktop-width` CSS
    custom property — which the panel, the Reset View button, and the layer-controls row all
    key off — and persists the chosen fraction of viewport width in the browser's
    `localStorage`, so the width survives Streamlit reruns. The fraction is clamped to
    `config.DASHBOARD_MIN_FRACTION`..`config.DASHBOARD_MAX_FRACTION`.

    Runs as a zero-size `components.v1.html` island that reaches into `window.parent`: the
    only way to execute JS from Streamlit, since `st.markdown` strips `<script>`. No-ops on
    the mobile layout, where the handle is hidden by the stylesheet's media query.
    """
    with st.container(key="wfm-resize-helper"):
        _components_html(_RESIZE_JS, height=0)


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
