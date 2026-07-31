"""Кластеризация точек и интерполяция сигнала на регулярную сетку (FR-4)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

import numpy as np
import pandas as pd
from pyproj import CRS, Transformer
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

logger = logging.getLogger(__name__)

METERS_PER_DEGREE_LAT = 111_320.0


@dataclass
class InterpolationParams:
    grid_step_m: float = 3.0
    padding_fraction: float = 0.1
    idw_power: float = 2.0
    max_distance_m: float = 10.0  # полуширина коридора вокруг трека
    neighbors: int = 12
    idw_radius_m: Optional[float] = None
    min_cluster_points: int = 3
    # Порог кластеризации НЕ выводится из max_distance_m: при узком коридоре это
    # раздробило бы сети на микрокластеры и отбросило большую часть замеров.
    cluster_eps_m: float = 100.0
    max_cells: int = 4_000_000
    mask_mode: str = "track"  # "track" — коридор вдоль маршрута, "points" — буфер вокруг точек
    bridge_max_m: float = 40.0  # длина пути между соседними детекциями не больше
    bridge_max_s: float = 30.0  # и пауза не дольше (защита от склейки разных заходов)


@dataclass
class GridResult:
    values: np.ndarray  # (rows, cols); NaN = нет данных; строка 0 = юг (наименьшая широта)
    bounds: tuple[float, float, float, float]  # lat_min, lon_min, lat_max, lon_max
    n_points: int
    grid_step_m: float


class Interpolator(Protocol):
    """Точка расширения под альтернативный метод интерполяции (например, Kriging, см. NFR-2)."""

    def __call__(
        self,
        lat: np.ndarray,
        lon: np.ndarray,
        values: np.ndarray,
        *,
        params: InterpolationParams,
    ) -> GridResult: ...


def _project_local(lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray, Transformer]:
    """Проецирует координаты в локальную равнопромежуточную систему (метры), центр — медиана точек."""

    lat0 = float(np.median(lat))
    lon0 = float(np.median(lon))
    crs = CRS.from_proj4(f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m")
    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = transformer.transform(lon, lat)
    return np.asarray(x, dtype=float), np.asarray(y, dtype=float), transformer


def _densify_polyline(pts: np.ndarray, spacing: float) -> np.ndarray:
    """Равномерно уплотняет ломаную, чтобы расстояние по KDTree приближало расстояние до отрезка."""

    if len(pts) < 2:
        return pts

    out = [pts[0]]
    for a, b in zip(pts[:-1], pts[1:]):
        length = float(np.hypot(*(b - a)))
        n_steps = max(int(np.ceil(length / spacing)), 1)
        for t in np.linspace(0.0, 1.0, n_steps + 1)[1:]:
            out.append(a + t * (b - a))
    return np.asarray(out)


def _build_track_corridor(
    sub_df: pd.DataFrame,
    params: InterpolationParams,
    route: Optional[pd.DataFrame] = None,
) -> np.ndarray:
    """Строит точки трека сети: сами замеры плюс мосты между соседними по времени детекциями.

    Замеры делались только вдоль маршрута оператора, поэтому маска «есть данные» должна быть
    коридором вдоль этого маршрута, а не объединением окружностей вокруг отдельных точек:
    при узком радиусе окружности перестают пересекаться и лента распадается на куски.

    Мост между двумя детекциями строится, только если они близки и по расстоянию, и по времени.
    Временное условие принципиально: без него мост протянулся бы между разными заходами
    (одна и та же точка, снятая в разные дни).

    Возвращает массив (N, 2) в географических координатах (lat, lon): проецирование обратно
    в метры делает вызывающая сторона тем же преобразованием, что и для сетки.
    """

    sub_df = sub_df.sort_values("timestamp")
    lat = sub_df["lat"].to_numpy()
    lon = sub_df["lon"].to_numpy()

    if len(sub_df) < 2:
        return np.column_stack([lat, lon])

    # Уплотнение должно идти с равным шагом в метрах, поэтому работаем в локальной проекции
    # и в конце возвращаемся в градусы.
    px, py, transformer = _project_local(lat, lon)
    pts = np.column_stack([px, py])
    timestamps = sub_df["timestamp"].tolist()
    segments: list[list[np.ndarray]] = [[pts[0]]]

    for i in range(len(pts) - 1):
        gap_s = (timestamps[i + 1] - timestamps[i]).total_seconds()
        if gap_s > params.bridge_max_s:
            segments.append([pts[i + 1]])  # разная сессия: коридор обрывается
            continue

        # Если между детекциями есть промежуточные позиции оператора, мост ведём по ним —
        # коридор повторяет изгибы маршрута, а не срезает углы через здания.
        chain = [pts[i]]
        if route is not None and len(route):
            between = route[
                (route["timestamp"] > timestamps[i]) & (route["timestamp"] < timestamps[i + 1])
            ]
            if len(between):
                bx, by = transformer.transform(
                    between["lon"].to_numpy(), between["lat"].to_numpy()
                )
                chain.extend(np.column_stack([bx, by]))
        chain.append(pts[i + 1])

        # Порог применяется к длине САМОГО ПУТИ, а не к прямой между детекциями: иначе
        # петля оператора между двумя близкими замерами утащила бы коридор далеко в сторону.
        # Так гарантируется, что закрашенная ячейка отстоит от реального замера не дальше
        # hypot(bridge_max_m / 2, max_distance_m).
        path_len = float(np.hypot(*np.diff(np.asarray(chain), axis=0).T).sum())
        if path_len > params.bridge_max_m:
            segments.append([pts[i + 1]])  # разрыв: коридор обрывается
            continue

        segments[-1].extend(chain[1:])

    spacing = max(params.grid_step_m / 2.0, 0.5)
    densified = np.vstack([_densify_polyline(np.asarray(seg), spacing) for seg in segments])

    corridor_lon, corridor_lat = transformer.transform(
        densified[:, 0], densified[:, 1], direction="INVERSE"
    )
    return np.column_stack([corridor_lat, corridor_lon])


def cluster_points(lat: np.ndarray, lon: np.ndarray, *, eps_m: float) -> np.ndarray:
    """Разбивает точки на пространственные кластеры (single-linkage с порогом eps_m).

    Нужно для сетей, замеры которых разнесены на большие расстояния (мобильные точки
    доступа, "переезжающие" вместе с оператором между заходами) — единая сетка на весь
    bounding box такой сети была бы неподъёмной, поэтому каждый кластер интерполируется
    отдельно.
    """

    n = len(lat)
    if n <= 1:
        return np.zeros(n, dtype=int)

    x, y, _ = _project_local(lat, lon)
    coords = np.column_stack([x, y])
    tree = cKDTree(coords)
    pairs = tree.query_pairs(r=eps_m, output_type="ndarray")

    if len(pairs):
        graph = csr_matrix(
            (np.ones(len(pairs), dtype=bool), (pairs[:, 0], pairs[:, 1])),
            shape=(n, n),
        )
    else:
        graph = csr_matrix((n, n), dtype=bool)

    _, labels = connected_components(graph, directed=False)
    return labels


def _build_grid_axes(
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    *,
    lat_center: float,
    padding_fraction: float,
    grid_step_m: float,
    max_distance_m: float,
    max_cells: int,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Строит оси сетки в градусах с отступом; шаг автоматически огрубляется при --max-cells."""

    lat_extent_m = (lat_max - lat_min) * METERS_PER_DEGREE_LAT
    lon_extent_m = (lon_max - lon_min) * METERS_PER_DEGREE_LAT * np.cos(np.radians(lat_center))

    pad_m = min(padding_fraction * max(lat_extent_m, lon_extent_m), max_distance_m)
    if pad_m <= 0:
        pad_m = max_distance_m  # единственная точка / вырожденный экстент

    pad_lat = pad_m / METERS_PER_DEGREE_LAT
    pad_lon = pad_m / (METERS_PER_DEGREE_LAT * np.cos(np.radians(lat_center)))

    lat_lo, lat_hi = lat_min - pad_lat, lat_max + pad_lat
    lon_lo, lon_hi = lon_min - pad_lon, lon_max + pad_lon

    def axes_for_step(step_m: float) -> tuple[np.ndarray, np.ndarray]:
        step_lat = step_m / METERS_PER_DEGREE_LAT
        step_lon = step_m / (METERS_PER_DEGREE_LAT * np.cos(np.radians(lat_center)))
        rows = max(int(np.ceil((lat_hi - lat_lo) / step_lat)), 1) + 1
        cols = max(int(np.ceil((lon_hi - lon_lo) / step_lon)), 1) + 1
        return (
            lat_lo + np.arange(rows) * step_lat,
            lon_lo + np.arange(cols) * step_lon,
        )

    lat_axis, lon_axis = axes_for_step(grid_step_m)
    n_cells = len(lat_axis) * len(lon_axis)

    if n_cells > max_cells:
        scale = np.sqrt(n_cells / max_cells)
        new_step = grid_step_m * scale
        logger.warning(
            "Сетка %dx%d (%d ячеек) превышает --max-cells=%d, шаг увеличен с %.2f до %.2f м",
            len(lat_axis), len(lon_axis), n_cells, max_cells, grid_step_m, new_step,
        )
        grid_step_m = new_step
        lat_axis, lon_axis = axes_for_step(grid_step_m)
    elif max(len(lat_axis), len(lon_axis)) > 500:
        logger.warning(
            "Сетка %dx%d — сторона больше 500 ячеек, расчёт и итоговый файл могут заметно вырасти",
            len(lat_axis), len(lon_axis),
        )

    return lat_axis, lon_axis, grid_step_m


