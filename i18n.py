from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys
from typing import Any


MUI_LANGUAGE_NAME = 0x00000008

TEXTS: dict[str, dict[str, str]] = {
    "ko": {
        "app_title": "Codex 사용량",
        "auto_refresh": "●  {minutes}분 자동 갱신",
        "settings_window_title": "Codex Usage 설정",
        "settings_title": "설정",
        "language": "언어",
        "language_auto": "자동",
        "language_korean": "한국어",
        "language_english": "English",
        "refresh_interval": "자동 갱신 주기",
        "tray_icon_basis": "트레이 아이콘 표시 기준",
        "low_balance_alert": "잔량 부족 알림",
        "startup": "Windows 시작 시 자동 실행",
        "minutes_option": "{minutes}분",
        "icon_week": "주간",
        "icon_five_hour": "5시간",
        "off": "꺼짐",
        "on": "켜짐",
        "threshold_option": "{threshold}% 이하",
        "plan_unknown": "요금제 정보 없음",
        "loading": "사용량을 조회하는 중...",
        "no_limits": "사용량 한도 정보가 없습니다.",
        "unexpected_error": "예상하지 못한 오류: {error}",
        "next_refresh": "다음 갱신",
        "last_refresh": "마지막 갱신",
        "status": "상태",
        "status_waiting": "대기",
        "status_refreshing": "갱신 중",
        "status_normal": "정상",
        "status_failed": "최근 갱신 실패",
        "graph_title": "최근 사용 추이",
        "graph_period": "최근 {days}일 · {label}",
        "history_empty": "기록이 쌓이면 그래프가 표시됩니다.",
        "settings_summary": "설정 요약",
        "icon_basis_short": "아이콘 기준",
        "alert_short": "알림",
        "startup_short": "자동 실행",
        "alert_notice_off": "ⓘ  잔량 부족 알림이 꺼져 있습니다.",
        "alert_notice_on": (
            "ⓘ  알림: 한도가 {threshold}% 이하로 "
            "내려가면 알림이 표시됩니다."
        ),
        "notification_title": "Codex 사용량 알림",
        "remaining": "{remaining:g}% 남음",
        "reset_unknown": "초기화 시각 정보 없음",
        "reset_days_hours": "{days}일 {hours}시간 후 초기화",
        "reset_hours_minutes": "{hours}시간 {minutes}분 후 초기화",
        "reset_minutes": "{minutes}분 후 초기화",
        "reset_soon": "곧 초기화",
        "window_week": "주간 한도",
        "window_five_hour": "5시간 한도",
    },
    "en": {
        "app_title": "Codex Usage",
        "auto_refresh": "●  Auto refresh every {minutes} min",
        "settings_window_title": "Codex Usage Settings",
        "settings_title": "Settings",
        "language": "Language",
        "language_auto": "Automatic",
        "language_korean": "Korean",
        "language_english": "English",
        "refresh_interval": "Auto refresh interval",
        "tray_icon_basis": "Tray icon display",
        "low_balance_alert": "Low balance notification",
        "startup": "Run when Windows starts",
        "minutes_option": "{minutes} min",
        "icon_week": "Weekly",
        "icon_five_hour": "5 hours",
        "off": "Off",
        "on": "On",
        "threshold_option": "{threshold}% or less",
        "plan_unknown": "Plan information unavailable",
        "loading": "Loading usage information...",
        "no_limits": "No usage limit information is available.",
        "unexpected_error": "Unexpected error: {error}",
        "next_refresh": "Next refresh",
        "last_refresh": "Last refresh",
        "status": "Status",
        "status_waiting": "Waiting",
        "status_refreshing": "Refreshing",
        "status_normal": "Normal",
        "status_failed": "Last refresh failed",
        "graph_title": "Recent usage",
        "graph_period": "Last {days} days · {label}",
        "history_empty": (
            "The graph will appear after usage "
            "history is collected."
        ),
        "settings_summary": "Settings summary",
        "icon_basis_short": "Tray display",
        "alert_short": "Notification",
        "startup_short": "Run at startup",
        "alert_notice_off": "ⓘ  Low balance notifications are disabled.",
        "alert_notice_on": (
            "ⓘ  Notification: You will be notified when "
            "remaining usage drops to {threshold}% or less."
        ),
        "notification_title": "Codex Usage Notification",
        "remaining": "{remaining:g}% remaining",
        "reset_unknown": "Reset time unavailable",
        "reset_days_hours": "Resets in {days}d {hours}h",
        "reset_hours_minutes": "Resets in {hours}h {minutes}m",
        "reset_minutes": "Resets in {minutes}m",
        "reset_soon": "Resets soon",
        "window_week": "Weekly limit",
        "window_five_hour": "5-hour limit",
    },
}


def detect_language() -> str:
    """Windows 표시 언어를 감지해 ko 또는 en을 반환한다."""
    forced_language = os.getenv(
        "CODEX_USAGE_TRAY_LANGUAGE",
        "",
    ).strip().lower()

    if forced_language in {"ko", "en"}:
        return forced_language

    if sys.platform != "win32":
        return "en"

    try:
        kernel32 = ctypes.WinDLL(
            "kernel32",
            use_last_error=True,
        )

        get_languages = (
            kernel32.GetUserPreferredUILanguages
        )
        get_languages.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.ULONG),
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.ULONG),
        ]
        get_languages.restype = wintypes.BOOL

        language_count = wintypes.ULONG(0)
        buffer_length = wintypes.ULONG(0)

        get_languages(
            MUI_LANGUAGE_NAME,
            ctypes.byref(language_count),
            None,
            ctypes.byref(buffer_length),
        )

        if buffer_length.value <= 1:
            return "en"

        buffer = ctypes.create_unicode_buffer(
            buffer_length.value
        )

        if not get_languages(
            MUI_LANGUAGE_NAME,
            ctypes.byref(language_count),
            buffer,
            ctypes.byref(buffer_length),
        ):
            return "en"

        language_names = [
            language_name
            for language_name in buffer[
                :buffer_length.value
            ].split("\0")
            if language_name
        ]

        if (
            language_names
            and language_names[0]
            .lower()
            .startswith("ko")
        ):
            return "ko"

    except (AttributeError, OSError):
        pass

    return "en"


def translate(
    language: str,
    key: str,
    **values: Any,
) -> str:
    """현재 언어에 맞는 번역 문자열을 반환한다."""
    language_table = TEXTS.get(
        language,
        TEXTS["en"],
    )

    template = language_table.get(
        key,
        TEXTS["en"].get(key, key),
    )

    try:
        return template.format(**values)
    except (KeyError, ValueError):
        return template
