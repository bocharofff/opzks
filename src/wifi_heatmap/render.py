"""Растеризация сеток, сборка folium-карты со слоями и легендой (FR-5, FR-6)."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import branca.colormap as bcm
import folium
import matplotlib
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd

from .interpolate import GridResult


@dataclass
class RenderParams:
    rssi_min: float = -90.0
    rssi_max: float = -30.0
    rssi_auto: bool = False
    opacity: float = 0.6
    cmap_name: str = "RdYlGn"
    # По умолчанию красный — сильный сигнал, зелёный — слабый (развёрнутое направление
    # относительно "родной" схемы matplotlib, где у RdYlGn 0=красный, 1=зелёный).
    reverse_cmap: bool = True


@dataclass
class NetworkLayer:
    display_name: str
    bssid: str
    grids: list[GridResult]
    points: Optional[pd.DataFrame] = field(default=None)  # колонки: lat, lon, rssi, timestamp


def _get_cmap(cmap_name: str, reverse: bool):
    cmap = matplotlib.colormaps[cmap_name]
    return cmap.reversed() if reverse else cmap


def grid_to_rgba(
    values: np.ndarray, *, vmin: float, vmax: float, cmap_name: str, reverse: bool = False
) -> np.ndarray:
    """Переводит матрицу RSSI в RGBA uint8: цвет по colormap, альфа=0 там, где NaN."""

    cmap = _get_cmap(cmap_name, reverse)
    span = (vmax - vmin) or 1.0
    with np.errstate(invalid="ignore"):
        norm = np.clip((values - vmin) / span, 0.0, 1.0)
    rgba = (cmap(np.nan_to_num(norm, nan=0.0)) * 255).astype(np.uint8)
    rgba[..., 3] = np.where(np.isnan(values), 0, 255).astype(np.uint8)
    return rgba


def _cmap_hex_colors(cmap_name: str, reverse: bool, n: int = 9) -> list[str]:
    cmap = _get_cmap(cmap_name, reverse)
    return [mcolors.rgb2hex(cmap(t)) for t in np.linspace(0.0, 1.0, n)]


def _layer_rssi_range(layer: NetworkLayer, render_params: RenderParams) -> tuple[float, float]:
    if not render_params.rssi_auto:
        return render_params.rssi_min, render_params.rssi_max

    values = np.concatenate([g.values[~np.isnan(g.values)] for g in layer.grids])
    if values.size == 0:
        return render_params.rssi_min, render_params.rssi_max
    vmin, vmax = float(values.min()), float(values.max())
    if vmin == vmax:
        vmin, vmax = vmin - 1.0, vmax + 1.0
    return vmin, vmax


def _add_title(m: folium.Map, title_text: str) -> None:
    escaped = html.escape(title_text)
    box = (
        '<div style="position: fixed; top: 10px; left: 60px; z-index: 9999; '
        'background: white; padding: 6px 12px; border: 1px solid #999; '
        'border-radius: 4px; font-family: sans-serif; font-size: 13px; '
        'max-width: 70%; box-shadow: 0 1px 4px rgba(0,0,0,0.3);">'
        f"{escaped}</div>"
    )
    m.get_root().html.add_child(folium.Element(box))


def build_map(
    layers: list[NetworkLayer], *, render_params: RenderParams, title: str, show_points: bool = False
) -> folium.Map:
    """Собирает folium-карту: по слою на сеть, легенда, LayerControl (FR-5, FR-6)."""

    all_bounds = [g.bounds for layer in layers for g in layer.grids]
    if not all_bounds:
        raise ValueError("Нет данных для отображения — ни одна сеть не дала ни одной сетки")

    lat_min = min(b[0] for b in all_bounds)
    lon_min = min(b[1] for b in all_bounds)
    lat_max = max(b[2] for b in all_bounds)
    lon_max = max(b[3] for b in all_bounds)

    m = folium.Map(tiles="OpenStreetMap", control_scale=True)
    m.fit_bounds([[lat_min, lon_min], [lat_max, lon_max]])

    # LayerControl строит список чекбоксов строго в порядке add_to(m) (см. folium.LayerControl.render).
    # Поэтому сначала добавляются ВСЕ тепловые слои, потом ВСЕ слои точек — это даёт предсказуемый
    # DOM-порядок [heat...][points...], на который опирается разбивка на два столбца ниже.
    n_points_layers = 0
    for i, layer in enumerate(layers):
        vmin, vmax = _layer_rssi_range(layer, render_params)
        layer_name = layer.display_name
        if render_params.rssi_auto:
            layer_name = f"{layer_name} [{vmin:.0f}..{vmax:.0f} дБм]"

        fg = folium.FeatureGroup(name=layer_name, show=(i == 0))
        for grid in layer.grids:
            rgba = grid_to_rgba(
                grid.values,
                vmin=vmin,
                vmax=vmax,
                cmap_name=render_params.cmap_name,
                reverse=render_params.reverse_cmap,
            )
            folium.raster_layers.ImageOverlay(
                image=rgba,
                bounds=[[grid.bounds[0], grid.bounds[1]], [grid.bounds[2], grid.bounds[3]]],
                origin="lower",
                opacity=render_params.opacity,
                pixelated=False,
            ).add_to(fg)
        fg.add_to(m)

    if show_points:
        for layer in layers:
            if layer.points is None or not len(layer.points):
                continue
            pts_fg = folium.FeatureGroup(name=f"{layer.display_name} · точки", show=False)
            for row in layer.points.itertuples(index=False):
                folium.CircleMarker(
                    location=[row.lat, row.lon],
                    radius=2,
                    color="#333333",
                    weight=1,
                    fill=True,
                    fill_opacity=0.7,
                    tooltip=f"RSSI {int(row.rssi)} дБм · {row.timestamp.isoformat()}",
                ).add_to(pts_fg)
            pts_fg.add_to(m)
            n_points_layers += 1

    legend = bcm.LinearColormap(
        colors=_cmap_hex_colors(render_params.cmap_name, render_params.reverse_cmap),
        vmin=render_params.rssi_min,
        vmax=render_params.rssi_max,
        caption="RSSI, дБм" + (" (для слоёв с авто-диапазоном — справочно)" if render_params.rssi_auto else ""),
    )
    legend.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    _add_title(m, title)

    if n_points_layers:
        _split_layer_control_into_columns(m, heat_count=len(layers))

    return m


def _split_layer_control_into_columns(m: folium.Map, *, heat_count: int) -> None:
    """Разносит чекбоксы LayerControl по двум колонкам: тепловые карты слева, точки справа.

    LayerControl уже построен в порядке [все тепловые слои][все слои точек] (см. build_map),
    поэтому первые `heat_count` DOM-элементов overlays-контейнера — тепловые слои, остальные —
    точки. Перемещение существующих узлов через appendChild не создаёт копии и не рвёт
    обработчики событий, которые Leaflet повесил на чекбоксы при их создании.
    """

    # root.header рендерится внутри <head> как есть — сюда можно класть теги <style> напрямую.
    css = """
    <style>
        .leaflet-control-layers-overlays { display: flex; gap: 14px; align-items: flex-start; }
        .leaflet-control-layers-overlays .wh-col { display: flex; flex-direction: column; }
        .leaflet-control-layers-overlays .wh-col-header {
            font-weight: 600; font-size: 11px; color: #666; margin-bottom: 3px;
            border-bottom: 1px solid #ddd; padding-bottom: 2px;
        }
    </style>
    """
    m.get_root().header.add_child(folium.Element(css))

    # root.script Figure сам оборачивает в <script>...</script> (см. Figure._template) —
    # добавлять сюда свои теги <script> нельзя: вложенный </script> обрывает разметку раньше
    # времени и ломает идущую следом инициализацию карты.
    js = f"""
    window.addEventListener('load', function() {{
        // LayerControl создаётся тем же общим блоком инициализации, что и эта функция,
        // но текстуально раньше (branca кладёт содержимое root.script перед кодом карты) —
        // поэтому DOM-манипуляция отложена до полной загрузки страницы.
        var container = document.querySelector('.leaflet-control-layers-overlays');
        if (!container) return;
        var items = Array.prototype.slice.call(container.children);
        var heatCount = {heat_count};

        var heatCol = document.createElement('div');
        heatCol.className = 'wh-col';
        var heatHeader = document.createElement('div');
        heatHeader.className = 'wh-col-header';
        heatHeader.textContent = 'Тепловые карты';
        heatCol.appendChild(heatHeader);

        var ptsCol = document.createElement('div');
        ptsCol.className = 'wh-col';
        var ptsHeader = document.createElement('div');
        ptsHeader.className = 'wh-col-header';
        ptsHeader.textContent = 'Точки замеров';
        ptsCol.appendChild(ptsHeader);

        items.forEach(function(item, i) {{
            (i < heatCount ? heatCol : ptsCol).appendChild(item);
        }});

        container.appendChild(heatCol);
        if (items.length > heatCount) container.appendChild(ptsCol);
    }});
    """
    m.get_root().script.add_child(folium.Element(js))


def save_map(m: folium.Map, path: str | Path) -> int:
    path = Path(path)
    m.save(str(path))
    return path.stat().st_size
