from __future__ import annotations

import ctypes
import os
import tempfile
import threading
import time
from ctypes import wintypes
from typing import Callable

from PIL import Image


if os.name != "nt":
    raise RuntimeError("NativeTrayIcon은 Windows에서만 사용할 수 있습니다.")


# Windows messages
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_CONTEXTMENU = 0x007B
WM_MOUSEMOVE = 0x0200
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_USER = 0x0400
WM_APP = 0x8000

NIN_SELECT = WM_USER
NIN_KEYSELECT = WM_USER + 1

TRAY_CALLBACK_MESSAGE = WM_APP + 1

# Shell_NotifyIcon
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_GUID = 0x00000020

NOTIFYICON_VERSION_4 = 4

# Menus
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

MENU_SHOW = 1001
MENU_REFRESH = 1002
MENU_QUIT = 1003

# LoadImage
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

MONITOR_DEFAULTTONEAREST = 0x00000002

ERROR_CLASS_ALREADY_EXISTS = 1410


user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


# 이 GUID는 앱의 트레이 아이콘을 영구적으로 식별한다.
# 한 번 배포한 뒤에는 값을 바꾸지 않는다.
CODEX_TRAY_GUID = GUID(
    0x01CA0C7C,
    0x0D26,
    0x4FB1,
    (wintypes.BYTE * 8)(
        0x9A,
        0x0B,
        0xCD,
        0x60,
        0x88,
        0xD5,
        0x22,
        0x00,
    ),
)


class _TimeoutOrVersion(ctypes.Union):
    _fields_ = [
        ("uTimeout", wintypes.UINT),
        ("uVersion", wintypes.UINT),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _anonymous_ = ("timeout_or_version",)
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HANDLE),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("timeout_or_version", _TimeoutOrVersion),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HANDLE),
    ]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("guidItem", GUID),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HANDLE),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND

user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = LRESULT

user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL

user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None

user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostMessageW.restype = wintypes.BOOL

user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = wintypes.BOOL

user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL

user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT

user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT

user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE,
    wintypes.LPCWSTR,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE

user32.DestroyIcon.argtypes = [wintypes.HANDLE]
user32.DestroyIcon.restype = wintypes.BOOL

user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wintypes.HMENU

user32.AppendMenuW.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_size_t,
    wintypes.LPCWSTR,
]
user32.AppendMenuW.restype = wintypes.BOOL

user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    ctypes.POINTER(wintypes.RECT),
]
user32.TrackPopupMenu.restype = wintypes.UINT

user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyMenu.restype = wintypes.BOOL

user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL

user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL

user32.MonitorFromRect.argtypes = [
    ctypes.POINTER(wintypes.RECT),
    wintypes.DWORD,
]
user32.MonitorFromRect.restype = wintypes.HMONITOR

user32.GetMonitorInfoW.argtypes = [
    wintypes.HMONITOR,
    ctypes.POINTER(MONITORINFO),
]
user32.GetMonitorInfoW.restype = wintypes.BOOL

shell32.Shell_NotifyIconW.argtypes = [
    wintypes.DWORD,
    ctypes.POINTER(NOTIFYICONDATAW),
]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

shell32.Shell_NotifyIconGetRect.argtypes = [
    ctypes.POINTER(NOTIFYICONIDENTIFIER),
    ctypes.POINTER(wintypes.RECT),
]
shell32.Shell_NotifyIconGetRect.restype = wintypes.LONG

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE


_instances: dict[int, "NativeTrayIcon"] = {}


@WNDPROC
def _window_proc(
    hwnd: int,
    message: int,
    wparam: int,
    lparam: int,
) -> int:
    instance = _instances.get(int(hwnd))

    if instance is not None:
        return instance._handle_window_message(
            hwnd,
            message,
            wparam,
            lparam,
        )

    return user32.DefWindowProcW(
        hwnd,
        message,
        wparam,
        lparam,
    )


