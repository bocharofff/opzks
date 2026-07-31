"""Тесты кластеризации и IDW-интерполяции."""

import numpy as np
import pandas as pd
from scipy.ndimage import label

from src.wifi_heatmap.interpolate import (
    InterpolationParams,
    _build_track_corridor,
    cluster_points,
    idw_grid,
    interpolate_network,
)


def _track_df(coords, start="2026-07-21T10:00:00Z", step_s=1):
    """Строит DataFrame замеров вдоль трека: coords — список (lat, lon)."""
    times = pd.to_datetime([start], utc=True)[0] + pd.to_timedelta(
        np.arange(len(coords)) * step_s, unit="s"
    )
    return pd.DataFrame(
        {
            "lat": [c[0] for c in coords],
            "lon": [c[1] for c in coords],
            "rssi": [-50.0] * len(coords),
            "timestamp": times,
        }
    )


def test_cluster_points_separates_distant_groups():
    # две плотные группы, разнесённые примерно на 35 км (как реальные "мобильные" сети)
    lat = np.array([55.0, 55.0001, 55.0002, 56.0, 56.0001, 56.0002])
    lon = np.array([37.0, 37.0001, 37.0002, 37.0, 37.0001, 37.0002])
    labels = cluster_points(lat, lon, eps_m=100.0)
    assert len(set(labels[:3])) == 1
    assert len(set(labels[3:])) == 1
    assert labels[0] != labels[3]


def test_cluster_points_single_group_when_dense():
    lat = np.linspace(55.0, 55.0005, 10)
    lon = np.linspace(37.0, 37.0005, 10)
    labels = cluster_points(lat, lon, eps_m=100.0)
    assert len(set(labels)) == 1


def test_idw_returns_value_at_sample_point():
    lat = np.array([55.0, 55.001, 55.0005])
    lon = np.array([37.0, 37.0, 37.001])
    rssi = np.array([-40.0, -80.0, -60.0])
    params = InterpolationParams(grid_step_m=2.0, max_distance_m=50.0, neighbors=3)
    result = idw_grid(lat, lon, rssi, params=params)

    lat_axis = np.linspace(result.bounds[0], result.bounds[2], result.values.shape[0])
    lon_axis = np.linspace(result.bounds[1], result.bounds[3], result.values.shape[1])
    for plat, plon, pval in zip(lat, lon, rssi):
        i = int(np.argmin(np.abs(lat_axis - plat)))
        j = int(np.argmin(np.abs(lon_axis - plon)))
        assert abs(result.values[i, j] - pval) < 0.5


def test_cell_beyond_max_distance_is_nan():
    lat = np.array([55.0, 55.001])  # ~111 м друг от друга
    lon = np.array([37.0, 37.0])
    rssi = np.array([-50.0, -55.0])
    params = InterpolationParams(grid_step_m=3.0, max_distance_m=20.0, padding_fraction=0.05)
    result = idw_grid(lat, lon, rssi, params=params)
    assert np.isnan(result.values).any()  # середина разрыва вне покрытия
    assert not np.isnan(result.values).all()  # у самих точек покрытие есть


def test_grid_step_matches_requested_when_small():
    lat = np.array([55.0, 55.0002])
    lon = np.array([37.0, 37.0002])
    rssi = np.array([-50.0, -55.0])
    params = InterpolationParams(grid_step_m=3.0, max_distance_m=50.0)
    result = idw_grid(lat, lon, rssi, params=params)
    assert abs(result.grid_step_m - 3.0) < 1e-6


def test_max_cells_guard_coarsens_step():
    lat = np.linspace(55.0, 56.0, 50)
    lon = np.linspace(37.0, 37.5, 50)
    rssi = np.full(50, -50.0)
    params = InterpolationParams(grid_step_m=3.0, max_cells=100_000)
    result = idw_grid(lat, lon, rssi, params=params)
    assert result.values.size <= 100_000 * 1.5
    assert result.grid_step_m > 3.0


def test_track_corridor_keeps_coverage_connected_at_tight_radius():
    """Регрессия на исходную жалобу: узкий радиус не должен рвать ленту вдоль маршрута.

    Две детекции в ~33 м друг от друга: точечная маска при радиусе 10 м даёт два несвязных
    пятна, коридор вдоль трека — одну связную ленту.
    """
    df = _track_df([(55.0, 37.0), (55.0003, 37.0)])  # ~33 м по широте
    params = InterpolationParams(grid_step_m=2.0, max_distance_m=10.0, padding_fraction=0.1)

    corridor = _build_track_corridor(df, params)
    with_track = idw_grid(
        df.lat.to_numpy(), df.lon.to_numpy(), df.rssi.to_numpy(),
        params=params, mask_points=corridor,
    )
    without_track = idw_grid(
        df.lat.to_numpy(), df.lon.to_numpy(), df.rssi.to_numpy(), params=params,
    )

    assert label(~np.isnan(with_track.values))[1] == 1
    assert label(~np.isnan(without_track.values))[1] == 2


