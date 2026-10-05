"""
Generate a heatmap PNG from alert data using folium + Playwright (async).
"""

import os
import tempfile

import folium
from folium.plugins import HeatMap
from playwright.async_api import async_playwright

_PRESETS = {
    None:        {"center": (46.60, 31.20), "zoom": 9,  "label": "Одеса + Миколаїв"},
    "odesa":     {"center": (46.38, 30.65), "zoom": 11, "label": "Одеса / Чорноморськ"},
    "mykolaiv":  {"center": (46.97, 32.00), "zoom": 11, "label": "Миколаїв"},
}

# Max circle radius (px) and min — scaled by mention count
_MARKER_MAX_R = 18
_MARKER_MIN_R = 5
# Show text labels only for top N locations
_LABEL_TOP_N = 15


async def render_heatmap(
    points: list[tuple[float, float, float]],
    days: int,
    city: str | None = None,
    mentions: list[dict] | None = None,
    width: int = 900,
    height: int = 700,
) -> bytes:
    """
    Render a heatmap image with optional location markers.
    points:   [(lat, lon, weight)]
    mentions: [{alias, lat, lon, count, city, district}] sorted by count desc
    Returns PNG bytes.
    """
    preset = _PRESETS.get(city, _PRESETS[None])
    m = folium.Map(
        location=preset["center"],
        zoom_start=preset["zoom"],
        tiles="CartoDB positron",
        width=width,
        height=height,
    )

    # Heatmap layer
    if points:
        HeatMap(
            data=[[lat, lon, w] for lat, lon, w in points],
            min_opacity=0.3,
            max_zoom=13,
            radius=25,
            blur=20,
            gradient={0.2: "blue", 0.5: "lime", 0.8: "orange", 1.0: "red"},
        ).add_to(m)

    # Markers layer
    if mentions:
        max_count = mentions[0]["count"] if mentions else 1
        for i, loc in enumerate(mentions):
            count = loc["count"]
            radius = _MARKER_MIN_R + (_MARKER_MAX_R - _MARKER_MIN_R) * (count / max_count) ** 0.5

            w = loc.get("max_weight", 1.0)
            fill_opacity = max(0.1, w * 0.85)
            label_opacity = max(0.15, w)

            folium.CircleMarker(
                location=[loc["lat"], loc["lon"]],
                radius=radius,
                color=f"rgba(255,255,255,{min(1.0, w + 0.15):.2f})",
                weight=1.5,
                fill=True,
                fill_color="#1a1a2e",
                fill_opacity=fill_opacity,
                tooltip=f"{loc['alias']} ({count})",
            ).add_to(m)

            # Permanent text label for top N
            if i < _LABEL_TOP_N:
                label_html = (
                    f'<div style="'
                    f'font-size:11px;font-weight:600;'
                    f'color:rgba(26,26,46,{label_opacity:.2f});'
                    f'white-space:nowrap;text-shadow:1px 1px 0 #fff,-1px -1px 0 #fff,'
                    f'1px -1px 0 #fff,-1px 1px 0 #fff;'
                    f'">{loc["alias"]}</div>'
                )
                folium.Marker(
                    location=[loc["lat"], loc["lon"]],
                    icon=folium.DivIcon(
                        html=label_html,
                        icon_size=(160, 20),
                        icon_anchor=(-int(radius) - 2, 10),
                    ),
                ).add_to(m)

    total_pts = len(points) if points else 0
    title_html = (
        f'<div style="position:fixed;top:10px;left:50%;transform:translateX(-50%);'
        f'background:rgba(255,255,255,0.88);padding:6px 14px;border-radius:6px;'
        f'font-size:14px;font-family:sans-serif;z-index:9999;">'
        f'{preset["label"]} · {days} дн. · {total_pts} точок</div>'
    )
    m.get_root().html.add_child(folium.Element(title_html))

    with tempfile.TemporaryDirectory() as tmpdir:
        html_path = os.path.join(tmpdir, "map.html")
        png_path = os.path.join(tmpdir, "map.png")
        m.save(html_path)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": width, "height": height})
            await page.goto(f"file://{html_path}")
            await page.wait_for_timeout(1200)
            await page.screenshot(path=png_path)
            await browser.close()

        with open(png_path, "rb") as f:
            return f.read()
