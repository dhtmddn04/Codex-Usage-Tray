import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import sys
import tkinter as tk
import threading
import time
import winreg
from datetime import datetime
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont

from native_tray import NativeTrayIcon
from usage_history import (UsageHistoryPoint, load_usage_history, save_usage_snapshot,)

from codex_client import (
    CodexClientError,
    UsageSnapshot,
    UsageWindow,
    get_usage,
)


POPUP_WIDTH = 340
INITIAL_POPUP_HEIGHT = 220
TASKBAR_GAP = 72

REFRESH_INTERVAL_OPTIONS = (1, 2, 5, 10)
DEFAULT_REFRESH_INTERVAL_MINUTES = 2

ICON_DISPLAY_OPTIONS = {
    "주간": 10_080,
    "5시간": 300,
}
DEFAULT_ICON_DISPLAY_MODE = "주간"

LOW_BALANCE_THRESHOLD_OPTIONS = (
    0,
    10,
    20,
    30,
)
DEFAULT_LOW_BALANCE_THRESHOLD = 20

SETTINGS_DIRECTORY = Path(
    os.getenv("APPDATA", str(Path.home()))
) / "CodexUsageTray"

SETTINGS_PATH = (
    SETTINGS_DIRECTORY
    / "settings.json"
)

STARTUP_REGISTRY_PATH = (
    r"Software\Microsoft\Windows\CurrentVersion\Run"
)
STARTUP_VALUE_NAME = "CodexUsageTray"

# 트레이 아이콘 hover 동작
HOVER_SHOW_DELAY_MS = 180
HOVER_HIDE_DELAY_MS = 100
HOVER_POLL_MS = 60
POPUP_ICON_GAP = 8

#TRAY_ICON_SIZE = 64
TRAY_ICON_SIZE = 32
TRAY_NEUTRAL = "#5C5F66"
TRAY_FONT_PATH = r"C:\Windows\Fonts\segoeuib.ttf"

BACKGROUND = "#202123"
CARD_BACKGROUND = "#2B2D31"
BORDER_COLOR = "#3A3C40"

TEXT_PRIMARY = "#F3F3F3"
TEXT_SECONDARY = "#A1A3A8"

GREEN = "#10A37F"
ORANGE = "#F0A43C"
RED = "#E25555"

HISTORY_GRAPH_DAYS = 7
HISTORY_GRAPH_MAX_POINTS = 48
HISTORY_GRAPH_HEIGHT = 130


# Tk 좌표와 Win32 트레이 좌표가 다른 DPI 환경에서도
# 같은 물리 픽셀 좌표계를 사용하기 위한 Win32 함수들
_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

_USER32.GetParent.argtypes = [wintypes.HWND]
_USER32.GetParent.restype = wintypes.HWND

_USER32.GetWindowRect.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.RECT),
]
_USER32.GetWindowRect.restype = wintypes.BOOL

_USER32.GetCursorPos.argtypes = [
    ctypes.POINTER(wintypes.POINT),
]
_USER32.GetCursorPos.restype = wintypes.BOOL

