"""Тесты растеризации и сборки folium-карты."""

import numpy as np
import pandas as pd
import pytest

from src.wifi_heatmap.interpolate import GridResult
from src.wifi_heatmap.render import NetworkLayer, RenderParams, build_map, grid_to_rgba


def test_grid_to_rgba_alpha_matches_nan_mask():
    values = np.array([[np.nan, -30.0], [-90.0, -60.0]])
    rgba = grid_to_rgba(values, vmin=-90, vmax=-30, cmap_name="RdYlGn")
    assert rgba.dtype == np.uint8
    assert rgba[0, 0, 3] == 0
    assert rgba[0, 1, 3] == 255
    assert rgba[1, 0, 3] == 255
    assert rgba[1, 1, 3] == 255


def test_grid_to_rgba_weak_signal_reddish_strong_signal_greenish():
    # поведение примитива при reverse=False (родное направление matplotlib) — используется,
    # когда пользователь явно передал --no-reverse-cmap
    values = np.array([[-90.0, -30.0]])
    rgba = grid_to_rgba(values, vmin=-90, vmax=-30, cmap_name="RdYlGn")
    weak_r, weak_g, _, _ = rgba[0, 0]
    strong_r, strong_g, _, _ = rgba[0, 1]
    assert weak_r > weak_g
    assert strong_g > strong_r


def test_default_render_params_show_red_as_strong_signal():
    # поведение по умолчанию (в самом инструменте): красный — сильный сигнал, зелёный — слабый
    params = RenderParams()
    assert params.reverse_cmap is True

    values = np.array([[-90.0, -30.0]])
    rgba = grid_to_rgba(values, vmin=-90, vmax=-30, cmap_name=params.cmap_name, reverse=params.reverse_cmap)
    weak_r, weak_g, _, _ = rgba[0, 0]
    strong_r, strong_g, _, _ = rgba[0, 1]
    assert weak_g > weak_r  # слабый сигнал — зелёный
    assert strong_r > strong_g  # сильный сигнал — красный


def test_build_map_embeds_base64_png_and_layer_name():
    grid = GridResult(
        values=np.array([[-50.0, -60.0], [-70.0, np.nan]]),
        bounds=(55.0, 37.0, 55.001, 37.001),
        n_points=4,
        grid_step_m=3.0,
    )
    layer = NetworkLayer(display_name="TestNet", bssid="AA:BB:CC:DD:EE:FF", grids=[grid])
    m = build_map([layer], render_params=RenderParams(), title="test title xyz")
    html = m.get_root().render()
    assert "data:image/png;base64," in html
    assert "TestNet" in html
    assert "test title xyz" in html


def test_build_map_show_points_adds_points_layer():
    grid = GridResult(
        values=np.array([[-50.0]]),
        bounds=(55.0, 37.0, 55.0001, 37.0001),
        n_points=1,
        grid_step_m=3.0,
    )
    points = pd.DataFrame(
        {
            "lat": [55.00005],
            "lon": [37.00005],
            "rssi": [-50],
            "timestamp": pd.to_datetime(["2026-07-21T10:00:00Z"]),
        }
    )
    layer = NetworkLayer(display_name="TestNet", bssid="AA:BB:CC:DD:EE:FF", grids=[grid], points=points)
    m = build_map([layer], render_params=RenderParams(), title="t", show_points=True)
    html = m.get_root().render()
    # non-ASCII в имени слоя folium/Jinja экранирует как \uXXXX (валидно для браузера,
    # см. живую проверку в браузере), поэтому сравниваем по числу вхождений ASCII-префикса
    assert html.count("TestNet") == 2  # основной тепловой слой + слой точек


def test_build_map_raises_without_any_grids():
    with pytest.raises(ValueError):
        build_map([], render_params=RenderParams(), title="t")


def _two_layers_with_points():
    layers = []
    for name in ("NetA", "NetB"):
        grid = GridResult(
            values=np.array([[-50.0]]), bounds=(55.0, 37.0, 55.0001, 37.0001), n_points=1, grid_step_m=3.0,
        )
        points = pd.DataFrame(
            {
                "lat": [55.00005], "lon": [37.00005], "rssi": [-50],
                "timestamp": pd.to_datetime(["2026-07-21T10:00:00Z"]),
            }
        )
        layers.append(NetworkLayer(display_name=name, bssid=name, grids=[grid], points=points))
    return layers


def test_build_map_splits_heat_and_points_into_two_columns_when_show_points():
    m = build_map(_two_layers_with_points(), render_params=RenderParams(), title="t", show_points=True)
    html = m.get_root().render()
    assert "wh-col" in html
    assert "Тепловые карты" in html
    assert "Точки замеров" in html
    assert "heatCount = 2" in html  # число тепловых слоёв, известное на момент сборки


def test_build_map_does_not_inject_column_split_without_show_points():
    m = build_map(_two_layers_with_points(), render_params=RenderParams(), title="t", show_points=False)
    html = m.get_root().render()
    assert "wh-col" not in html
