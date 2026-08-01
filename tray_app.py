import ctypes
from ctypes import wintypes
import queue
import sys
import threading
import time
from datetime import datetime
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont

from native_tray import NativeTrayIcon

from codex_client import (
    CodexClientError,
    UsageSnapshot,
    UsageWindow,
    get_usage,
)


POPUP_WIDTH = 340
INITIAL_POPUP_HEIGHT = 220
TASKBAR_GAP = 72

# 팝업을 닫아도 2분마다 아이콘과 사용량을 갱신
AUTO_REFRESH_MS = 2 * 60 * 1000

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


# Tk 좌표와 Win32 트레이 좌표가 다른 DPI 환경에서도
# 같은 물리 픽셀 좌표계를 사용하기 위한 Win32 함수들
_USER32 = ctypes.WinDLL("user32", use_last_error=True)

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

try:
    _USER32.GetDpiForWindow.argtypes = [wintypes.HWND]
    _USER32.GetDpiForWindow.restype = wintypes.UINT
except AttributeError:
    pass

SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


class UsageTrayApp:
    """Codex 사용량 트레이 애플리케이션."""

    def __init__(self) -> None:
        ctk.set_appearance_mode("dark")

        # CustomTkinter 이벤트 루프를 실행하기 위한 숨겨진 창
        self.root = ctk.CTk()
        self.root.withdraw()

        self.popup: ctk.CTkToplevel | None = None
        self.content_frame: ctk.CTkFrame | None = None
        self.footer_label: ctk.CTkLabel | None = None

        self.popup_height = INITIAL_POPUP_HEIGHT
        self.loading = False
        self.auto_refresh_job: str | None = None
        self.next_refresh_at: float | None = None
        self.refresh_countdown_job: str | None = None
        self.latest_snapshot: UsageSnapshot | None = None

        self.popup_opened_by_hover = False
        self.hover_show_job: str | None = None
        self.hover_monitor_job: str | None = None
        self.hover_leave_started_at: float | None = None

        # 트레이 스레드가 GUI 스레드에 명령을 전달하는 통로
        self.command_queue: queue.Queue[
            tuple[str, Any | None]
        ] = queue.Queue()

        self.tray_icon = self._create_tray_icon()

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

    @staticmethod
    def _select_icon_window(
        snapshot: UsageSnapshot,
    ) -> UsageWindow | None:
        """아이콘에는 주간 한도를 우선 표시한다."""
        windows = snapshot.windows

        for duration_mins in (10_080, 300):
            for usage_window in windows:
                if (usage_window.duration_mins == duration_mins):
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
                refresh=True,
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

        status_label = ctk.CTkLabel(
            header,
            text="●  2분 자동 갱신",
            text_color=GREEN,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=11,
            ),
        )
        status_label.pack(side="right")

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

        self.footer_label = ctk.CTkLabel(
            outer_frame,
            text="",
            text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(
                family="맑은 고딕",
                size=10,
            ),
        )
        self.footer_label.pack(
            anchor="e",
            padx=18,
            pady=(0, 13),
        )

        self.popup.update_idletasks()
        self._hide_popup_from_taskbar()
        self._apply_windows_rounding()
        self._position_popup()       

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

        self.latest_snapshot = snapshot
        self._update_tray_status(snapshot)

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
        self.popup_height = 146 + len(windows) * 118
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

        self._update_refresh_footer()


    def _display_error(self, message: str) -> None:
        """조회 오류를 아이콘과 팝업에 표시한다."""
        self.loading = False

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

    def _schedule_auto_refresh(self) -> None:
        """팝업 상태와 관계없이 다음 갱신을 예약한다."""
        self._cancel_auto_refresh()

        self.next_refresh_at = (
            time.monotonic()
            + AUTO_REFRESH_MS / 1000
        )

        self.auto_refresh_job = self.root.after(
            AUTO_REFRESH_MS,
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
    app = UsageTrayApp()
    app.run()


if __name__ == "__main__":
    main()