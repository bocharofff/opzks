"""Группировка измерений по BSSID, правило именования и отбор сетей для слоёв."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import pandas as pd


@dataclass(frozen=True)
class NetworkSummary:
    bssid: str
    ssid: str
    display_name: str
    n_points: int
    rssi_min: int
    rssi_max: int
    rssi_mean: float
    bbox: tuple[float, float, float, float]  # lat_min, lon_min, lat_max, lon_max
    first_seen: pd.Timestamp
    last_seen: pd.Timestamp


@dataclass
class SelectionResult:
    networks: list[NetworkSummary]
    warnings: list[str]


def _is_hidden(ssid: str, bssid: str) -> bool:
    return ssid == "" or ssid.upper() == bssid


def build_network_index(df: pd.DataFrame) -> dict[str, NetworkSummary]:
    """Строит сводку по каждому BSSID и присваивает отображаемые имена слоёв.

    Имена сетей не уникальны: одинаковые SSID различаются суффиксами «(1)», «(2)»,
    скрытые сети (пустой SSID) показываются своим MAC.
    """

    grouped = df.groupby("bssid", sort=False)
    stats = grouped.agg(
        ssid=("ssid", "first"),
        n_points=("rssi", "size"),
        rssi_min=("rssi", "min"),
        rssi_max=("rssi", "max"),
        rssi_mean=("rssi", "mean"),
        lat_min=("lat", "min"),
        lat_max=("lat", "max"),
        lon_min=("lon", "min"),
        lon_max=("lon", "max"),
        first_seen=("timestamp", "min"),
        last_seen=("timestamp", "max"),
    )

    # Детерминированный порядок: время первого появления, при совпадении — сам BSSID.
    order = (
        stats.reset_index()[["bssid", "first_seen"]]
        .sort_values(["first_seen", "bssid"], kind="stable")["bssid"]
        .tolist()
    )

    seen_counts: dict[str, int] = {}
    summaries: dict[str, NetworkSummary] = {}
    for bssid in order:
        row = stats.loc[bssid]
        ssid = row["ssid"]
        if _is_hidden(ssid, bssid):
            display_name = bssid
        else:
            count = seen_counts.get(ssid, 0)
            display_name = ssid if count == 0 else f"{ssid} ({count})"
            seen_counts[ssid] = count + 1

        summaries[bssid] = NetworkSummary(
            bssid=bssid,
            ssid=ssid,
            display_name=display_name,
            n_points=int(row["n_points"]),
            rssi_min=int(row["rssi_min"]),
            rssi_max=int(row["rssi_max"]),
            rssi_mean=float(row["rssi_mean"]),
            bbox=(
                float(row["lat_min"]),
                float(row["lon_min"]),
                float(row["lat_max"]),
                float(row["lon_max"]),
            ),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
        )

    return summaries


def select_networks(
    index: dict[str, NetworkSummary],
    *,
    bssids: Optional[Iterable[str]] = None,
    top: int = 10,
    min_points: int = 20,
) -> SelectionResult:
    """Отбирает сети для построения слоёв: явный список --bssid либо --top по числу точек."""

    warnings: list[str] = []

    if bssids:
        requested = [b.strip().upper() for b in bssids if b.strip()]
        selected = []
        for bssid in requested:
            summary = index.get(bssid)
            if summary is None:
                warnings.append(
                    f"BSSID {bssid} не найден во входных данных. "
                    "Используйте --list-networks, чтобы посмотреть доступные сети."
                )
                continue
            if summary.n_points < min_points:
                warnings.append(
                    f"Сеть {summary.display_name} ({bssid}): {summary.n_points} замеров "
                    f"меньше --min-points={min_points}, но построена явно по --bssid."
                )
            selected.append(summary)
        return SelectionResult(networks=selected, warnings=warnings)

    eligible = [s for s in index.values() if s.n_points >= min_points]
    skipped = len(index) - len(eligible)
    if skipped:
        warnings.append(
            f"Пропущено {skipped} сетей с числом замеров меньше --min-points={min_points}"
        )

    eligible.sort(key=lambda s: (-s.n_points, s.bssid))

    if len(eligible) > top:
        warnings.append(
            f"Найдено {len(eligible)} сетей, прошедших --min-points; "
            f"показаны {top} с наибольшим числом замеров (--top)."
        )

    return SelectionResult(networks=eligible[:top], warnings=warnings)


def format_network_table(summaries: Iterable[NetworkSummary]) -> str:
    summaries = sorted(summaries, key=lambda s: (-s.n_points, s.bssid))
    if not summaries:
        return "Сети не найдены."

    header = f"{'Имя':<30}  {'BSSID':<17}  {'Точек':>6}  {'RSSI min/avg/max':>18}  Период наблюдения"
    lines = [header, "-" * len(header)]
    for s in summaries:
        name = s.display_name if len(s.display_name) <= 30 else s.display_name[:27] + "..."
        rssi_range = f"{s.rssi_min:>4}/{s.rssi_mean:>5.1f}/{s.rssi_max:<4}"
        period = f"{s.first_seen.isoformat()} .. {s.last_seen.isoformat()}"
        lines.append(f"{name:<30}  {s.bssid:<17}  {s.n_points:>6}  {rssi_range:>18}  {period}")
    return "\n".join(lines)