def idw_grid(
    lat: np.ndarray,
    lon: np.ndarray,
    values: np.ndarray,
    *,
    params: InterpolationParams,
    mask_points: Optional[np.ndarray] = None,
) -> GridResult:
    """Строит сетку методом IDW (Inverse Distance Weighting) для одного кластера точек.

    `mask_points` — массив (N, 2) в координатах (lat, lon), задающий геометрию, от которой
    отсчитывается маска «есть данные» (обычно коридор вдоль трека). Значения интерполируются
    только по фактическим замерам; маска влияет лишь на то, какие ячейки будут закрашены.
    Если не передан, маска считается от самих точек замеров — прежнее поведение по FR-4.
    """

    n = len(lat)
    lat_min, lat_max = float(np.min(lat)), float(np.max(lat))
    lon_min, lon_max = float(np.min(lon)), float(np.max(lon))
    lat_center = float(np.median(lat))

    lat_axis, lon_axis, actual_step = _build_grid_axes(
        lat_min, lat_max, lon_min, lon_max,
        lat_center=lat_center,
        padding_fraction=params.padding_fraction,
        grid_step_m=params.grid_step_m,
        max_distance_m=params.max_distance_m,
        max_cells=params.max_cells,
    )
    rows, cols = len(lat_axis), len(lon_axis)

    grid_lat, grid_lon = np.meshgrid(lat_axis, lon_axis, indexing="ij")
    px, py, transformer = _project_local(lat, lon)
    gx, gy = transformer.transform(grid_lon.ravel(), grid_lat.ravel())
    grid_pts = np.column_stack([gx, gy])

    tree = cKDTree(np.column_stack([px, py]))

    # Маска "нет данных" (независимо от --idw-radius): расстояние до коридора вдоль трека,
    # либо до самих точек замеров, если коридор не передан.
    if mask_points is not None and len(mask_points):
        mx, my = transformer.transform(mask_points[:, 1], mask_points[:, 0])
        mask_tree = cKDTree(np.column_stack([mx, my]))
    else:
        mask_tree = tree

    nearest_dist, _ = mask_tree.query(grid_pts, k=1)
    has_data = nearest_dist <= params.max_distance_m

    # До --idw-neighbors точек в пределах --idw-radius (или без ограничения радиуса) — для среднего.
    k = min(params.neighbors, n)
    upper_bound = params.idw_radius_m if params.idw_radius_m is not None else np.inf
    dist, idx = tree.query(grid_pts, k=k, distance_upper_bound=upper_bound)
    if k == 1:
        dist = dist.reshape(-1, 1)
        idx = idx.reshape(-1, 1)

    valid = np.isfinite(dist)
    safe_idx = np.where(valid, idx, 0)
    neighbor_values = values[safe_idx]

    safe_dist = np.maximum(dist, 1e-9)
    weights = np.where(valid, 1.0 / np.power(safe_dist, params.idw_power), 0.0)
    weight_sum = weights.sum(axis=1)
    has_weight = weight_sum > 0

    with np.errstate(invalid="ignore", divide="ignore"):
        interpolated = (weights * neighbor_values).sum(axis=1) / weight_sum

    grid_values = np.where(has_data & has_weight, interpolated, np.nan).reshape(rows, cols)

    return GridResult(
        values=grid_values,
        bounds=(float(lat_axis[0]), float(lon_axis[0]), float(lat_axis[-1]), float(lon_axis[-1])),
        n_points=n,
        grid_step_m=actual_step,
    )


