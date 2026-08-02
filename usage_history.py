from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from codex_client import UsageSnapshot


HISTORY_DIRECTORY = Path(
    os.getenv("APPDATA", str(Path.home()))
) / "CodexUsageTray"

HISTORY_PATH = (
    HISTORY_DIRECTORY
    / "usage_history.jsonl"
)

HISTORY_RETENTION_DAYS = 14
HISTORY_MAX_RECORDS = 25_000

_last_cleanup_date: date | None = None


@dataclass(frozen=True, slots=True)
class UsageHistoryPoint:
    """그래프에 표시할 한 시점의 잔여 사용량."""

    recorded_at: datetime
    remaining_percent: float


def save_usage_snapshot(
    snapshot: UsageSnapshot,
) -> bool:
    """한 번 조회한 사용량을 JSONL 파일에 추가한다."""
    record = {
        "recorded_at": snapshot.fetched_at.isoformat(
            timespec="seconds"
        ),
        "plan_type": snapshot.plan_type,
        "limits": [
            {
                "duration_mins": window.duration_mins,
                "remaining_percent": round(
                    window.remaining_percent,
                    2,
                ),
                "resets_at": (
                    window.resets_at.isoformat(
                        timespec="seconds"
                    )
                    if window.resets_at is not None
                    else None
                ),
            }
            for window in snapshot.windows
        ],
    }

    try:
        HISTORY_DIRECTORY.mkdir(
            parents=True,
            exist_ok=True,
        )

        with HISTORY_PATH.open(
            "a",
            encoding="utf-8",
        ) as history_file:
            history_file.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                )
                + "\n"
            )

        _cleanup_history_if_needed()

        return True

    except OSError:
        # 기록 실패가 앱의 사용량 갱신을 막지는 않게 한다.
        return False

def _cleanup_history_if_needed() -> None:
    """하루에 한 번만 오래된 기록을 정리한다."""
    global _last_cleanup_date

    today = datetime.now().date()

    if _last_cleanup_date == today:
        return

    if cleanup_usage_history():
        _last_cleanup_date = today


def cleanup_usage_history(
    *,
    retention_days: int = HISTORY_RETENTION_DAYS,
    max_records: int = HISTORY_MAX_RECORDS,
) -> bool:
    """오래된 기록과 최대 개수를 넘는 기록을 제거한다."""
    cutoff = datetime.now() - timedelta(
        days=max(1, retention_days)
    )

    max_records = max(
        1,
        max_records,
    )

    try:
        lines = HISTORY_PATH.read_text(
            encoding="utf-8"
        ).splitlines()

    except FileNotFoundError:
        return True

    except OSError:
        return False

    valid_records: list[
        tuple[datetime, str]
    ] = []

    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(record, dict):
            continue

        recorded_at_value = record.get(
            "recorded_at"
        )

        if not isinstance(
            recorded_at_value,
            str,
        ):
            continue

        try:
            recorded_at = datetime.fromisoformat(
                recorded_at_value
            )
        except ValueError:
            continue

        if recorded_at < cutoff:
            continue

        valid_records.append(
            (
                recorded_at,
                line,
            )
        )

    valid_records.sort(
        key=lambda item: item[0]
    )

    if len(valid_records) > max_records:
        valid_records = valid_records[
            -max_records:
        ]

    cleaned_lines = [
        line
        for _, line in valid_records
    ]

    # 정리할 내용이 없다면 파일을 다시 쓰지 않는다.
    if cleaned_lines == lines:
        return True

    if cleaned_lines:
        cleaned_text = (
            "\n".join(cleaned_lines)
            + "\n"
        )
    else:
        cleaned_text = ""

    temporary_path = HISTORY_PATH.with_name(
        f"{HISTORY_PATH.name}.tmp"
    )

    try:
        temporary_path.write_text(
            cleaned_text,
            encoding="utf-8",
        )

        temporary_path.replace(
            HISTORY_PATH
        )

        return True

    except OSError:
        try:
            temporary_path.unlink(
                missing_ok=True
            )
        except OSError:
            pass

        return False


def load_usage_history(
    duration_mins: int,
    *,
    days: int = 7,
    max_points: int = 48,
) -> tuple[UsageHistoryPoint, ...]:
    """지정한 한도의 최근 기록을 그래프용으로 읽는다."""
    if max_points <= 0:
        return ()

    cutoff = datetime.now() - timedelta(
        days=max(1, days)
    )

    try:
        lines = HISTORY_PATH.read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return ()

    points: list[UsageHistoryPoint] = []

    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(record, dict):
            continue

        recorded_at_value = record.get(
            "recorded_at"
        )
        limits = record.get("limits")

        if (
            not isinstance(recorded_at_value, str)
            or not isinstance(limits, list)
        ):
            continue

        try:
            recorded_at = datetime.fromisoformat(
                recorded_at_value
            )
        except ValueError:
            continue

        if recorded_at < cutoff:
            continue

        for limit in limits:
            if not isinstance(limit, dict):
                continue

            if (
                limit.get("duration_mins")
                != duration_mins
            ):
                continue

            try:
                remaining_percent = float(
                    limit.get(
                        "remaining_percent"
                    )
                )
            except (TypeError, ValueError):
                continue

            points.append(
                UsageHistoryPoint(
                    recorded_at=recorded_at,
                    remaining_percent=min(
                        100.0,
                        max(
                            0.0,
                            remaining_percent,
                        ),
                    ),
                )
            )
            break

    points.sort(
        key=lambda point: point.recorded_at
    )

    if len(points) <= max_points:
        return tuple(points)

    if max_points == 1:
        return (points[-1],)

    last_index = len(points) - 1

    selected_indexes = {
        round(
            index
            * last_index
            / (max_points - 1)
        )
        for index in range(max_points)
    }

    return tuple(
        points[index]
        for index in sorted(selected_indexes)
    )