_USER32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
_USER32.SetWindowPos.restype = wintypes.BOOL
_KERNEL32.CreateMutexW.argtypes = [
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
_KERNEL32.CreateMutexW.restype = wintypes.HANDLE

_KERNEL32.CloseHandle.argtypes = [
    wintypes.HANDLE,
]
_KERNEL32.CloseHandle.restype = wintypes.BOOL

try:
    _USER32.GetDpiForWindow.argtypes = [wintypes.HWND]
    _USER32.GetDpiForWindow.restype = wintypes.UINT
except AttributeError:
    pass

SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

ERROR_ALREADY_EXISTS = 183

SINGLE_INSTANCE_MUTEX_NAME = (
    r"Local\CodexUsageTray.SingleInstance"
)

def _acquire_single_instance_mutex() -> int | None:
    """첫 번째 앱 인스턴스만 실행되도록 Mutex를 만든다."""
    ctypes.set_last_error(0)

    mutex_handle = _KERNEL32.CreateMutexW(
        None,
        False,
        SINGLE_INSTANCE_MUTEX_NAME,
    )

    if not mutex_handle:
        raise ctypes.WinError(
            ctypes.get_last_error()
        )

    if (
        ctypes.get_last_error()
        == ERROR_ALREADY_EXISTS
    ):
        _KERNEL32.CloseHandle(
            mutex_handle
        )
        return None

    return int(mutex_handle)


def _close_single_instance_mutex(
    mutex_handle: int,
) -> None:
    """앱 종료 시 Mutex 핸들을 닫는다."""
    _KERNEL32.CloseHandle(
        mutex_handle
    )

class UsageTrayApp:
    """Codex 사용량 트레이 애플리케이션."""

    def __init__(self) -> None:
        ctk.set_appearance_mode("dark")

        # CustomTkinter 이벤트 루프를 실행하기 위한 숨겨진 창
        self.root = ctk.CTk()
        self.root.withdraw()

        self.settings = self._load_settings()
        self.auto_refresh_minutes = (
            self._normalize_refresh_minutes(
                self.settings.get(
                    "auto_refresh_minutes"
                )
            )
        )

        self.icon_display_mode = (
            self._normalize_icon_display_mode(
                self.settings.get(
                    "icon_display_mode"
                )
            )
        )

        self.low_balance_threshold = (
            self._normalize_low_balance_threshold(
                self.settings.get(
                    "low_balance_threshold"
                )
            )
        )

        self.start_with_windows = (
            self._is_startup_enabled()
        )

        self.popup: ctk.CTkToplevel | None = None
        self.settings_window: ctk.CTkToplevel | None = None
        self.startup_switch: ctk.CTkSwitch | None = None
        self.content_frame: ctk.CTkFrame | None = None
        self.footer_label: ctk.CTkLabel | None = None
        self.auto_refresh_label: ctk.CTkLabel | None = None

        self.status_label: ctk.CTkLabel | None = None
        self.refresh_status_text = "대기"
        self.refresh_status_color = TEXT_SECONDARY

        self.popup_height = INITIAL_POPUP_HEIGHT
        self.loading = False
        self.auto_refresh_job: str | None = None
        self.next_refresh_at: float | None = None
        self.refresh_countdown_job: str | None = None
        self.latest_snapshot: UsageSnapshot | None = None

        self.low_balance_notified_keys: set[
            tuple[int | None, int | None]
        ] = set()

        self.popup_opened_by_hover = False
        self.hover_show_job: str | None = None
        self.hover_monitor_job: str | None = None
        self.hover_leave_started_at: float | None = None

        # 트레이 스레드가 GUI 스레드에 명령을 전달하는 통로
        self.command_queue: queue.Queue[
            tuple[str, Any | None]
        ] = queue.Queue()

        self.tray_icon = self._create_tray_icon()

    @staticmethod
    def _normalize_refresh_minutes(
        value: Any,
    ) -> int:
        """자동 갱신 주기 값을 허용된 숫자로 정리한다."""
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return DEFAULT_REFRESH_INTERVAL_MINUTES

        if minutes in REFRESH_INTERVAL_OPTIONS:
            return minutes

        return DEFAULT_REFRESH_INTERVAL_MINUTES

    @staticmethod
    def _normalize_icon_display_mode(
        value: Any,
    ) -> str:
        """트레이 아이콘 표시 기준을 올바른 값으로 정리한다."""
        if value in ICON_DISPLAY_OPTIONS:
            return str(value)

        return DEFAULT_ICON_DISPLAY_MODE

    @staticmethod
    def _normalize_low_balance_threshold(
        value: Any,
    ) -> int:
        """잔량 부족 알림 기준을 올바른 값으로 정리한다."""
        try:
            threshold = int(value)
        except (TypeError, ValueError):
            return DEFAULT_LOW_BALANCE_THRESHOLD

        if threshold in LOW_BALANCE_THRESHOLD_OPTIONS:
            return threshold

        return DEFAULT_LOW_BALANCE_THRESHOLD

    @staticmethod
    def _get_startup_command() -> str:
        """현재 실행 형태에 맞는 자동 실행 명령을 만든다."""
        if getattr(sys, "frozen", False):
            executable_path = Path(
                sys.executable
            ).resolve()

            return f'"{executable_path}"'

        python_path = Path(
            sys.executable
        ).resolve()

        pythonw_path = python_path.with_name(
            "pythonw.exe"
        )

        if pythonw_path.exists():
            python_path = pythonw_path

        script_path = Path(__file__).resolve()

        return (
            f'"{python_path}" '
            f'"{script_path}"'
        )

    @staticmethod
    def _is_startup_enabled() -> bool:
        """Windows 자동 실행 등록 여부를 확인한다."""
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                STARTUP_REGISTRY_PATH,
            ) as registry_key:
                value, _ = winreg.QueryValueEx(
                    registry_key,
                    STARTUP_VALUE_NAME,
                )

            return bool(value)

        except (FileNotFoundError, OSError):
            return False

    @staticmethod
    def _set_startup_enabled(
        enabled: bool,
    ) -> bool:
        """Windows 자동 실행 등록을 추가하거나 제거한다."""
        try:
            with winreg.CreateKey(
                winreg.HKEY_CURRENT_USER,
                STARTUP_REGISTRY_PATH,
            ) as registry_key:
                if enabled:
                    winreg.SetValueEx(
                        registry_key,
                        STARTUP_VALUE_NAME,
                        0,
                        winreg.REG_SZ,
                        UsageTrayApp._get_startup_command(),
                    )
                else:
                    try:
                        winreg.DeleteValue(
                            registry_key,
                            STARTUP_VALUE_NAME,
                        )
                    except FileNotFoundError:
                        pass

            return True

        except OSError:
            return False

    @staticmethod
    def _load_settings() -> dict[str, Any]:
        """저장된 앱 설정을 읽는다."""
        try:
            text = SETTINGS_PATH.read_text(
                encoding="utf-8"
            )
            data = json.loads(text)
        except (
            OSError,
            json.JSONDecodeError,
        ):
            return {}

        if not isinstance(data, dict):
            return {}

        return data

    def _save_settings(self) -> None:
        """현재 앱 설정을 파일에 저장한다."""
        self.settings[
            "auto_refresh_minutes"
        ] = self.auto_refresh_minutes

        self.settings[
            "icon_display_mode"
        ] = self.icon_display_mode

        self.settings[
            "low_balance_threshold"
        ] = self.low_balance_threshold

        self.settings[
            "start_with_windows"
        ] = self.start_with_windows

        try:
            SETTINGS_DIRECTORY.mkdir(
                parents=True,
                exist_ok=True,
            )

            temporary_path = (
                SETTINGS_PATH.with_suffix(".tmp")
            )

            temporary_path.write_text(
                json.dumps(
                    self.settings,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            temporary_path.replace(
                SETTINGS_PATH
            )

        except OSError:
            # 저장 실패가 앱 실행 자체를 막지는 않게 한다.
            pass

    def _create_tray_image(
        self,
        remaining_percent: float | None = None,
    ) -> Image.Image:
        """남은 사용량 숫자가 들어간 트레이 아이콘을 만든다."""
        image = Image.new(
            "RGBA",
            (TRAY_ICON_SIZE, TRAY_ICON_SIZE),
            (0, 0, 0, 0),
        )
        draw = ImageDraw.Draw(image)

        if remaining_percent is None:
            text = "..."
            background = TRAY_NEUTRAL
        else:
            rounded_percent = round(
                min(100.0, max(0.0, remaining_percent))
            )
            text = str(rounded_percent)
            background = self._get_usage_color(
                remaining_percent
            )

        draw.rounded_rectangle(
            #(5, 5, 59, 59),
            (0, 0, 31, 31),
            #radius=14,
            radius=7,
            fill=self._hex_to_rgba(background),
        )

        # 글자 수에 따라 아이콘 안에서 최대한 크게 표시
        if text == "...":
            font_size = 15
        elif len(text) == 3:
            font_size = 15
        elif len(text) == 2:
            font_size = 17
        else:
            font_size = 18

        try:
            font = ImageFont.truetype(
                TRAY_FONT_PATH,
                font_size,
            )
        except OSError:
            font = ImageFont.load_default()

        # 사각형의 정확한 중심
        center_x = TRAY_ICON_SIZE / 2
        center_y = TRAY_ICON_SIZE / 2

        # 글꼴 모양에 따른 미세 보정
        if text == "...":
            # 글꼴의 마침표는 아래쪽에 붙으므로 점을 직접 가운데 그린다.
            dot_y = 16
            dot_radius = 1

            for dot_x in (11, 16, 21):
                draw.ellipse(
                    (
                        dot_x - dot_radius,
                        dot_y - dot_radius,
                        dot_x + dot_radius,
                        dot_y + dot_radius,
                    ),
                    fill=(255, 255, 255, 255),
                )
        else:
            if text.isdigit():
                center_x -= 0.5
                center_y -= 0.5

            draw.text(
                (center_x, center_y),
                text,
                font=font,
                fill=(255, 255, 255, 255),
                anchor="mm",
            )

        return image

    @staticmethod
    def _hex_to_rgba(
        color: str,
    ) -> tuple[int, int, int, int]:
        """#RRGGBB 문자열을 Pillow용 RGBA 값으로 바꾼다."""
        value = color.lstrip("#")
        return (
            int(value[0:2], 16),
            int(value[2:4], 16),
            int(value[4:6], 16),
            255,
        )

    def _select_icon_window(
        self,
        snapshot: UsageSnapshot,
    ) -> UsageWindow | None:
        """설정에서 선택한 한도를 트레이 아이콘에 표시한다."""
        windows = snapshot.windows

        preferred_duration = ICON_DISPLAY_OPTIONS[
            self.icon_display_mode
        ]

        for usage_window in windows:
            if (
                usage_window.duration_mins
                == preferred_duration
            ):
                return usage_window

        # 선택한 한도가 없으면 존재하는 한도를 대신 표시한다.
        for duration_mins in (10_080, 300):
            for usage_window in windows:
                if (
                    usage_window.duration_mins
                    == duration_mins
                ):
                    return usage_window

        if windows:
            return windows[0]

        return None

    @staticmethod
    def _sort_windows(
        windows: tuple[UsageWindow, ...],
    ) -> tuple[UsageWindow, ...]:
        """팝업과 툴팁에서 5시간, 주간 순서로 정렬한다."""
        priority = {
            300: 0,
            10_080: 1,
        }

        return tuple(
            sorted(
                windows,
                key=lambda usage_window: priority.get(
                    usage_window.duration_mins,
                    2,
                ),
            )
        )

    def _check_low_balance_alert(
        self,
        snapshot: UsageSnapshot,
    ) -> None:
        """잔량이 설정값 이하일 때 Windows 알림을 표시한다."""
        if self.low_balance_threshold <= 0:
            return

        current_keys: set[
            tuple[int | None, int | None]
        ] = set()

        pending_items: list[
            tuple[
                tuple[int | None, int | None],
                str,
            ]
        ] = []

        for usage_window in snapshot.windows:
            reset_timestamp = (
                int(usage_window.resets_at.timestamp())
                if usage_window.resets_at is not None
                else None
            )

            alert_key = (
                usage_window.duration_mins,
                reset_timestamp,
            )
            current_keys.add(alert_key)

            if (
                usage_window.remaining_percent
                > self.low_balance_threshold
            ):
                continue

            if (
                alert_key
                in self.low_balance_notified_keys
            ):
                continue

            message = (
                f"{usage_window.label}: "
                f"{usage_window.remaining_percent:g}% 남음"
            )

            pending_items.append(
                (alert_key, message)
            )

        self.low_balance_notified_keys.intersection_update(
            current_keys
        )

        if not pending_items:
            return

        notification_sent = (
            self.tray_icon.show_notification(
                "Codex 사용량 알림",
                "\n".join(
                    message
                    for _, message in pending_items
                ),
            )
        )

        if notification_sent:
            self.low_balance_notified_keys.update(
                alert_key
                for alert_key, _ in pending_items
            )

    def _update_tray_status(
        self,
        snapshot: UsageSnapshot,
    ) -> None:
        """조회 결과로 숫자 아이콘과 툴팁을 갱신한다."""
        icon_window = self._select_icon_window(
            snapshot
        )

        if icon_window is None:
            self._set_tray_error()
            return

        self.tray_icon.icon = self._create_tray_image(
            icon_window.remaining_percent
        )

    def _set_tray_error(self) -> None:
        """사용량을 읽지 못했을 때 중립 아이콘을 표시한다."""
        self.tray_icon.icon = self._create_tray_image()

    def _create_tray_icon(self) -> NativeTrayIcon:
        """Windows 네이티브 트레이 아이콘을 만든다."""
        return NativeTrayIcon(
            icon=self._create_tray_image(),
            on_activate=self._request_toggle,
            on_refresh=self._request_refresh,
            on_quit=self._request_quit,
            on_hover=self._request_hover,
        )

    def _request_toggle(self) -> None:
        """트레이 아이콘 클릭 요청을 GUI 스레드로 전달한다."""
        self.command_queue.put(("toggle", None))

    def _request_refresh(self) -> None:
        """수동 새로고침 요청을 GUI 스레드로 전달한다."""
        self.command_queue.put(("refresh", None))

    def _request_quit(self) -> None:
        """종료 요청을 GUI 스레드로 전달한다."""
        self.command_queue.put(("quit", None))

    def _request_hover(self) -> None:
        """트레이 아이콘 hover 요청을 GUI 스레드로 전달한다."""
        self.command_queue.put(("hover", None))

    def _process_commands(self) -> None:
        """트레이·조회 스레드의 명령을 GUI 스레드에서 처리한다."""
        while True:
            try:
                command, data = self.command_queue.get_nowait()
            except queue.Empty:
                break

            if command == "toggle":
                self._toggle_popup()

            elif command == "hover":
                self._handle_tray_hover()

            elif command == "refresh":
                self._refresh_usage(
                    show_loading=self._popup_is_visible()
                )

            elif (
                command == "usage_result"
                and isinstance(data, UsageSnapshot)
            ):
                save_usage_snapshot(data)
                self._display_usage(data)

            elif command == "usage_error":
                self._display_error(str(data))

            elif command == "quit":
                self._quit_app()
                return

        self.root.after(100, self._process_commands)

    def _popup_is_visible(self) -> bool:
        """팝업이 현재 화면에 표시되어 있는지 확인한다."""
        return bool(
            self.popup is not None
            and self.popup.winfo_exists()
            and self.popup.winfo_viewable()
        )

    def _toggle_popup(self) -> None:
        """클릭으로 팝업을 고정하거나 닫는다."""
        if self._popup_is_visible():
            if self.popup_opened_by_hover:
                # hover 팝업을 클릭하면 일반 팝업으로 고정한다.
                self.popup_opened_by_hover = False
                self._cancel_hover_jobs()

                if self.popup is not None:
                    self.popup.focus_force()
            else:
                self._hide_popup()
        else:
            self._show_popup(
                focus=True,
                refresh=False,
            )

    def _show_popup(
        self,
        *,
        focus: bool,
        refresh: bool,
    ) -> None:
        """기존 상세 팝업을 표시한다."""
        if self.popup is None or not self.popup.winfo_exists():
            self._build_popup()
        else:
            self.popup.deiconify()

        self._position_popup()
        self.popup.lift()

        if focus:
            self.popup_opened_by_hover = False
            self.popup.after(
                20,
                self.popup.focus_force,
            )

        if self.latest_snapshot is not None:
            self._display_usage(
                self.latest_snapshot,
                schedule_refresh=False,
            )

            if refresh:
                self._refresh_usage(
                    show_loading=False
                )
        else:
            self._refresh_usage(
                show_loading=True
            )

    def _build_popup(self) -> None:
        """제목 표시줄이 없는 팝오버 창을 만든다."""
        self.popup = ctk.CTkToplevel(self.root)

        self.popup.title("Codex Usage")
        self.popup.resizable(False, False)
        self.popup.configure(fg_color=BACKGROUND)

        # 일반 창의 제목 표시줄과 버튼 제거
        self.popup.overrideredirect(True)

        # 다른 창보다 위에 표시
        self.popup.attributes("-topmost", True)

        # 약간 부드러운 투명도
        self.popup.attributes("-alpha", 0.99)

        self.popup.protocol(
            "WM_DELETE_WINDOW",
            self._hide_popup,
        )

        self.popup.bind(
            "<FocusOut>",
            self._on_focus_out,
        )

        self.popup.bind(
            "<Escape>",
            lambda event: self._hide_popup(),
        )

        outer_frame = ctk.CTkFrame(
            self.popup,
            fg_color=BACKGROUND,
            corner_radius=18,
        )
        outer_frame.pack(
            fill="both",
            expand=True,
            padx=0,
            pady=0,
        )

        header = ctk.CTkFrame(
            outer_frame,
            fg_color="transparent",
        )
        header.pack(
            fill="x",
            padx=18,
            pady=(16, 9),
        )

        title_label = ctk.CTkLabel(
            header,
            text="Codex 사용량",
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=18,
                weight="bold",
            ),
        )
        title_label.pack(side="left")

        settings_button = ctk.CTkButton(
            header,
            text="⚙",
            width=30,
            height=30,
            corner_radius=8,
            fg_color="transparent",
            hover_color=CARD_BACKGROUND,
            text_color=TEXT_SECONDARY,
            command=self._open_settings,
            font=ctk.CTkFont(
                family="Segoe UI Symbol",
                size=18,
            ),
        )
        settings_button.pack(side="right")

        self.auto_refresh_label = ctk.CTkLabel(
            header,
            text=(
                f"●  {self.auto_refresh_minutes}분 "
                "자동 갱신"
            ),
            text_color=GREEN,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=11,
            ),
        )
        self.auto_refresh_label.pack(
            side="right",
            padx=(0, 8),
        )

        self.content_frame = ctk.CTkFrame(
            outer_frame,
            fg_color="transparent",
        )
        self.content_frame.pack(
            fill="both",
            expand=True,
            padx=16,
            pady=(0, 4),
        )

        footer_frame = ctk.CTkFrame(
            outer_frame,
            fg_color="transparent",
        )
        footer_frame.pack(
            fill="x",
            padx=18,
            pady=(0, 13),
        )

        self.status_label = ctk.CTkLabel(
            footer_frame,
            text=f"●  {self.refresh_status_text}",
            text_color=self.refresh_status_color,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=10,
            ),
        )
        self.status_label.pack(side="left")

        self.footer_label = ctk.CTkLabel(
            footer_frame,
            text="",
            text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=10,
            ),
        )

        self.footer_label.pack(side="right")
        self.popup.update_idletasks()
        self._hide_popup_from_taskbar()
        self._apply_windows_rounding()
        self._position_popup()       

    def _open_settings(self) -> None:
        """설정 창을 열거나 기존 설정 창을 앞으로 가져온다."""
        if (
            self.settings_window is not None
            and self.settings_window.winfo_exists()
        ):
            self.settings_window.deiconify()
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        self.settings_window = ctk.CTkToplevel(
            self.root
        )
        self.settings_window.title("Codex Usage 설정")
        self.settings_window.resizable(False, False)
        self.settings_window.configure(
            fg_color=BACKGROUND
        )
        self.settings_window.attributes(
            "-topmost",
            True,
        )
        self.settings_window.protocol(
            "WM_DELETE_WINDOW",
            self._close_settings,
        )

        width = 360
        height = 330
        screen_width = (
            self.settings_window.winfo_screenwidth()
        )
        screen_height = (
            self.settings_window.winfo_screenheight()
        )

        x = (screen_width - width) // 2
        y = (screen_height - height) // 2

        self.settings_window.geometry(
            f"{width}x{height}+{x}+{y}"
        )

        title_label = ctk.CTkLabel(
            self.settings_window,
            text="설정",
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=20,
                weight="bold",
            ),
        )
        title_label.pack(
            anchor="w",
            padx=22,
            pady=(20, 14),
        )

        refresh_row = ctk.CTkFrame(
            self.settings_window,
            height=48,
            corner_radius=10,
            fg_color=CARD_BACKGROUND,
        )
        refresh_row.pack(
            fill="x",
            padx=20,
            pady=5,
        )
        refresh_row.pack_propagate(False)

        refresh_label = ctk.CTkLabel(
            refresh_row,
            text="자동 갱신 주기",
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=12,
            ),
        )
        refresh_label.pack(
            side="left",
            padx=14,
        )

        refresh_menu = ctk.CTkOptionMenu(
            refresh_row,
            values=[
                f"{minutes}분"
                for minutes in REFRESH_INTERVAL_OPTIONS
            ],
            command=self._on_refresh_interval_changed,
            width=92,
            height=30,
            corner_radius=8,
            fg_color="#3A3C40",
            button_color="#44464A",
            button_hover_color="#50535A",
            dropdown_fg_color=CARD_BACKGROUND,
            dropdown_hover_color="#3A3C40",
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=11,
            ),
        )
        refresh_menu.set(
            f"{self.auto_refresh_minutes}분"
        )
        refresh_menu.pack(
            side="right",
            padx=12,
        )

        icon_row = ctk.CTkFrame(
            self.settings_window,
            height=48,
            corner_radius=10,
            fg_color=CARD_BACKGROUND,
        )
        icon_row.pack(
            fill="x",
            padx=20,
            pady=5,
        )
        icon_row.pack_propagate(False)

        icon_label = ctk.CTkLabel(
            icon_row,
            text="트레이 아이콘 표시 기준",
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=12,
            ),
        )
        icon_label.pack(
            side="left",
            padx=14,
        )

        icon_menu = ctk.CTkOptionMenu(
            icon_row,
            values=list(ICON_DISPLAY_OPTIONS),
            command=self._on_icon_display_mode_changed,
            width=92,
            height=30,
            corner_radius=8,
            fg_color="#3A3C40",
            button_color="#44464A",
            button_hover_color="#50535A",
            dropdown_fg_color=CARD_BACKGROUND,
            dropdown_hover_color="#3A3C40",
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=11,
            ),
        )
        icon_menu.set(self.icon_display_mode)
        icon_menu.pack(
            side="right",
            padx=12,
        )

        alert_row = ctk.CTkFrame(
            self.settings_window,
            height=48,
            corner_radius=10,
            fg_color=CARD_BACKGROUND,
        )
        alert_row.pack(
            fill="x",
            padx=20,
            pady=5,
        )
        alert_row.pack_propagate(False)

        alert_label = ctk.CTkLabel(
            alert_row,
            text="잔량 부족 알림",
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=12,
            ),
        )
        alert_label.pack(
            side="left",
            padx=14,
        )

        alert_menu = ctk.CTkOptionMenu(
            alert_row,
            values=[
                "꺼짐",
                "10% 이하",
                "20% 이하",
                "30% 이하",
            ],
            command=(
                self._on_low_balance_threshold_changed
            ),
            width=92,
            height=30,
            corner_radius=8,
            fg_color="#3A3C40",
            button_color="#44464A",
            button_hover_color="#50535A",
            dropdown_fg_color=CARD_BACKGROUND,
            dropdown_hover_color="#3A3C40",
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=11,
            ),
        )

        if self.low_balance_threshold == 0:
            alert_menu.set("꺼짐")
        else:
            alert_menu.set(
                f"{self.low_balance_threshold}% 이하"
            )

        alert_menu.pack(
            side="right",
            padx=12,
        )

        startup_row = ctk.CTkFrame(
            self.settings_window,
            height=48,
            corner_radius=10,
            fg_color=CARD_BACKGROUND,
        )
        startup_row.pack(
            fill="x",
            padx=20,
            pady=5,
        )
        startup_row.pack_propagate(False)

        startup_label = ctk.CTkLabel(
            startup_row,
            text="Windows 시작 시 자동 실행",
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=12,
            ),
        )
        startup_label.pack(
            side="left",
            padx=14,
        )

        self.startup_switch = ctk.CTkSwitch(
            startup_row,
            text="",
            width=48,
            command=self._on_startup_changed,
            progress_color=GREEN,
            button_color=TEXT_PRIMARY,
            button_hover_color="#D8D8D8",
        )
        self.startup_switch.pack(
            side="right",
            padx=14,
        )

        if self.start_with_windows:
            self.startup_switch.select()
        else:
            self.startup_switch.deselect()

    def _on_icon_display_mode_changed(
        self,
        selected_value: str,
    ) -> None:
        """트레이 아이콘에 표시할 한도를 변경한다."""
        mode = self._normalize_icon_display_mode(
            selected_value
        )

        if mode == self.icon_display_mode:
            return

        self.icon_display_mode = mode
        self._save_settings()

        if self.latest_snapshot is not None:
            self._update_tray_status(
                self.latest_snapshot
            )

    def _on_low_balance_threshold_changed(
        self,
        selected_value: str,
    ) -> None:
        """잔량 부족 알림 기준을 변경한다."""
        if selected_value == "꺼짐":
            threshold = 0
        else:
            threshold = int(
                selected_value.split("%", 1)[0]
            )

        threshold = (
            self._normalize_low_balance_threshold(
                threshold
            )
        )

        if threshold == self.low_balance_threshold:
            return

        self.low_balance_threshold = threshold
        self._save_settings()

        if threshold == 0:
            self.low_balance_notified_keys.clear()
            return

        if self.latest_snapshot is not None:
            self._check_low_balance_alert(
                self.latest_snapshot
            )

    def _on_refresh_interval_changed(
        self,
        selected_value: str,
    ) -> None:
        """선택한 자동 갱신 주기를 즉시 적용한다."""
        minutes = self._normalize_refresh_minutes(
            selected_value.removesuffix("분")
        )

        if minutes == self.auto_refresh_minutes:
            return

        self.auto_refresh_minutes = minutes

        self._save_settings()
        self._update_auto_refresh_label()

        # 조회 중이 아니라면 현재 예약을 새 주기로 교체한다.
        if (
            not self.loading
            and self.latest_snapshot is not None
        ):
            self._schedule_auto_refresh()

    def _update_auto_refresh_label(self) -> None:
        """팝업 상단의 자동 갱신 문구를 바꾼다."""
        if self.auto_refresh_label is None:
            return

        self.auto_refresh_label.configure(
            text=(
                f"●  {self.auto_refresh_minutes}분 "
                "자동 갱신"
            )
        )

    def _on_startup_changed(self) -> None:
        """Windows 자동 실행 설정을 즉시 적용한다."""
        if self.startup_switch is None:
            return

        requested_enabled = bool(
            self.startup_switch.get()
        )

        if not self._set_startup_enabled(
            requested_enabled
        ):
            if self.start_with_windows:
                self.startup_switch.select()
            else:
                self.startup_switch.deselect()

            return

        self.start_with_windows = requested_enabled
        self._save_settings()

    def _close_settings(self) -> None:
        """설정 창을 닫는다."""
        if self.settings_window is not None:
            self.settings_window.destroy()
            self.settings_window = None
            self.startup_switch = None

    def _hide_popup_from_taskbar(self) -> None:
        """팝업이 작업표시줄과 Alt+Tab에 나타나지 않게 한다."""
        if sys.platform != "win32" or self.popup is None:
            return

        try:
            user32 = ctypes.windll.user32

            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000

            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020

            window_id = self.popup.winfo_id()
            hwnd = user32.GetParent(window_id)

            if not hwnd:
                hwnd = window_id

            get_window_long = user32.GetWindowLongPtrW
            set_window_long = user32.SetWindowLongPtrW

            get_window_long.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            get_window_long.restype = ctypes.c_ssize_t

            set_window_long.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_ssize_t,
            ]
            set_window_long.restype = ctypes.c_ssize_t

            extended_style = get_window_long(
                hwnd,
                GWL_EXSTYLE,
            )

            extended_style |= WS_EX_TOOLWINDOW
            extended_style &= ~WS_EX_APPWINDOW

            set_window_long(
                hwnd,
                GWL_EXSTYLE,
                extended_style,
            )

            user32.SetWindowPos(
                hwnd,
                None,
                0,
                0,
                0,
                0,
                SWP_NOMOVE
                | SWP_NOSIZE
                | SWP_NOZORDER
                | SWP_FRAMECHANGED,
            )

        except (AttributeError, OSError):
            pass

    def _apply_windows_rounding(self) -> None:
        """Windows 11의 둥근 창 모서리를 요청한다."""
        if sys.platform != "win32" or self.popup is None:
            return

        try:
            window_id = self.popup.winfo_id()

            hwnd = ctypes.windll.user32.GetParent(
                window_id
            )

            if not hwnd:
                hwnd = window_id

            # DWMWA_WINDOW_CORNER_PREFERENCE
            attribute = 33

            # DWMWCP_ROUND
            preference = ctypes.c_int(2)

            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                attribute,
                ctypes.byref(preference),
                ctypes.sizeof(preference),
            )

        except (AttributeError, OSError):
            # 둥근 모서리를 적용하지 못해도 기능에는 문제 없음
            pass

    def _get_popup_hwnd(self) -> int | None:
        """CustomTkinter 팝업의 실제 최상위 Win32 HWND를 반환한다."""
        if self.popup is None:
            return None

        tk_hwnd = self.popup.winfo_id()
        wrapper_hwnd = _USER32.GetParent(tk_hwnd)
        return int(wrapper_hwnd or tk_hwnd)

    def _get_cursor_position(self) -> tuple[int, int] | None:
        """현재 마우스의 Win32 물리 픽셀 좌표를 반환한다."""
        point = wintypes.POINT()

        if not _USER32.GetCursorPos(
            ctypes.byref(point)
        ):
            return None

        return point.x, point.y

    def _get_popup_rect(
        self,
    ) -> tuple[int, int, int, int] | None:
        """팝업의 Win32 물리 픽셀 좌표를 반환한다."""
        hwnd = self._get_popup_hwnd()

        if hwnd is None:
            return None

        rect = wintypes.RECT()

        if not _USER32.GetWindowRect(
            hwnd,
            ctypes.byref(rect),
        ):
            return None

        return (
            rect.left,
            rect.top,
            rect.right,
            rect.bottom,
        )

    def _position_popup(self) -> None:
        """DPI와 무관하게 팝업을 트레이 아이콘 바로 위에 배치한다."""
        if self.popup is None:
            return

        # Tk에는 크기만 맡기고, 실제 화면 위치는 Win32 물리 좌표로 이동한다.
        self.popup.geometry(
            f"{POPUP_WIDTH}x{self.popup_height}"
        )
        self.popup.update_idletasks()

        hwnd = self._get_popup_hwnd()
        popup_rect = self._get_popup_rect()
        tray_rect = self.tray_icon.get_rect()
        work_area = self.tray_icon.get_work_area()

        if (
            hwnd is not None
            and popup_rect is not None
            and tray_rect is not None
            and work_area is not None
        ):
            popup_left, popup_top, popup_right, popup_bottom = popup_rect
            popup_width_px = popup_right - popup_left
            popup_height_px = popup_bottom - popup_top

            tray_left, tray_top, tray_right, tray_bottom = tray_rect
            work_left, work_top, work_right, work_bottom = work_area

            icon_center_x = (tray_left + tray_right) // 2

            try:
                dpi = int(_USER32.GetDpiForWindow(hwnd))
            except (AttributeError, OSError):
                dpi = 96

            if dpi <= 0:
                dpi = 96

            gap_px = max(1, round(POPUP_ICON_GAP * dpi / 96))

            x = icon_center_x - popup_width_px // 2
            x = max(
                work_left,
                min(x, work_right - popup_width_px),
            )

            y = tray_top - popup_height_px - gap_px

            # 작업표시줄이 위쪽에 있는 환경에서는 아이콘 아래에 표시한다.
            if y < work_top:
                y = tray_bottom + gap_px

            flags = (
                SWP_NOSIZE
                | SWP_NOZORDER
                | SWP_NOACTIVATE
            )

            _USER32.SetWindowPos(
                hwnd,
                0,
                x,
                y,
                0,
                0,
                flags,
            )
            return

        # 아이콘 좌표가 아직 준비되지 않은 극초기 상태의 임시 위치
        screen_width = self.popup.winfo_screenwidth()
        screen_height = self.popup.winfo_screenheight()
        x = screen_width - POPUP_WIDTH - 16
        y = screen_height - self.popup_height - TASKBAR_GAP
        self.popup.geometry(
            f"{POPUP_WIDTH}x{self.popup_height}+{x}+{y}"
        )

    def _handle_tray_hover(self) -> None:
        """아이콘 위에 머무르면 팝업 표시를 예약한다."""
        self.hover_leave_started_at = None

        if (
            self._popup_is_visible()
            and self.popup_opened_by_hover
        ):
            return

        if self.hover_show_job is None:
            self.hover_show_job = self.root.after(
                HOVER_SHOW_DELAY_MS,
                self._show_popup_from_hover,
            )

    def _show_popup_from_hover(self) -> None:
        """실제로 아이콘 위에 있을 때만 hover 팝업을 연다."""
        self.hover_show_job = None

        if not self._cursor_is_over_tray_icon():
            return

        self.popup_opened_by_hover = True
        self.hover_leave_started_at = None

        self._show_popup(
            focus=False,
            refresh=False,
        )
        self._start_hover_monitor()

    def _start_hover_monitor(self) -> None:
        """아이콘과 팝업에서 마우스가 벗어났는지 확인한다."""
        if self.hover_monitor_job is None:
            self.hover_monitor_job = self.root.after(
                HOVER_POLL_MS,
                self._monitor_hover,
            )

    def _monitor_hover(self) -> None:
        """아이콘과 팝업 양쪽을 벗어나면 잠시 뒤 숨긴다."""
        self.hover_monitor_job = None

        if (
            not self.popup_opened_by_hover
            or not self._popup_is_visible()
        ):
            return

        pointer = self._get_cursor_position()

        if pointer is None:
            self.hover_monitor_job = self.root.after(
                HOVER_POLL_MS,
                self._monitor_hover,
            )
            return

        pointer_x, pointer_y = pointer

        over_icon = self._point_is_over_tray_icon(
            pointer_x,
            pointer_y,
        )
        over_popup = self._point_is_over_popup(
            pointer_x,
            pointer_y,
        )

        if over_icon or over_popup:
            self.hover_leave_started_at = None
        elif self.hover_leave_started_at is None:
            self.hover_leave_started_at = time.monotonic()
        elif (
            time.monotonic()
            - self.hover_leave_started_at
        ) * 1000 >= HOVER_HIDE_DELAY_MS:
            self._hide_popup()
            return

        self.hover_monitor_job = self.root.after(
            HOVER_POLL_MS,
            self._monitor_hover,
        )

    def _cursor_is_over_tray_icon(self) -> bool:
        """현재 마우스가 트레이 아이콘 위에 있는지 확인한다."""
        pointer = self._get_cursor_position()

        if pointer is None:
            return False

        return self._point_is_over_tray_icon(
            pointer[0],
            pointer[1],
        )

    def _point_is_over_tray_icon(
        self,
        x: int,
        y: int,
    ) -> bool:
        tray_rect = self.tray_icon.get_rect()

        if tray_rect is None:
            return False

        left, top, right, bottom = tray_rect
        margin = 2

        return (
            left - margin <= x <= right + margin
            and top - margin <= y <= bottom + margin
        )

    def _point_is_over_popup(
        self,
        x: int,
        y: int,
    ) -> bool:
        if (
            self.popup is None
            or not self._popup_is_visible()
        ):
            return False

        popup_rect = self._get_popup_rect()

        if popup_rect is None:
            return False

        left, top, right, bottom = popup_rect

        return (
            left <= x <= right
            and top <= y <= bottom
        )

    def _cancel_hover_jobs(self) -> None:
        """예약된 hover 작업을 취소한다."""
        if self.hover_show_job is not None:
            self.root.after_cancel(
                self.hover_show_job
            )
            self.hover_show_job = None

        if self.hover_monitor_job is not None:
            self.root.after_cancel(
                self.hover_monitor_job
            )
            self.hover_monitor_job = None

        self.hover_leave_started_at = None

    def _on_focus_out(self, event: object) -> None:
        """포커스가 빠진 뒤 실제로 창 밖을 클릭했는지 확인한다."""
        if self.popup_opened_by_hover:
            return

        if self.popup is not None:
            self.popup.after(
                80,
                self._hide_if_focus_outside,
            )

    def _hide_if_focus_outside(self) -> None:
        """포커스가 팝업 밖에 있으면 팝업을 숨긴다."""
        if not self._popup_is_visible() or self.popup is None:
            return

        focused_widget = self.popup.focus_get()

        if focused_widget is None:
            self._hide_popup()
            return

        if focused_widget.winfo_toplevel() is not self.popup:
            self._hide_popup()

    def _hide_popup(self) -> None:
        """팝업만 숨기고 트레이 프로그램은 유지한다."""
        self.popup_opened_by_hover = False
        self._cancel_hover_jobs()

        if self.popup is not None:
            self.popup.withdraw()

    def _clear_content(self) -> None:
        """사용량 표시 영역을 비운다."""
        if self.content_frame is None:
            return

        for widget in self.content_frame.winfo_children():
            widget.destroy()

    def _refresh_usage(
        self,
        *,
        show_loading: bool,
    ) -> None:
        """백그라운드에서 최신 사용량을 조회한다."""
        if self.loading:
            return

        self.loading = True
        self._cancel_auto_refresh()

        self._set_refresh_status(
            "갱신 중",
            ORANGE,
        )

        if show_loading:
            self._clear_content()

            if self.content_frame is not None:
                loading_label = ctk.CTkLabel(
                    self.content_frame,
                    text="사용량을 조회하는 중...",
                    text_color=TEXT_SECONDARY,
                    font=ctk.CTkFont(
                        family="맑은 고딕",
                        size=13,
                    ),
                )
                loading_label.pack(
                    expand=True,
                    pady=45,
                )

            if self.footer_label is not None:
                self.footer_label.configure(
                    text="최신 정보를 불러오는 중"
                )

        worker = threading.Thread(
            target=self._usage_worker,
            daemon=True,
        )
        worker.start()

    def _usage_worker(self) -> None:
        """화면을 멈추지 않고 Codex 사용량을 조회한다."""
        try:
            snapshot = get_usage()

            self.command_queue.put(
                ("usage_result", snapshot)
            )

        except CodexClientError as error:
            self.command_queue.put(
                ("usage_error", str(error))
            )

        except Exception as error:
            self.command_queue.put(
                (
                    "usage_error",
                    f"예상하지 못한 오류: {error}",
                )
            )

    def _display_usage(
        self,
        snapshot: UsageSnapshot,
        *,
        schedule_refresh: bool = True
    ) -> None:
        """조회 결과를 아이콘과 팝업에 반영한다."""
        self.loading = False

        windows = self._sort_windows(
            snapshot.windows
        )

        if not windows:
            self._display_error(
                "사용량 한도 정보가 없습니다."
            )
            return

        self._set_refresh_status(
            "정상",
            GREEN,
        )

        self.latest_snapshot = snapshot
        self._update_tray_status(snapshot)
        self._check_low_balance_alert(snapshot)

        if schedule_refresh:
            self._schedule_auto_refresh()

        # 팝업을 아직 만든 적이 없어도 아이콘 갱신은 완료된다.
        if self.content_frame is None:
            return

        self._clear_content()

        plan_name = (
            f"ChatGPT {snapshot.plan_type.capitalize()}"
            if snapshot.plan_type
            else "요금제 정보 없음"
        )

        plan_label = ctk.CTkLabel(
            self.content_frame,
            text=plan_name,
            text_color=TEXT_SECONDARY,
            anchor="w",
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=11,
            ),
        )
        plan_label.pack(
            fill="x",
            pady=(0, 8),
        )

        # 요금제 표시와 한도 카드 개수에 맞춰 창 높이 조절
        self.popup_height = 332 + len(windows) * 118
        self._position_popup()

        for usage_window in windows:
            remaining = usage_window.remaining_percent
            usage_color = self._get_usage_color(
                remaining
            )

            card = ctk.CTkFrame(
                self.content_frame,
                corner_radius=14,
                fg_color=CARD_BACKGROUND,
            )
            card.pack(
                fill="x",
                pady=(0, 9),
            )

            top_row = ctk.CTkFrame(
                card,
                fg_color="transparent",
            )
            top_row.pack(
                fill="x",
                padx=14,
                pady=(12, 5),
            )

            name_label = ctk.CTkLabel(
                top_row,
                text=usage_window.label,
                text_color=TEXT_PRIMARY,
                font=ctk.CTkFont(
                    family="맑은 고딕",
                    size=14,
                    weight="bold",
                ),
            )
            name_label.pack(side="left")

            percent_label = ctk.CTkLabel(
                top_row,
                text=f"{remaining:g}% 남음",
                text_color=usage_color,
                font=ctk.CTkFont(
                    family="맑은 고딕",
                    size=14,
                    weight="bold",
                ),
            )
            percent_label.pack(side="right")

            progress = ctk.CTkProgressBar(
                card,
                height=8,
                corner_radius=4,
                progress_color=usage_color,
                fg_color="#44464A",
            )
            progress.pack(
                fill="x",
                padx=14,
                pady=(2, 9),
            )
            progress.set(remaining / 100.0)

            reset_label = ctk.CTkLabel(
                card,
                text=self._format_reset_line(
                    usage_window.resets_at
                ),
                text_color=TEXT_SECONDARY,
                anchor="w",
                font=ctk.CTkFont(
                    family="맑은 고딕",
                    size=10,
                ),
            )
            reset_label.pack(
                fill="x",
                padx=14,
                pady=(0, 11),
            )

        graph_window = self._select_icon_window(
            snapshot
        )

        if graph_window is not None:
            self._create_history_graph(
                graph_window
            )

        self._update_refresh_footer()

    def _create_history_graph(
        self,
        usage_window: UsageWindow,
    ) -> None:
        """최근 잔여 사용량 기록을 선 그래프로 표시한다."""
        if self.content_frame is None:
            return

        graph_card = ctk.CTkFrame(
            self.content_frame,
            corner_radius=14,
            fg_color=CARD_BACKGROUND,
        )
        graph_card.pack(
            fill="x",
            pady=(0, 9),
        )

        title_row = ctk.CTkFrame(
            graph_card,
            fg_color="transparent",
        )
        title_row.pack(
            fill="x",
            padx=14,
            pady=(11, 3),
        )

        title_label = ctk.CTkLabel(
            title_row,
            text="최근 사용 추이",
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=12,
                weight="bold",
            ),
        )
        title_label.pack(side="left")

        period_label = ctk.CTkLabel(
            title_row,
            text=usage_window.label,
            text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=9,
            ),
        )
        period_label.pack(side="right")

        history_points = load_usage_history(
            usage_window.duration_mins or 0,
            days=HISTORY_GRAPH_DAYS,
            max_points=HISTORY_GRAPH_MAX_POINTS,
        )

        graph_canvas = tk.Canvas(
            graph_card,
            height=HISTORY_GRAPH_HEIGHT,
            background=CARD_BACKGROUND,
            highlightthickness=0,
            borderwidth=0,
        )
        graph_canvas.pack(
            fill="x",
            padx=10,
            pady=(0, 10),
        )

        graph_canvas.after(
            20,
            lambda: self._draw_history_graph(
                graph_canvas,
                history_points,
            ),
        )

    def _draw_history_graph(
        self,
        canvas: tk.Canvas,
        points: tuple[UsageHistoryPoint, ...],
    ) -> None:
        """Canvas에 잔여 사용량 선 그래프를 그린다."""
        if not canvas.winfo_exists():
            return

        canvas.delete("all")

        width = max(
            canvas.winfo_width(),
            280,
        )
        height = HISTORY_GRAPH_HEIGHT

        left = 34
        right = width - 8
        top = 8
        bottom = height - 24

        for percent in (100, 75, 50, 25, 0):
            y = top + (
                100 - percent
            ) / 100 * (bottom - top)

            canvas.create_line(
                left,
                y,
                right,
                y,
                fill="#3A3C40",
                width=1,
            )

            canvas.create_text(
                left - 5,
                y,
                text=f"{percent}%",
                fill=TEXT_SECONDARY,
                anchor="e",
                font=("맑은 고딕", 7),
            )

        if not points:
            canvas.create_text(
                (left + right) / 2,
                (top + bottom) / 2,
                text=(
                    "기록이 쌓이면 "
                    "그래프가 표시됩니다."
                ),
                fill=TEXT_SECONDARY,
                font=("맑은 고딕", 9),
            )
            return

        start_time = points[0].recorded_at
        end_time = points[-1].recorded_at

        total_seconds = max(
            1.0,
            (
                end_time
                - start_time
            ).total_seconds(),
        )

        coordinates: list[float] = []

        for point in points:
            elapsed_seconds = (
                point.recorded_at
                - start_time
            ).total_seconds()

            x = left + (
                elapsed_seconds
                / total_seconds
            ) * (right - left)

            y = top + (
                100
                - point.remaining_percent
            ) / 100 * (bottom - top)

            coordinates.extend((x, y))

        if len(points) == 1:
            x, y = coordinates

            canvas.create_oval(
                x - 3,
                y - 3,
                x + 3,
                y + 3,
                fill=GREEN,
                outline="",
            )

        else:
            canvas.create_line(
                *coordinates,
                fill=GREEN,
                width=2,
            )

            marker_step = max(
                1,
                len(points) // 10,
            )

            for index in range(
                0,
                len(points),
                marker_step,
            ):
                x = coordinates[index * 2]
                y = coordinates[index * 2 + 1]

                canvas.create_oval(
                    x - 2,
                    y - 2,
                    x + 2,
                    y + 2,
                    fill=GREEN,
                    outline="",
                )

        same_day = (
            start_time.date()
            == end_time.date()
        )

        time_format = (
            "%H:%M"
            if same_day
            else "%m/%d"
        )

        canvas.create_text(
            left,
            height - 8,
            text=start_time.strftime(
                time_format
            ),
            fill=TEXT_SECONDARY,
            anchor="w",
            font=("맑은 고딕", 7),
        )

        canvas.create_text(
            right,
            height - 8,
            text=end_time.strftime(
                time_format
            ),
            fill=TEXT_SECONDARY,
            anchor="e",
            font=("맑은 고딕", 7),
        )


    def _display_error(self, message: str) -> None:
        """조회 오류를 아이콘과 팝업에 표시한다."""
        self.loading = False

        self._set_refresh_status(
            "최근 갱신 실패",
            RED,
        )

        if self.latest_snapshot is None:
            self._set_tray_error()
        else:
            # 일시적인 오류라면 마지막 정상 아이콘은 유지한다.
            self._update_tray_status(
                self.latest_snapshot
            )

        self._schedule_auto_refresh()

        # 백그라운드 갱신 실패로 숨겨진 팝업 내용을
        # 지우지 않도록, 보이는 경우에만 오류를 그린다.
        if (
            self.content_frame is None
            or not self._popup_is_visible()
        ):
            return

        self._clear_content()
        self.popup_height = 200
        self._position_popup()

        error_label = ctk.CTkLabel(
            self.content_frame,
            text=message,
            text_color=RED,
            wraplength=290,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=12,
            ),
        )
        error_label.pack(
            expand=True,
            pady=35,
        )

        if self.footer_label is not None:
            self.footer_label.configure(
                text="조회 실패"
            )

    def _set_refresh_status(
        self,
        text: str,
        color: str,
    ) -> None:
        """현재 갱신 상태를 저장하고 팝업에 표시한다."""
        self.refresh_status_text = text
        self.refresh_status_color = color

        if self.status_label is not None:
            self.status_label.configure(
                text=f"●  {text}",
                text_color=color,
            )

    def _schedule_auto_refresh(self) -> None:
        """팝업 상태와 관계없이 다음 갱신을 예약한다."""
        self._cancel_auto_refresh()

        refresh_interval_ms = (
            self.auto_refresh_minutes
            * 60
            * 1000
        )

        self.next_refresh_at = (
            time.monotonic()
            + refresh_interval_ms / 1000
        )

        self.auto_refresh_job = self.root.after(
            refresh_interval_ms,
            self._auto_refresh,
        )

        self._restart_refresh_countdown()

    def _auto_refresh(self) -> None:
        """예약된 자동 갱신을 실행한다."""
        self.auto_refresh_job = None
        self.next_refresh_at = None

        self._cancel_refresh_countdown()
        self._update_refresh_footer()
        self._refresh_usage(show_loading=False)

    def _cancel_auto_refresh(self) -> None:
        """예약된 자동 갱신을 취소한다."""
        if self.auto_refresh_job is None:
            return

        self.root.after_cancel(
            self.auto_refresh_job
        )
        self.auto_refresh_job = None

    def _restart_refresh_countdown(self) -> None:
        """자동 갱신 카운트다운을 처음부터 시작한다."""
        self._cancel_refresh_countdown()
        self._update_refresh_countdown()

    def _update_refresh_countdown(self) -> None:
        """다음 자동 갱신까지 남은 시간을 1초마다 갱신한다."""
        self.refresh_countdown_job = None
        self._update_refresh_footer()

        if self.next_refresh_at is None:
            return

        if time.monotonic() >= self.next_refresh_at:
            return

        self.refresh_countdown_job = self.root.after(
            1000,
            self._update_refresh_countdown,
        )

    def _cancel_refresh_countdown(self) -> None:
        """실행 중인 카운트다운 갱신을 취소한다."""
        if self.refresh_countdown_job is None:
            return

        self.root.after_cancel(
            self.refresh_countdown_job
        )
        self.refresh_countdown_job = None

    def _update_refresh_footer(self) -> None:
        """다음 갱신 시간과 마지막 갱신 시각을 표시한다."""
        if self.footer_label is None:
            return

        if self.next_refresh_at is None:
            countdown_text = "--:--"
        else:
            remaining_seconds = max(
                0,
                int(
                    self.next_refresh_at
                    - time.monotonic()
                    + 0.999
                ),
            )
            minutes, seconds = divmod(
                remaining_seconds,
                60,
            )
            countdown_text = (
                f"{minutes:02d}:{seconds:02d}"
            )

        if self.latest_snapshot is None:
            fetched_text = "--:--:--"
        else:
            fetched_text = (
                self.latest_snapshot.fetched_at.strftime(
                    "%H:%M:%S"
                )
            )

        self.footer_label.configure(
            text=(
                f"다음 갱신 {countdown_text}"
                f"  ·  마지막 갱신 {fetched_text}"
            )
        )

    @staticmethod
    def _get_usage_color(
        remaining_percent: float,
    ) -> str:
        """남은 비율에 따라 진행 막대 색상을 결정한다."""
        if remaining_percent > 50:
            return GREEN

        if remaining_percent > 20:
            return ORANGE

        return RED

    @staticmethod
    def _format_reset_line(
        reset_time: datetime | None,
    ) -> str:
        """초기화까지 남은 시간과 실제 시각을 함께 표시한다."""
        if reset_time is None:
            return "초기화 시각 정보 없음"

        remaining = reset_time - datetime.now()
        total_seconds = max(
            0,
            int(remaining.total_seconds()),
        )

        days, remainder = divmod(
            total_seconds,
            86_400,
        )
        hours, remainder = divmod(
            remainder,
            3_600,
        )
        minutes = remainder // 60

        if days > 0:
            relative_text = (
                f"{days}일 {hours}시간 후 초기화"
            )
        elif hours > 0:
            relative_text = (
                f"{hours}시간 {minutes}분 후 초기화"
            )
        elif minutes > 0:
            relative_text = (
                f"{minutes}분 후 초기화"
            )
        else:
            relative_text = "곧 초기화"

        absolute_text = reset_time.strftime(
            "%m월 %d일 %H:%M"
        )

        return (
            f"{relative_text}  ·  {absolute_text}"
        )

    def _quit_app(self) -> None:
        """트레이 아이콘과 GUI를 모두 종료한다."""
        self._cancel_auto_refresh()
        self._cancel_refresh_countdown()
        self.tray_icon.stop()

        if self.popup is not None:
            self.popup.destroy()

        if self.settings_window is not None:
            self.settings_window.destroy()

        self.root.destroy()

    def run(self) -> None:
        """트레이와 CustomTkinter 이벤트 루프를 시작한다."""
        tray_thread = threading.Thread(
            target=self.tray_icon.run,
            daemon=True,
        )
        tray_thread.start()

        self.root.after(
            100,
            self._process_commands,
        )
        self.root.after(
            300,
            lambda: self._refresh_usage(
                show_loading=False
            ),
        )
        self.root.mainloop()


def main() -> None:
    mutex_handle = (
        _acquire_single_instance_mutex()
    )

    # 이미 실행 중이면 두 번째 앱은 바로 종료한다.
    if mutex_handle is None:
        return

    try:
        app = UsageTrayApp()
        app.run()
    finally:
        _close_single_instance_mutex(
            mutex_handle
        )


if __name__ == "__main__":
    main()