def test_track_corridor_covers_less_area_than_wide_point_buffer():
    # длинный трек (~290 м): только на таком масштабе отступ сетки определяется радиусом,
    # а не долей от размера bbox, и разница в ширине ленты становится видна
    df = _track_df([(55.0 + i * 0.00009, 37.0) for i in range(30)])

    tight = InterpolationParams(grid_step_m=3.0, max_distance_m=10.0)
    wide = InterpolationParams(grid_step_m=3.0, max_distance_m=50.0)

    corridor = _build_track_corridor(df, tight)
    narrow_grid = idw_grid(
        df.lat.to_numpy(), df.lon.to_numpy(), df.rssi.to_numpy(),
        params=tight, mask_points=corridor,
    )
    wide_grid = idw_grid(df.lat.to_numpy(), df.lon.to_numpy(), df.rssi.to_numpy(), params=wide)

    narrow_area = (~np.isnan(narrow_grid.values)).sum() * narrow_grid.grid_step_m ** 2
    wide_area = (~np.isnan(wide_grid.values)).sum() * wide_grid.grid_step_m ** 2
    assert narrow_area < wide_area / 2


def test_corridor_does_not_bridge_large_spatial_gap():
    # разрыв ~110 м при пороге 40 м — мост не строится
    df = _track_df([(55.0, 37.0), (55.001, 37.0)])
    params = InterpolationParams(grid_step_m=3.0, bridge_max_m=40.0, bridge_max_s=30.0)
    corridor = _build_track_corridor(df, params)
    assert len(corridor) == 2  # только сами детекции, без уплотнённого моста


def test_bridge_threshold_applies_to_path_length_not_straight_line():
    """Петля оператора между двумя близкими замерами не должна утаскивать коридор в сторону.

    Прямая между детекциями всего ~11 м, но оператор сделал крюк длиной больше порога —
    мост строиться не должен, иначе закрашенная зона уйдёт дальше обещанной полуширины.
    """
    df = _track_df([(55.0, 37.0), (55.0001, 37.0)], step_s=10)  # прямая ~11 м
    detour = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-07-21T10:00:03Z", "2026-07-21T10:00:06Z"], utc=True
            ),
            "lat": [55.00025, 55.00025],
            "lon": [37.0003, 37.0],
        }
    )
    params = InterpolationParams(grid_step_m=3.0, bridge_max_m=40.0, bridge_max_s=30.0)

    assert len(_build_track_corridor(df, params, detour)) == 2  # крюк длиннее 40 м — моста нет
    assert len(_build_track_corridor(df, params)) > 2  # без крюка прямой мост строится


def test_corridor_does_not_bridge_across_long_time_gap():
    """Защита от склейки разных заходов: та же точка, снятая через сутки, не соединяется."""
    df = _track_df([(55.0, 37.0), (55.00009, 37.0)], step_s=86400)
    params = InterpolationParams(grid_step_m=3.0, bridge_max_m=40.0, bridge_max_s=30.0)
    corridor = _build_track_corridor(df, params)
    assert len(corridor) == 2


def test_corridor_follows_operator_route_between_detections():
    """Мост должен идти по реальному треку оператора, а не срезать угол по прямой."""
    df = _track_df([(55.0, 37.0), (55.0, 37.0005)], step_s=10)  # детекции по долготе
    # оператор в промежутке заметно уклонялся к северу
    route = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-07-21T10:00:05Z"], utc=True),
            "lat": [55.0002],
            "lon": [37.00025],
        }
    )
    params = InterpolationParams(grid_step_m=3.0, bridge_max_m=60.0, bridge_max_s=30.0)

    straight = _build_track_corridor(df, params)
    via_route = _build_track_corridor(df, params, route)

    assert via_route[:, 0].max() > straight[:, 0].max() + 0.0001


def test_cluster_eps_independent_of_max_distance():
    """Порог кластеризации не должен следовать за узким радиусом коридора."""
    coords = [(55.0 + i * 0.00027, 37.0) for i in range(5)]  # шаги ~30 м
    df = _track_df(coords)
    params = InterpolationParams(max_distance_m=10.0, min_cluster_points=3)
    assert params.cluster_eps_m == 100.0

    grids = interpolate_network(df, params)
    assert len(grids) == 1
    assert grids[0].n_points == 5


def test_mask_mode_points_reproduces_legacy_behavior():
    df = _track_df([(55.0, 37.0), (55.00009, 37.0), (55.00018, 37.0)])
    params = InterpolationParams(grid_step_m=2.0, max_distance_m=10.0, mask_mode="points")

    from_pipeline = interpolate_network(df, params)[0]
    legacy = idw_grid(df.lat.to_numpy(), df.lon.to_numpy(), df.rssi.to_numpy(), params=params)

    np.testing.assert_array_equal(
        np.isnan(from_pipeline.values), np.isnan(legacy.values)
    )


def test_interpolate_network_drops_micro_clusters():
    rows = [[55.0 + i * 0.00001, 37.0, -50] for i in range(10)]  # плотный кластер из 10 точек
    rows.append([56.0, 37.0, -50])  # одиночная точка далеко — должна отброситься как шум
    df = pd.DataFrame(rows, columns=["lat", "lon", "rssi"])

    params = InterpolationParams(min_cluster_points=3)
    grids = interpolate_network(df, params)
    assert len(grids) == 1
    assert grids[0].n_points == 10