def interpolate_network(
    df_net: pd.DataFrame,
    params: InterpolationParams,
    route: Optional[pd.DataFrame] = None,
) -> list[GridResult]:
    """Кластеризует точки одной сети и строит по одной IDW-сетке на каждый кластер.

    `route` — общий трек оператора (колонки timestamp/lat/lon по всем сетям): используется,
    чтобы мосты коридора шли по фактически пройденному пути, а не по прямой.
    """

    lat = df_net["lat"].to_numpy()
    lon = df_net["lon"].to_numpy()
    rssi = df_net["rssi"].to_numpy(dtype=float)

    labels = cluster_points(lat, lon, eps_m=params.cluster_eps_m)

    grids: list[GridResult] = []
    n_clusters = labels.max() + 1 if len(labels) else 0
    n_dropped_clusters = 0
    n_dropped_points = 0

    for label in range(n_clusters):
        mask = labels == label
        count = int(mask.sum())
        if count < params.min_cluster_points:
            n_dropped_clusters += 1
            n_dropped_points += count
            continue

        corridor = None
        if params.mask_mode == "track" and "timestamp" in df_net.columns:
            corridor = _build_track_corridor(df_net.loc[mask], params, route)

        grids.append(
            idw_grid(lat[mask], lon[mask], rssi[mask], params=params, mask_points=corridor)
        )

    if n_clusters > 1:
        logger.debug(
            "Сеть разбита на %d кластер(ов), построено сеток: %d", n_clusters, len(grids)
        )
    if n_dropped_clusters:
        logger.debug(
            "Отброшено %d микрокластер(ов) (%d точек) с числом замеров меньше --min-cluster-points=%d",
            n_dropped_clusters, n_dropped_points, params.min_cluster_points,
        )

    return grids
