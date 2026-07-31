"""Загрузка и валидация измерений вардрайвинга (CSV или выборка из БД)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("lat", "lon", "rssi", "bssid", "ssid", "timestamp")
MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

# Порядок фиксирован: строка засчитывается по первой сработавшей причине,
# чтобы сумма в отчёте совпадала с числом отброшенных строк.
DROP_REASONS = (
    "missing_coords",
    "bad_coords",
    "bad_rssi",
    "bad_bssid",
    "bad_timestamp",
)


class LoaderError(Exception):
    """Ошибка входных данных, которую нужно показать пользователю без трейсбека."""


@dataclass
class ValidationReport:
    rows_read: int
    rows_valid: int
    dropped: dict = field(default_factory=dict)
    duplicates_removed: int = 0
    unique_bssids: int = 0

    def format(self) -> str:
        lines = [f"Прочитано строк: {self.rows_read}"]
        total_dropped = sum(self.dropped.values())
        if total_dropped:
            reasons = ", ".join(
                f"{reason}={count}" for reason, count in self.dropped.items() if count
            )
            lines.append(f"Отброшено строк: {total_dropped} ({reasons})")
        else:
            lines.append("Отброшено строк: 0")
        lines.append(f"Удалено точных дублей: {self.duplicates_removed}")
        lines.append(f"Валидных строк: {self.rows_valid}")
        lines.append(f"Найдено уникальных BSSID: {self.unique_bssids}")
        return "\n".join(lines)


def _as_numeric(col: pd.Series) -> pd.Series:
    """Приводит колонку к числам независимо от исходного типа.

    Источников два: CSV (всё строки) и SQLite (lat/lon уже float, rssi — int),
    поэтому ``.str``-аксессор применяем только к нечисловым колонкам.
    """
    if pd.api.types.is_numeric_dtype(col):
        return pd.to_numeric(col, errors="coerce")
    return pd.to_numeric(col.astype(str).str.strip(), errors="coerce")


def _as_timestamp(col: pd.Series) -> pd.Series:
    """Приводит колонку ко времени (UTC) независимо от исходного типа."""
    if pd.api.types.is_datetime64_any_dtype(col):
        ts = pd.to_datetime(col, errors="coerce", utc=True)
    else:
        ts = pd.to_datetime(
            col.astype(str).str.strip(), format="ISO8601", utc=True, errors="coerce"
        )
    return ts


def _as_text(col: pd.Series) -> pd.Series:
    """Приводит колонку к строкам; NULL из SQLite становится пустой строкой, а не 'None'."""
    return col.where(col.notna(), "").astype(str)


def validate_measurements(
    raw: pd.DataFrame,
    *,
    rssi_bounds: tuple[int, int] = (-100, 0),
    source_name: str = "входные данные",
) -> tuple[pd.DataFrame, ValidationReport]:
    """Валидирует и нормализует таблицу измерений, возвращает чистый DataFrame и отчёт.

    Вынесено из :func:`load_measurements`, чтобы ровно та же валидация применялась
    и к CSV, и к выборке из проектной SQLite-базы (профиль экспорта ``heatmap``) —
    без промежуточного файла на диске.

    Принимает колонки в любом типе: строки (CSV) или уже готовые float/int/datetime
    (SQLite). Ожидает наличие всех :data:`REQUIRED_COLUMNS`.
    """

    missing_columns = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing_columns:
        raise LoaderError(
            f"В источнике ({source_name}) отсутствуют обязательные колонки: "
            + ", ".join(missing_columns)
        )

    rows_read = len(raw)
    if rows_read == 0:
        raise LoaderError(f"Источник ({source_name}) не содержит ни одной строки данных")

    df = raw[list(REQUIRED_COLUMNS)].copy()

    df["bssid"] = _as_text(df["bssid"]).str.strip().str.upper()
    df["ssid"] = _as_text(df["ssid"]).str.strip()

    lat = _as_numeric(df["lat"])
    lon = _as_numeric(df["lon"])
    rssi = _as_numeric(df["rssi"])
    timestamp = _as_timestamp(df["timestamp"])

    df["lat"], df["lon"], df["rssi"], df["timestamp"] = lat, lon, rssi, timestamp

    remaining = np.ones(len(df), dtype=bool)
    dropped: dict[str, int] = {}

    missing_mask = remaining & (lat.isna() | lon.isna() | ((lat == 0) & (lon == 0)))
    dropped["missing_coords"] = int(missing_mask.sum())
    remaining &= ~missing_mask.to_numpy()

    bad_coords_mask = remaining & ~(lat.between(-90, 90) & lon.between(-180, 180)).to_numpy()
    dropped["bad_coords"] = int(bad_coords_mask.sum())
    remaining &= ~bad_coords_mask

    lo, hi = rssi_bounds
    bad_rssi_mask = remaining & (rssi.isna() | ~rssi.between(lo, hi)).to_numpy()
    dropped["bad_rssi"] = int(bad_rssi_mask.sum())
    remaining &= ~bad_rssi_mask

    bssid_valid = df["bssid"].str.fullmatch(MAC_RE).fillna(False).to_numpy()
    bad_bssid_mask = remaining & ~bssid_valid
    dropped["bad_bssid"] = int(bad_bssid_mask.sum())
    remaining &= ~bad_bssid_mask

    bad_ts_mask = remaining & timestamp.isna().to_numpy()
    dropped["bad_timestamp"] = int(bad_ts_mask.sum())
    remaining &= ~bad_ts_mask

    clean = df.loc[remaining].copy()
    clean["rssi"] = clean["rssi"].astype(int)

    before_dedup = len(clean)
    clean = clean.drop_duplicates(subset=["bssid", "timestamp", "lat", "lon"])
    duplicates_removed = before_dedup - len(clean)

    clean = clean.reset_index(drop=True)

    if len(clean) == 0:
        raise LoaderError(
            f"Ни одна строка не прошла валидацию ({source_name}) — проверьте формат данных"
        )

    report = ValidationReport(
        rows_read=rows_read,
        rows_valid=len(clean),
        dropped=dropped,
        duplicates_removed=duplicates_removed,
        unique_bssids=int(clean["bssid"].nunique()),
    )

    return clean, report


def load_measurements(
    path: str | Path, *, rssi_bounds: tuple[int, int] = (-100, 0)
) -> tuple[pd.DataFrame, ValidationReport]:
    """Читает CSV, валидирует и нормализует строки, возвращает чистый DataFrame и отчёт."""

    path = Path(path)
    if not path.is_file():
        raise LoaderError(f"Файл не найден: {path}")

    try:
        # dtype=str + keep_default_na=False: разбор чисел/дат делаем сами,
        # чтобы битые значения превращались в NaN точечно, а не роняли парсер CSV.
        raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception as exc:
        raise LoaderError(f"Не удалось прочитать CSV: {exc}") from exc

    return validate_measurements(raw, rssi_bounds=rssi_bounds, source_name=str(path))
