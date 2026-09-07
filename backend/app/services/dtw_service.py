from __future__ import annotations

from typing import Any

try:
    from fastdtw import fastdtw
except Exception:  # noqa: BLE001
    fastdtw = None


# Prototype golden standard for motion sequence; replace with clinical templates.
GOLDEN_STANDARD = [
    [0.10, 0.15, 0.20],
    [0.12, 0.17, 0.22],
    [0.14, 0.20, 0.24],
    [0.16, 0.22, 0.26],
    [0.18, 0.24, 0.28],
]


def _to_series(coordinates: Any) -> list[list[float]]:
    if not isinstance(coordinates, list):
        return []

    series: list[list[float]] = []
    for item in coordinates:
        if isinstance(item, dict):
            x = float(item.get("x", 0.0))
            y = float(item.get("y", 0.0))
            z = float(item.get("z", 0.0))
            series.append([x, y, z])
        elif isinstance(item, list) and len(item) >= 3:
            series.append([float(item[0]), float(item[1]), float(item[2])])
    return series


def _euclidean(a: list[float], b: list[float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _fallback_distance(series: list[list[float]], target: list[list[float]]) -> float:
    if not series:
        return 999.0

    limit = min(len(series), len(target))
    if limit == 0:
        return 999.0

    total = 0.0
    for i in range(limit):
        total += _euclidean(series[i], target[i])
    return total / limit


def _pose_quality_score(coordinates: list[dict[str, Any]]) -> float:
    valid_points = [
        item
        for item in coordinates
        if isinstance(item, dict)
        and isinstance(item.get("x"), (int, float))
        and isinstance(item.get("y"), (int, float))
        and -0.25 <= float(item.get("x", 0.0)) <= 1.25
        and -0.25 <= float(item.get("y", 0.0)) <= 1.25
    ]
    if len(valid_points) < 8:
        return 0.0

    visibility_values = [max(0.0, min(1.0, float(item.get("visibility", 1.0)))) for item in valid_points]
    average_visibility = sum(visibility_values) / max(len(visibility_values), 1)
    visible_points = [item for item in valid_points if float(item.get("visibility", 1.0)) >= 0.35]
    points = visible_points or valid_points
    xs = [float(item["x"]) for item in points]
    ys = [float(item["y"]) for item in points]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)

    coverage = min(1.0, max(width, 0.0) / 0.35) * min(1.0, max(height, 0.0) / 0.65)
    in_frame_ratio = sum(1 for item in points if 0.0 <= float(item["x"]) <= 1.0 and 0.0 <= float(item["y"]) <= 1.0) / max(len(points), 1)

    score = 100.0 * ((0.60 * average_visibility) + (0.25 * coverage) + (0.15 * in_frame_ratio))
    return round(max(0.0, min(100.0, score)), 2)


def score_motion(coordinates: Any) -> float:
    """Return a 0-100 live pose score from incoming skeleton coordinates.

    Full MediaPipe pose frames are scored as detection/coverage quality so the
    live session has an immediate useful signal. Short synthetic sequences keep
    the original DTW placeholder path for legacy contract tests.
    """
    if isinstance(coordinates, list) and len(coordinates) >= 20 and all(isinstance(item, dict) for item in coordinates):
        return _pose_quality_score(coordinates)

    series = _to_series(coordinates)

    if fastdtw and series:
        try:
            distance, _ = fastdtw(series, GOLDEN_STANDARD, dist=_euclidean)
        except Exception:  # noqa: BLE001
            distance = _fallback_distance(series, GOLDEN_STANDARD)
    else:
        distance = _fallback_distance(series, GOLDEN_STANDARD)

    # Prototype transform distance -> score.
    score = max(0.0, min(100.0, 100.0 - (distance * 100.0)))
    return round(score, 2)