class NativeTrayIcon:
    """Windows 알림 영역 아이콘과 hover 메시지를 직접 처리한다."""

    ICON_ID = 1
    CLASS_NAME = "CodexUsageNativeTrayWindow"

    def __init__(
        self,
        *,
        icon: Image.Image,
        on_activate: Callable[[], None],
        on_refresh: Callable[[], None],
        on_quit: Callable[[], None],
        on_hover: Callable[[], None],
    ) -> None:
        self._image = icon.copy()

        self._on_activate = on_activate
        self._on_refresh = on_refresh
        self._on_quit = on_quit
        self._on_hover = on_hover

        self._hwnd: int | None = None
        self._hicon: int | None = None
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._stopping = False

        self._taskbar_created_message = (
            user32.RegisterWindowMessageW(
                "TaskbarCreated"
            )
        )

        self._last_activate_at = 0.0
        self._last_context_at = 0.0
        self._last_hover_at = 0.0

    @property
    def icon(self) -> Image.Image:
        return self._image

    @icon.setter
    def icon(self, image: Image.Image) -> None:
        self._image = image.copy()

        if self._ready.is_set():
            self._replace_icon(self._image)

    def run(self) -> None:
        """현재 스레드에서 Windows 메시지 루프를 실행한다."""
        instance = kernel32.GetModuleHandleW(None)
        self._register_window_class(instance)

        hwnd = user32.CreateWindowExW(
            0,
            self.CLASS_NAME,
            self.CLASS_NAME,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            instance,
            None,
        )

        if not hwnd:
            raise ctypes.WinError(
                ctypes.get_last_error()
            )

        self._hwnd = int(hwnd)
        _instances[self._hwnd] = self

        try:
            self._replace_icon(
                self._image,
                add=True,
            )
            self._ready.set()

            message = wintypes.MSG()

            while user32.GetMessageW(
                ctypes.byref(message),
                None,
                0,
                0,
            ) > 0:
                user32.TranslateMessage(
                    ctypes.byref(message)
                )
                user32.DispatchMessageW(
                    ctypes.byref(message)
                )
        finally:
            self._ready.clear()
            self._delete_icon()

            if self._hwnd is not None:
                _instances.pop(
                    self._hwnd,
                    None,
                )

            self._hwnd = None

    def stop(self) -> None:
        """알림 영역 아이콘과 메시지 창을 종료한다."""
        self._stopping = True

        if self._hwnd is not None:
            user32.PostMessageW(
                self._hwnd,
                WM_CLOSE,
                0,
                0,
            )

    def get_rect(
        self,
    ) -> tuple[int, int, int, int] | None:
        """아이콘의 실제 화면 좌표를 반환한다."""
        if (
            not self._ready.is_set()
            or self._hwnd is None
        ):
            return None

        identifier = NOTIFYICONIDENTIFIER()
        identifier.cbSize = ctypes.sizeof(
            NOTIFYICONIDENTIFIER
        )
        # 고정 GUID가 있으면 Windows는 hWnd와 uID보다 GUID를 우선한다.
        identifier.hWnd = self._hwnd
        identifier.uID = self.ICON_ID
        identifier.guidItem = CODEX_TRAY_GUID

        rect = wintypes.RECT()

        result = shell32.Shell_NotifyIconGetRect(
            ctypes.byref(identifier),
            ctypes.byref(rect),
        )

        if result != 0:
            return None

        return (
            rect.left,
            rect.top,
            rect.right,
            rect.bottom,
        )

    def get_work_area(
        self,
    ) -> tuple[int, int, int, int] | None:
        """아이콘이 있는 모니터의 작업 영역을 반환한다."""
        icon_rect = self.get_rect()

        if icon_rect is None:
            return None

        rect = wintypes.RECT(*icon_rect)
        monitor = user32.MonitorFromRect(
            ctypes.byref(rect),
            MONITOR_DEFAULTTONEAREST,
        )

        if not monitor:
            return None

        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(
            MONITORINFO
        )

        if not user32.GetMonitorInfoW(
            monitor,
            ctypes.byref(info),
        ):
            return None

        return (
            info.rcWork.left,
            info.rcWork.top,
            info.rcWork.right,
            info.rcWork.bottom,
        )

    def _register_window_class(
        self,
        instance: int,
    ) -> None:
        window_class = WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(
            WNDCLASSEXW
        )
        window_class.lpfnWndProc = _window_proc
        window_class.hInstance = instance
        window_class.lpszClassName = (
            self.CLASS_NAME
        )

        atom = user32.RegisterClassExW(
            ctypes.byref(window_class)
        )

        if atom:
            return

        error_code = ctypes.get_last_error()

        if error_code != ERROR_CLASS_ALREADY_EXISTS:
            raise ctypes.WinError(error_code)

    def _make_notify_data(
        self,
        *,
        flags: int = 0,
        hicon: int | None = None,
    ) -> NOTIFYICONDATAW:
        if self._hwnd is None:
            raise RuntimeError(
                "트레이 메시지 창이 아직 없습니다."
            )

        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(
            NOTIFYICONDATAW
        )
        data.hWnd = self._hwnd
        data.uID = self.ICON_ID
        data.uFlags = flags | NIF_GUID
        data.uCallbackMessage = (
            TRAY_CALLBACK_MESSAGE
        )
        data.guidItem = CODEX_TRAY_GUID

        if hicon is not None:
            data.hIcon = hicon

        return data

    def _replace_icon(
        self,
        image: Image.Image,
        *,
        add: bool = False,
    ) -> None:
        with self._lock:
            new_hicon = self._load_hicon(image)

            if add:
                data = self._make_notify_data(
                    flags=(
                        NIF_MESSAGE
                        | NIF_ICON
                    ),
                    hicon=new_hicon,
                )
                operation = NIM_ADD
            else:
                data = self._make_notify_data(
                    flags=NIF_ICON,
                    hicon=new_hicon,
                )
                operation = NIM_MODIFY

            if not shell32.Shell_NotifyIconW(
                operation,
                ctypes.byref(data),
            ):
                user32.DestroyIcon(new_hicon)
                raise ctypes.WinError(
                    ctypes.get_last_error()
                )

            if add:
                version_data = (
                    self._make_notify_data()
                )
                version_data.uVersion = (
                    NOTIFYICON_VERSION_4
                )
                shell32.Shell_NotifyIconW(
                    NIM_SETVERSION,
                    ctypes.byref(version_data),
                )

            old_hicon = self._hicon
            self._hicon = int(new_hicon)

            if old_hicon is not None:
                user32.DestroyIcon(old_hicon)

    @staticmethod
    def _load_hicon(
        image: Image.Image,
    ) -> int:
        file_descriptor, icon_path = (
            tempfile.mkstemp(
                suffix=".ico",
                prefix="codex_usage_",
            )
        )
        os.close(file_descriptor)

        try:
            image.convert("RGBA").save(
                icon_path,
                format="ICO",
                sizes=[
                    (16, 16),
                    (20, 20),
                    (24, 24),
                    (32, 32),
                ],
            )

            hicon = user32.LoadImageW(
                None,
                icon_path,
                IMAGE_ICON,
                32,
                32,
                LR_LOADFROMFILE,
            )

            if not hicon:
                raise ctypes.WinError(
                    ctypes.get_last_error()
                )

            return int(hicon)
        finally:
            try:
                os.remove(icon_path)
            except OSError:
                pass

    def _delete_icon(self) -> None:
        with self._lock:
            if self._hwnd is not None:
                try:
                    data = self._make_notify_data()
                    shell32.Shell_NotifyIconW(
                        NIM_DELETE,
                        ctypes.byref(data),
                    )
                except RuntimeError:
                    pass

            if self._hicon is not None:
                user32.DestroyIcon(
                    self._hicon
                )
                self._hicon = None

    def _handle_window_message(
        self,
        hwnd: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> int:
        if message == TRAY_CALLBACK_MESSAGE:
            event = int(lparam) & 0xFFFF
            self._handle_tray_event(event)
            return 0

        if (
            self._taskbar_created_message
            and message
            == self._taskbar_created_message
        ):
            if not self._stopping:
                self._replace_icon(
                    self._image,
                    add=True,
                )
            return 0

        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0

        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(
            hwnd,
            message,
            wparam,
            lparam,
        )

    def _handle_tray_event(
        self,
        event: int,
    ) -> None:
        if event == WM_MOUSEMOVE:
            now = time.monotonic()

            if now - self._last_hover_at > 0.04:
                self._last_hover_at = now
                self._on_hover()
            return

        if event in (
            NIN_SELECT,
            NIN_KEYSELECT,
            WM_LBUTTONUP,
        ):
            now = time.monotonic()

            if now - self._last_activate_at > 0.2:
                self._last_activate_at = now
                self._on_activate()
            return

        if event in (
            WM_CONTEXTMENU,
            WM_RBUTTONUP,
        ):
            now = time.monotonic()

            if now - self._last_context_at > 0.2:
                self._last_context_at = now
                self._show_context_menu()

    def _show_context_menu(self) -> None:
        if self._hwnd is None:
            return

        menu = user32.CreatePopupMenu()

        if not menu:
            return

        try:
            user32.AppendMenuW(
                menu,
                MF_STRING,
                MENU_SHOW,
                "사용량 보기",
            )
            user32.AppendMenuW(
                menu,
                MF_STRING,
                MENU_REFRESH,
                "새로고침",
            )
            user32.AppendMenuW(
                menu,
                MF_SEPARATOR,
                0,
                None,
            )
            user32.AppendMenuW(
                menu,
                MF_STRING,
                MENU_QUIT,
                "종료",
            )

            point = wintypes.POINT()

            if not user32.GetCursorPos(
                ctypes.byref(point)
            ):
                return

            user32.SetForegroundWindow(
                self._hwnd
            )

            command = user32.TrackPopupMenu(
                menu,
                (
                    TPM_RIGHTBUTTON
                    | TPM_RETURNCMD
                ),
                point.x,
                point.y,
                0,
                self._hwnd,
                None,
            )

            user32.PostMessageW(
                self._hwnd,
                WM_NULL,
                0,
                0,
            )

            if command == MENU_SHOW:
                self._on_activate()
            elif command == MENU_REFRESH:
                self._on_refresh()
            elif command == MENU_QUIT:
                self._on_quit()
        finally:
            user32.DestroyMenu(menu)
