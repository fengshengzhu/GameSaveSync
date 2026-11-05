# -*- coding: utf-8 -*-
"""Windows 系统托盘图标：纯 ctypes 实现，不引入第三方依赖。
非 Windows 平台 available() 返回 False，界面自动降级为普通关闭行为。
"""
import queue
import sys
import threading

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    # wintypes 没有 LRESULT，它与 LPARAM 同为 LONG_PTR
    LRESULT = wintypes.LPARAM

WM_APP_TRAY = 0x8000 + 1  # WM_APP + 1，托盘回调消息
WM_QUIT = 0x0012

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO = 0x1

IDI_APPLICATION = 32512
MF_STRING = 0x0
TPM_RETURNCMD = 0x0100
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CLOSE = 0x0010

EVT_RESTORE = "restore"
EVT_QUIT = "quit"


def available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND,
                                               wintypes.UINT, wintypes.WPARAM,
                                               wintypes.LPARAM)),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", ctypes.c_void_p),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]


class TrayIcon:
    """在独立线程里创建隐藏窗口 + 托盘图标并跑消息泵。
    用户操作通过 events 队列（线程安全）交给主线程轮询处理。
    """

    def __init__(self, tooltip: str, events: "queue.Queue"):
        self.tooltip = tooltip[:127]
        self.events = events
        self._thread = None
        self._tid = None
        self._hwnd = None
        self._nid = None
        self._hicon = None

    # ---------- 外部接口 ----------
    def start(self):
        if not available() or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="GameSaveSync-Tray")
        self._thread.start()

    def stop(self):
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)

    def show_balloon(self, title: str, message: str):
        """首次最小化时给一个气泡提示（通知中心可见）。"""
        if not self._hwnd or not self._nid:
            return
        nid = self._nid
        nid.dwInfoFlags = NIIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    # ---------- 内部实现 ----------
    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._tid = kernel32.GetCurrentThreadId()

        _WndProc = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND,
                                      wintypes.UINT, wintypes.WPARAM,
                                      wintypes.LPARAM)

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_APP_TRAY:
                if lparam == WM_LBUTTONUP or lparam == WM_LBUTTONDBLCLK:
                    self.events.put(EVT_RESTORE)
                elif lparam == WM_RBUTTONUP:
                    self._popup_menu(hwnd, user32)
                return 0
            if msg == WM_CLOSE:
                self.events.put(EVT_QUIT)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wnd_proc_cb = _WndProc(wnd_proc)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        hinstance = kernel32.GetModuleHandleW(None)
        self._hicon = user32.LoadIconW(None, IDI_APPLICATION)
        class_name = "GameSaveSyncTrayWnd"
        wc = WNDCLASSW()
        wc.lpfnWndProc = wnd_proc_cb
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        wc.hIcon = self._hicon
        if not user32.RegisterClassW(ctypes.byref(wc)):
            return

        # 隐藏的普通顶层窗口（不用 message-only，托盘回调走它）
        self._hwnd = user32.CreateWindowExW(0, class_name, "GameSaveSync",
                                            0, 0, 0, 0, 0, None, None,
                                            hinstance, None)
        if not self._hwnd:
            return

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip
        self._nid = nid
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        # 消息泵
        msg_struct = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg_struct), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg_struct))
            user32.DispatchMessageW(ctypes.byref(msg_struct))

        ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        user32.DestroyWindow(self._hwnd)

    def _popup_menu(self, hwnd, user32):
        hmenu = user32.CreatePopupMenu()
        CMD_SHOW, CMD_QUIT = 1001, 1002
        user32.AppendMenuW(hmenu, MF_STRING, CMD_SHOW, "显示主窗口")
        user32.AppendMenuW(hmenu, MF_STRING, CMD_QUIT, "退出")
        # 让菜单能正常消失：先把焦点交给托盘窗口
        user32.SetForegroundWindow(hwnd)
        x, y = self._cursor_pos(user32)
        cmd = user32.TrackPopupMenuEx(hmenu, TPM_RETURNCMD, x, y, hwnd, None)
        user32.DestroyMenu(hmenu)
        if cmd == CMD_SHOW:
            self.events.put(EVT_RESTORE)
        elif cmd == CMD_QUIT:
            self.events.put(EVT_QUIT)

    @staticmethod
    def _cursor_pos(user32):
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y
