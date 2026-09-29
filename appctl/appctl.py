#!/usr/bin/env python3
"""appctl v0.2 — cross-platform application lifecycle & observation adapter for The Architect.

One typed vocabulary for app lifecycle and observation on any OS, so the agent never
invents raw shell text. Every command emits JSON evidence the verifier can check.

    appctl open <app> [--args ...]                         start an application
    appctl focus <app>                                     bring its window to foreground
    appctl see <app> [--output <path>]                     capture window screenshot (PNG)
    appctl diff <before.png> <after.png> [--region ..]     detect pixel changes / verify UI
    appctl type <app> <text>                               send typed text to application
    appctl key <app> <key>                                 send keystroke/combination
    appctl status <app> [--json]                           is it running? pid, window title
    appctl list                                            running GUI processes
    appctl quit <app> [--force]                            close gracefully, or force-kill

Exit codes: 0 = ok, 1 = action failed (app not found / not running), 2 = usage error.
All output is JSON on stdout: {"ok": bool, "action": str, ...}.
"""

import argparse
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import time
import zlib

try:
    from appctl.registry import registry, SchemaValidationError
except ImportError:
    try:
        from registry import registry, SchemaValidationError
    except ImportError:
        registry = None
        SchemaValidationError = Exception

try:
    from appctl.node import execute_macro_node
except ImportError:
    try:
        from node import execute_macro_node
    except ImportError:
        execute_macro_node = None

try:
    from appctl.a11y import run_a11y_tree, run_a11y_query, run_a11y_click
except ImportError:
    try:
        from a11y import run_a11y_tree, run_a11y_query, run_a11y_click
    except ImportError:
        run_a11y_tree = run_a11y_query = run_a11y_click = None

try:
    from appctl.vision import ocr_image, run_vision_find, run_vision_click
except ImportError:
    try:
        from vision import ocr_image, run_vision_find, run_vision_click
    except ImportError:
        ocr_image = run_vision_find = run_vision_click = None

try:
    from appctl.router import LocalRouter, run_benchmark
except ImportError:
    try:
        from router import LocalRouter, run_benchmark
    except ImportError:
        LocalRouter = run_benchmark = None

try:
    from appctl.browser import (
        detect_browsers,
        fork_browser,
        list_browser_workers,
        destroy_browser_worker,
        browser_status,
        bridge_browser,
        SAOBrowserBridge,
        get_unified_browser_url,
        CDPClient,
        load_cookie_file,
    )
except ImportError:
    try:
        from browser import (
            detect_browsers,
            fork_browser,
            list_browser_workers,
            destroy_browser_worker,
            browser_status,
            bridge_browser,
            SAOBrowserBridge,
            get_unified_browser_url,
            CDPClient,
            load_cookie_file,
        )
    except ImportError:
        detect_browsers = fork_browser = list_browser_workers = destroy_browser_worker = browser_status = bridge_browser = SAOBrowserBridge = get_unified_browser_url = CDPClient = load_cookie_file = None

OS = platform.system()  # Windows | Darwin | Linux


def emit(payload, code=0):
    print(json.dumps(payload, indent=2 if sys.stdout.isatty() else None))
    sys.exit(code)


def fail(action, reason, **extra):
    emit({"ok": False, "action": action, "error": reason, **extra}, code=1)


def run(cmd, **kw):
    """Run a command, return (returncode, stdout stripped). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, **kw)
        return p.returncode, p.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return 127, str(e)


# ---------------------------------------------------------------- Windows ----
if OS == "Windows":
    import ctypes
    from ctypes import wintypes
    import time

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_QUERY_INFORMATION = 0x0400
    WM_CLOSE = 0x0010
    WM_SYSCOMMAND = 0x0112
    SC_CLOSE = 0xF060
    SW_RESTORE = 9
    SW_SHOW = 5
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_SHOWWINDOW = 0x0040

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [
                ("mi", MOUSEINPUT),
                ("ki", KEYBDINPUT),
                ("hi", HARDWAREINPUT),
            ]
        _fields_ = [
            ("type", wintypes.DWORD),
            ("u", _U),
        ]

    _gdi32 = ctypes.windll.gdi32
    _dwmapi = ctypes.windll.dwmapi

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _DESKTOPENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.LPWSTR, wintypes.LPARAM)

    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _user32.OpenDesktopW.restype = wintypes.HANDLE
    _user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _user32.OpenInputDesktop.restype = wintypes.HANDLE
    _user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    _user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, _WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumDesktopsW.argtypes = [wintypes.HANDLE, _DESKTOPENUMPROC, wintypes.LPARAM]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    _user32.SetThreadDesktop.restype = wintypes.BOOL
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.LockSetForegroundWindow.argtypes = [wintypes.UINT]
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.UINT,
    ]
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    _user32.PrintWindow.restype = wintypes.BOOL
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
    _user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    _user32.SetCursorPos.restype = wintypes.BOOL
    _user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_ulong]
    _user32.mouse_event.restype = None

    try:
        _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            _user32.SetProcessDPIAware()
        except Exception:
            pass

    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    _gdi32.BitBlt.restype = wintypes.BOOL
    _gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    _gdi32.GetDIBits.restype = ctypes.c_int
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]

    def _win_attach_desktop():
        """Attach current thread to the active input desktop or Default desktop."""
        hdesk = _user32.OpenInputDesktop(0, False, 0x01FF)
        if not hdesk:
            hdesk = _user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk:
            _user32.SetThreadDesktop(hdesk)
            return hdesk
        return None

    _win_attach_desktop()


def _ps(script):
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])


def _win_all_processes():
    """Return list of dicts: [{'pid': int, 'name': str}] for all running processes."""
    hSnap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if hSnap == -1 or hSnap == 0:
        return []
    pe = _PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    procs = []
    if _kernel32.Process32FirstW(hSnap, ctypes.byref(pe)):
        while True:
            procs.append({"pid": pe.th32ProcessID, "name": pe.szExeFile})
            if not _kernel32.Process32NextW(hSnap, ctypes.byref(pe)):
                break
    _kernel32.CloseHandle(hSnap)
    return procs


def _win_process_creation_time(pid):
    """Return process creation time as integer (FILETIME 64-bit), or 0 if inaccessible."""
    hProc = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hProc:
        hProc = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if hProc:
        ft_create = wintypes.FILETIME()
        ft_exit = wintypes.FILETIME()
        ft_kernel = wintypes.FILETIME()
        ft_user = wintypes.FILETIME()
        if _kernel32.GetProcessTimes(
            hProc, ctypes.byref(ft_create), ctypes.byref(ft_exit),
            ctypes.byref(ft_kernel), ctypes.byref(ft_user)
        ):
            _kernel32.CloseHandle(hProc)
            return (ft_create.dwHighDateTime << 32) | ft_create.dwLowDateTime
        _kernel32.CloseHandle(hProc)
    return 0


def _win_visible_windows():
    """Return list of dicts: [{'hwnd': int, 'pid': int, 'title': str}] for visible titled windows."""
    windows = []
    h_winsta = _user32.GetProcessWindowStation()
    desktop_names = []

    def desk_cb(lpszDesktop, lparam):
        desktop_names.append(lpszDesktop)
        return 1

    _user32.EnumDesktopsW(h_winsta, _DESKTOPENUMPROC(desk_cb), 0)
    if not desktop_names:
        desktop_names = ["Default"]

    def make_cb(desk_windows):
        def wnd_cb(hwnd, lparam):
            if not _user32.IsWindowVisible(hwnd):
                return 1
            length = _user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return 1
            buff = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value.strip()
            if not title:
                return 1
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            desk_windows.append({"hwnd": hwnd, "pid": pid.value, "title": title})
            return 1
        return _WNDENUMPROC(wnd_cb)

    for dname in desktop_names:
        h_desk = _user32.OpenDesktopW(dname, 0, False, 0x01FF)
        if h_desk:
            desk_windows = []
            cb = make_cb(desk_windows)
            _user32.EnumDesktopWindows(h_desk, cb, 0)
            _user32.CloseDesktop(h_desk)
            windows.extend(desk_windows)

    cb = make_cb(windows)
    _user32.EnumWindows(cb, 0)

    seen = set()
    dedup = []
    for w in windows:
        if w["hwnd"] not in seen:
            seen.add(w["hwnd"])
            dedup.append(w)
    return dedup


def _win_match_procs(app):
    """Find processes matching app name (case-insensitive, with/without .exe)."""
    target = app.lower()
    if target.endswith(".exe"):
        target = target[:-4]
    matches = []
    for p in _win_all_processes():
        pname = p["name"].lower()
        pbase = pname[:-4] if pname.endswith(".exe") else pname
        if (
            pbase == target
            or pbase.startswith(f"{target}64")
            or pbase.startswith(f"{target}32")
            or pbase.startswith(f"{target}-")
            or pbase.startswith(f"{target}_")
            or (len(target) >= 3 and pbase.startswith(target))
        ):
            matches.append(p)
    return matches


class _WinProcess:
    def __init__(self, pid, hProcess, hThread):
        self.pid = pid
        self.hProcess = hProcess
        self.hThread = hThread
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        code = wintypes.DWORD()
        if _kernel32.GetExitCodeProcess(self.hProcess, ctypes.byref(code)):
            if code.value != 259:  # STILL_ACTIVE
                self.returncode = code.value
                return self.returncode
        return None

    def close(self):
        if self.hProcess:
            _kernel32.CloseHandle(self.hProcess)
            self.hProcess = None
        if self.hThread:
            _kernel32.CloseHandle(self.hThread)
            self.hThread = None


def _win_spawn(cmd_list):
    """Spawn a process on the interactive desktop using CreateProcessW, with subprocess fallback."""
    binary = shutil.which(cmd_list[0]) or shutil.which(f"{cmd_list[0]}.exe") or cmd_list[0]
    full_cmd = [binary] + cmd_list[1:]
    cmd_str = subprocess.list2cmdline(full_cmd)

    hdesk = _user32.OpenInputDesktop(0, False, 0x01FF)
    desk_name = "WinSta0\\Default"
    if hdesk:
        buf = ctypes.create_unicode_buffer(256)
        if _user32.GetUserObjectInformationW(hdesk, 2, buf, 512, None) and buf.value:
            desk_name = f"WinSta0\\{buf.value}"
        _user32.CloseDesktop(hdesk)

    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(_STARTUPINFOW)
    si.lpDesktop = desk_name
    pi = _PROCESS_INFORMATION()

    if _kernel32.CreateProcessW(None, cmd_str, None, None, False, 0, None, None, ctypes.byref(si), ctypes.byref(pi)):
        return _WinProcess(pi.dwProcessId, pi.hProcess, pi.hThread)

    return subprocess.Popen(
        full_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def win_open(app, args):
    _win_attach_desktop()
    initial_windows = {w["hwnd"] for w in _win_visible_windows()}
    initial_pids = {p["pid"] for p in _win_match_procs(app)}

    try:
        proc = _win_spawn([app] + args)
    except OSError as e:
        return fail("open", f"could not start '{app}'", detail=str(e))

    window_title = ""
    target_pid = proc.pid
    alive = True

    for _ in range(30):
        time.sleep(0.1)

        # 1. Direct PID window match
        for w in _win_visible_windows():
            if w["pid"] == proc.pid:
                window_title = w["title"]
                target_pid = proc.pid
                break
        if window_title:
            break

        # 2. Check for newly appeared window matching app or process
        curr_windows = _win_visible_windows()
        new_windows = [w for w in curr_windows if w["hwnd"] not in initial_windows]
        app_procs = _win_match_procs(app)
        app_pids = {p["pid"] for p in app_procs}
        app_lower = (app[:-4] if app.lower().endswith(".exe") else app).lower()

        for w in new_windows:
            if w["pid"] in app_pids or app_lower in w["title"].lower():
                window_title = w["title"]
                target_pid = w["pid"]
                break
        if window_title:
            break

        # 3. Check process exit
        poll_code = proc.poll()
        if poll_code is not None:
            if poll_code != 0:
                alive = False
                break

    if hasattr(proc, "close"):
        proc.close()

    if not alive:
        return fail("open", f"'{app}' exited immediately (code {proc.poll()})",
                    evidence={"pid": proc.pid})

    if not window_title and proc.poll() == 0:
        current_matches = _win_match_procs(app)
        new_pids = [m["pid"] for m in current_matches if m["pid"] not in initial_pids]
        if new_pids:
            target_pid = new_pids[0]
        elif current_matches:
            target_pid = current_matches[0]["pid"]
        else:
            return fail("open", f"'{app}' exited immediately (code 0)",
                        evidence={"pid": proc.pid})

    evidence = {"pid": target_pid, "verified": bool(window_title)}
    if window_title:
        evidence["window"] = window_title
    return emit({"ok": True, "action": "open", "app": app, "evidence": evidence})


def win_status(app):
    _win_attach_desktop()
    if app.isdigit():
        all_procs = {p["pid"]: p["name"] for p in _win_all_processes()}
        procs = [{"pid": int(app), "name": all_procs.get(int(app), "")}] if int(app) in all_procs else []
    else:
        procs = _win_match_procs(app)

    windows = _win_visible_windows()

    if not procs and not app.isdigit():
        app_lower = (app[:-4] if app.lower().endswith(".exe") else app).lower()
        matching_hwnds = [w for w in windows if app_lower in w["title"].lower()]
        if matching_hwnds:
            all_p = {p["pid"]: p["name"] for p in _win_all_processes()}
            procs = [{"pid": w["pid"], "name": all_p.get(w["pid"], app)} for w in matching_hwnds]

    if not procs:
        return emit({"ok": True, "action": "status", "app": app,
                     "evidence": {"running": False}})

    w_map = {}
    for w in windows:
        if w["pid"] not in w_map:
            w_map[w["pid"]] = w["title"]

    app_lower = (app[:-4] if app.lower().endswith(".exe") else app).lower()
    if not any(w_map.get(p["pid"]) for p in procs):
        for w in windows:
            if app_lower in w["title"].lower():
                w_map[procs[0]["pid"]] = w["title"]
                break

    proc_list = [
        {"pid": p["pid"], "name": p["name"], "window": w_map.get(p["pid"], "")}
        for p in procs
    ]
    return emit({"ok": True, "action": "status", "app": app, "evidence": {
        "running": True, "processes": proc_list}})


def win_list():
    windows = _win_visible_windows()
    all_procs = {p["pid"]: p["name"] for p in _win_all_processes()}
    results = []
    seen = set()
    for w in windows:
        pid = w["pid"]
        if pid in seen:
            continue
        seen.add(pid)
        results.append({
            "pid": pid,
            "name": all_procs.get(pid, ""),
            "window": w["title"],
        })
    emit({"ok": True, "action": "list", "evidence": {
        "count": len(results),
        "processes": results}})


def _win_resolve_target(app):
    """Find visible window matching app name, title substring, or PID (newest first)."""
    windows = _win_visible_windows()
    candidates = []
    if app.isdigit():
        candidates = [w for w in windows if w["pid"] == int(app)]
    else:
        app_lower = (app[:-4] if app.lower().endswith(".exe") else app).lower()
        for w in windows:
            if app_lower in w["title"].lower():
                candidates.append(w)
        if not candidates:
            match_pids = {p["pid"] for p in _win_match_procs(app)}
            candidates = [w for w in windows if w["pid"] in match_pids]

    if not candidates:
        return None

    def _win_area(w):
        r = RECT()
        if _user32.GetWindowRect(w["hwnd"], ctypes.byref(r)):
            return max(0, r.right - r.left) * max(0, r.bottom - r.top)
        return 0

    candidates.sort(key=lambda w: (_win_process_creation_time(w["pid"]), _win_area(w)), reverse=True)
    return candidates[0]


def _win_focus_window(target_hwnd):
    """Attach to desktop, restore, bypass foreground-lock, and poll for foreground verification."""
    _win_attach_desktop()
    if _user32.IsIconic(target_hwnd):
        _user32.ShowWindow(target_hwnd, SW_RESTORE)
    else:
        _user32.ShowWindow(target_hwnd, SW_SHOW)

    fore_wnd = _user32.GetForegroundWindow()
    fore_tid = _user32.GetWindowThreadProcessId(fore_wnd, None)
    target_tid = _user32.GetWindowThreadProcessId(target_hwnd, None)
    cur_tid = _kernel32.GetCurrentThreadId()

    if fore_tid and fore_tid != cur_tid:
        _user32.AttachThreadInput(cur_tid, fore_tid, True)
    if target_tid and target_tid != cur_tid:
        _user32.AttachThreadInput(cur_tid, target_tid, True)

    if fore_wnd != target_hwnd and _user32.GetAncestor(fore_wnd, 2) != target_hwnd:
        _user32.LockSetForegroundWindow(2)  # LSFW_UNLOCK
        _user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
        _user32.keybd_event(0x12, 0, 2, 0)  # VK_MENU up
        _user32.SetWindowPos(target_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        _user32.SetWindowPos(target_hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        _user32.BringWindowToTop(target_hwnd)
        _user32.SetForegroundWindow(target_hwnd)
        time.sleep(0.02)
        _user32.keybd_event(0x1B, 0, 0, 0)  # VK_ESCAPE down
        _user32.keybd_event(0x1B, 0, 2, 0)  # VK_ESCAPE up
    else:
        _user32.BringWindowToTop(target_hwnd)
        _user32.SetForegroundWindow(target_hwnd)

    if fore_tid and fore_tid != cur_tid:
        _user32.AttachThreadInput(cur_tid, fore_tid, False)
    if target_tid and target_tid != cur_tid:
        _user32.AttachThreadInput(cur_tid, target_tid, False)

    for _ in range(15):
        time.sleep(0.1)
        fg = _user32.GetForegroundWindow()
        if fg == target_hwnd or _user32.GetAncestor(fg, 2) == target_hwnd:
            return True, fg
    return False, _user32.GetForegroundWindow()


def win_focus(app):
    target = _win_resolve_target(app)
    if not target:
        return fail("focus", f"no visible window for '{app}'")

    target_hwnd = target["hwnd"]
    target_pid = target["pid"]
    target_title = target["title"]

    ok, actual = _win_focus_window(target_hwnd)
    if ok:
        return emit({
            "ok": True,
            "action": "focus",
            "app": app,
            "evidence": {
                "foreground": True,
                "verified": True,
                "pid": target_pid,
                "window": target_title,
            }
        })

    return fail(
        "focus",
        f"window did not come to foreground for '{app}'",
        evidence={
            "foreground": False,
            "expected_hwnd": target_hwnd,
            "actual_hwnd": actual,
            "pid": target_pid,
            "window": target_title,
        },
    )


def _win_see_raw(target, output_path=None):
    target_hwnd = target["hwnd"]
    target_pid = target["pid"]
    target_title = target["title"]

    rect = RECT()
    if _dwmapi.DwmGetWindowAttribute(target_hwnd, 9, ctypes.byref(rect), ctypes.sizeof(RECT)) != 0:
        _user32.GetWindowRect(target_hwnd, ctypes.byref(rect))

    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid window geometry ({width}x{height}) for '{target_title}'")

    hdc_screen = _user32.GetDC(0)
    hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
    hbm = _gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    _gdi32.SelectObject(hdc_mem, hbm)

    captured = _user32.PrintWindow(target_hwnd, hdc_mem, 2)
    if not captured:
        _gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, rect.left, rect.top, 0x00CC0020 | 0x40000000)

    bih = BITMAPINFOHEADER()
    bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bih.biWidth = width
    bih.biHeight = -height
    bih.biPlanes = 1
    bih.biBitCount = 32
    bih.biCompression = 0

    buf = ctypes.create_string_buffer(width * height * 4)
    _gdi32.GetDIBits(hdc_mem, hbm, 0, height, buf, ctypes.byref(bih), 0)

    _gdi32.DeleteObject(hbm)
    _gdi32.DeleteDC(hdc_mem)
    _user32.ReleaseDC(0, hdc_screen)

    raw_bgra = memoryview(buf)
    rgb_data = bytearray(width * height * 3)
    rgb_data[0::3] = raw_bgra[2::4]
    rgb_data[1::3] = raw_bgra[1::4]
    rgb_data[2::3] = raw_bgra[0::4]

    png_bytes = encode_png_rgb(width, height, rgb_data)

    if not output_path:
        clean_app = "".join(c for c in target_title if c.isalnum() or c in ("-", "_")) or "target"
        out_dir = os.path.join(os.environ.get("TEMP", "."), "appctl_see")
        os.makedirs(out_dir, exist_ok=True)
        output_path = os.path.join(out_dir, f"{clean_app}_{int(time.time() * 1000)}.png")

    with open(output_path, "wb") as f:
        f.write(png_bytes)

    return {
        "path": os.path.abspath(output_path),
        "geometry": {"x": rect.left, "y": rect.top, "w": width, "h": height},
        "pid": target_pid,
        "hwnd": target_hwnd,
        "window": target_title,
        "size_bytes": len(png_bytes),
    }


def win_see(app, output_path=None):
    _win_attach_desktop()
    target = _win_resolve_target(app)
    if not target:
        return fail("see", f"no visible window for '{app}'")

    try:
        ev = _win_see_raw(target, output_path)
    except ValueError as e:
        return fail("see", str(e))

    return emit({
        "ok": True,
        "action": "see",
        "app": app,
        "evidence": ev
    })


def _win_type_raw(target, text):
    fore = _user32.GetForegroundWindow()
    if fore != target["hwnd"] and _user32.GetAncestor(fore, 2) != target["hwnd"]:
        _win_focus_window(target["hwnd"])
        time.sleep(0.05)

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD = 1

    chars_sent = 0
    for char in text:
        if char == "\r":
            continue
        if char == "\n":
            inputs = (INPUT * 2)()
            inputs[0].type = INPUT_KEYBOARD
            inputs[0].u.ki.wVk = 0x0D
            inputs[1].type = INPUT_KEYBOARD
            inputs[1].u.ki.wVk = 0x0D
            inputs[1].u.ki.dwFlags = KEYEVENTF_KEYUP
            _user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
        else:
            code = ord(char)
            inputs = (INPUT * 2)()
            inputs[0].type = INPUT_KEYBOARD
            inputs[0].u.ki.wVk = 0
            inputs[0].u.ki.wScan = code
            inputs[0].u.ki.dwFlags = KEYEVENTF_UNICODE
            inputs[1].type = INPUT_KEYBOARD
            inputs[1].u.ki.wVk = 0
            inputs[1].u.ki.wScan = code
            inputs[1].u.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            _user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
        chars_sent += 1
        time.sleep(0.01)
    return chars_sent


def win_type(app, text):
    _win_attach_desktop()
    target = _win_resolve_target(app)
    if not target:
        return fail("type", f"no visible window for '{app}'")

    chars_sent = _win_type_raw(target, text)
    return emit({
        "ok": True,
        "action": "type",
        "app": app,
        "evidence": {
            "typed": True,
            "chars": chars_sent,
            "pid": target["pid"],
            "window": target["title"],
        }
    })


def _win_key_raw(target, key_name):
    fore = _user32.GetForegroundWindow()
    if fore != target["hwnd"] and _user32.GetAncestor(fore, 2) != target["hwnd"]:
        _win_focus_window(target["hwnd"])
        time.sleep(0.05)

    VK_MAP = {
        "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
        "backspace": 0x08, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25,
        "right": 0x27, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
        "alt": 0x12, "menu": 0x12, "ctrl": 0x11, "control": 0x11, "shift": 0x10,
        **{f"f{i}": 0x6F + i for i in range(1, 13)}
    }

    key_lower = key_name.lower()
    ctrl = False
    shift = False
    alt = False

    parts = key_lower.split("+")
    actual_key = parts[-1]
    for m in parts[:-1]:
        if m in ("ctrl", "control"):
            ctrl = True
        elif m == "shift":
            shift = True
        elif m == "alt":
            alt = True

    vk = VK_MAP.get(actual_key)
    if not vk:
        if len(actual_key) == 1:
            vk = ord(actual_key.upper())
        else:
            raise ValueError(f"unsupported key: '{key_name}'")

    def key_event(v, up=False):
        flags = 0x0002 if up else 0
        scan = 0 if v in (0x10, 0x11, 0x12) else _user32.MapVirtualKeyW(v, 0)
        _user32.keybd_event(v, scan, flags, 0)

    if ctrl:
        key_event(0x11, False)
        time.sleep(0.03)
    if shift:
        key_event(0x10, False)
        time.sleep(0.03)
    if alt:
        key_event(0x12, False)
        time.sleep(0.03)

    key_event(vk, False)
    time.sleep(0.05)
    key_event(vk, True)

    if alt:
        time.sleep(0.03)
        key_event(0x12, True)
    if shift:
        time.sleep(0.03)
        key_event(0x10, True)
    if ctrl:
        time.sleep(0.03)
        key_event(0x11, True)


def win_key(app, key_name):
    _win_attach_desktop()
    target = _win_resolve_target(app)
    if not target:
        return fail("key", f"no visible window for '{app}'")

    try:
        _win_key_raw(target, key_name)
    except ValueError as e:
        return fail("key", str(e))

    return emit({
        "ok": True,
        "action": "key",
        "app": app,
        "evidence": {
            "sent": True,
            "key": key_name,
            "pid": target["pid"],
            "window": target["title"],
        }
    })


def _win_click_raw(target, x, y, button="left"):
    fore = _user32.GetForegroundWindow()
    if fore != target["hwnd"] and _user32.GetAncestor(fore, 2) != target["hwnd"]:
        _win_focus_window(target["hwnd"])
        time.sleep(0.05)

    rect = RECT()
    if _dwmapi.DwmGetWindowAttribute(target["hwnd"], 9, ctypes.byref(rect), ctypes.sizeof(RECT)) != 0:
        _user32.GetWindowRect(target["hwnd"], ctypes.byref(rect))

    screen_x = rect.left + int(x)
    screen_y = rect.top + int(y)

    _user32.SetCursorPos(screen_x, screen_y)
    time.sleep(0.02)

    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010

    if button == "left":
        _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    elif button == "right":
        _user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        _user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
    elif button == "double":
        _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.05)
        _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def win_click(app, x, y, button="left"):
    _win_attach_desktop()
    target = _win_resolve_target(app)
    if not target:
        return fail("click", f"no visible window for '{app}'")

    try:
        _win_click_raw(target, x, y, button)
    except Exception as e:
        return fail("click", str(e))

    return emit({
        "ok": True,
        "action": "click",
        "app": app,
        "evidence": {
            "clicked": True,
            "x": int(x),
            "y": int(y),
            "button": button,
            "pid": target["pid"],
            "window": target["title"],
        }
    })


def win_quit(app, force):
    _win_attach_desktop()
    if app.isdigit():
        pids = [int(app)]
        procs = [p for p in _win_all_processes() if p["pid"] == int(app)]
    else:
        app_lower = (app[:-4] if app.lower().endswith(".exe") else app).lower()
        windows = _win_visible_windows()
        title_windows = [w for w in windows if app_lower in w["title"].lower()]
        if title_windows:
            pids = list({w["pid"] for w in title_windows})
            all_procs = {p["pid"]: p["name"] for p in _win_all_processes()}
            procs = [{"pid": pid, "name": all_procs.get(pid, app)} for pid in pids]
        else:
            procs = _win_match_procs(app)
            pids = [p["pid"] for p in procs]

    if not procs:
        return emit({"ok": True, "action": "quit", "app": app,
                     "evidence": {"running": False, "method": "already-closed"}})

    if not force:
        windows = _win_visible_windows()
        target_hwnds = [w["hwnd"] for w in windows if w["pid"] in pids]
        for hwnd in target_hwnds:
            _user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

        for _ in range(15):
            time.sleep(0.2)
            alive = [p for p in _win_all_processes() if p["pid"] in pids]
            if not alive:
                return emit({"ok": True, "action": "quit", "app": app,
                             "evidence": {"running": False, "method": "graceful"}})

        alive = [p for p in _win_all_processes() if p["pid"] in pids]
        return emit({"ok": True, "action": "quit", "app": app,
                     "evidence": {"running": bool(alive), "method": "graceful"}})

    for pid in pids:
        hProc = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if hProc:
            _kernel32.TerminateProcess(hProc, 1)
            _kernel32.CloseHandle(hProc)

    time.sleep(0.2)
    still_alive = [p for p in _win_all_processes() if p["pid"] in pids]
    return emit({"ok": True, "action": "quit", "app": app,
                 "evidence": {"running": bool(still_alive), "method": "force"}})


# ----------------------------------------------------------- PNG & Diff Engine -
def _png_chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_png_rgb(width, height, rgb_bytes):
    """Encode raw RGB bytes (width * height * 3) into PNG format."""
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    raw_lines = bytearray()
    stride = width * 3
    for y in range(height):
        raw_lines.append(0)  # filter None
        raw_lines.extend(rgb_bytes[y * stride : (y + 1) * stride])
    idat = _png_chunk(b"IDAT", zlib.compress(bytes(raw_lines), level=6))
    iend = _png_chunk(b"IEND", b"")
    return header + ihdr + idat + iend


def decode_png(png_bytes):
    """Decode PNG bytes into (width, height, rgb_bytes). Returns RGB 3-bytes-per-pixel."""
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Invalid PNG signature")
    idx = 8
    idat_parts = []
    width = height = color_type = bit_depth = None
    while idx < len(png_bytes):
        length = struct.unpack(">I", png_bytes[idx : idx + 4])[0]
        tag = png_bytes[idx + 4 : idx + 8]
        data = png_bytes[idx + 8 : idx + 8 + length]
        idx += 12 + length
        if tag == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", data[:10])
        elif tag == b"IDAT":
            idat_parts.append(data)
        elif tag == b"IEND":
            break

    if not width or not height or bit_depth != 8:
        raise ValueError("Unsupported PNG format (expected 8-bit depth)")

    channels = 4 if color_type == 6 else (3 if color_type == 2 else (1 if color_type == 0 else 3))
    raw = zlib.decompress(b"".join(idat_parts))
    stride = 1 + width * channels
    pixels = bytearray(width * height * channels)
    prev_line = bytearray(width * channels)

    def _paeth(a, b, c):
        p = a + b - c
        pa = abs(p - a)
        pb = abs(p - b)
        pc = abs(p - c)
        if pa <= pb and pa <= pc:
            return a
        elif pb <= pc:
            return b
        return c

    for y in range(height):
        line_start = y * stride
        filt = raw[line_start]
        line_data = raw[line_start + 1 : line_start + stride]
        curr_line = bytearray(width * channels)
        for x in range(width * channels):
            val = line_data[x]
            a = curr_line[x - channels] if x >= channels else 0
            b = prev_line[x]
            c = prev_line[x - channels] if x >= channels else 0
            if filt == 0:
                res = val
            elif filt == 1:
                res = (val + a) & 0xFF
            elif filt == 2:
                res = (val + b) & 0xFF
            elif filt == 3:
                res = (val + ((a + b) // 2)) & 0xFF
            elif filt == 4:
                res = (val + _paeth(a, b, c)) & 0xFF
            else:
                res = val
            curr_line[x] = res
        pixels[y * width * channels : (y + 1) * width * channels] = curr_line
        prev_line = curr_line

    if channels == 4:
        rgb = bytearray(width * height * 3)
        mv = memoryview(pixels)
        rgb[0::3] = mv[0::4]
        rgb[1::3] = mv[1::4]
        rgb[2::3] = mv[2::4]
        return width, height, bytes(rgb)
    elif channels == 1:
        rgb = bytearray(width * height * 3)
        mv = memoryview(pixels)
        rgb[0::3] = mv
        rgb[1::3] = mv
        rgb[2::3] = mv
        return width, height, bytes(rgb)
    return width, height, bytes(pixels)


def compute_diff(before_path, after_path, region_str=None, mask_str=None, threshold=0.001):
    """Compare two PNG images for pixel changes with optional region & mask. Returns dict result."""
    if not os.path.exists(before_path):
        raise FileNotFoundError(f"before image not found: '{before_path}'")
    if not os.path.exists(after_path):
        raise FileNotFoundError(f"after image not found: '{after_path}'")

    with open(before_path, "rb") as f:
        b_data = f.read()
    with open(after_path, "rb") as f:
        a_data = f.read()
    w1, h1, rgb1 = decode_png(b_data)
    w2, h2, rgb2 = decode_png(a_data)

    if (w1, h1) != (w2, h2):
        return {
            "ok": True,
            "action": "diff",
            "evidence": {
                "changed": True,
                "geometry_changed": True,
                "before": {"w": w1, "h": h1},
                "after": {"w": w2, "h": h2},
                "verdict": "pass",
            }
        }

    def parse_rect(s):
        if not s:
            return None
        parts = [int(p.strip()) for p in s.split(",")]
        if len(parts) != 4:
            raise ValueError("Expected x,y,w,h (4 integers)")
        return parts

    region = parse_rect(region_str)
    mask = parse_rect(mask_str)

    rx, ry, rw, rh = region if region else (0, 0, w1, h1)
    rx = max(0, min(rx, w1 - 1))
    ry = max(0, min(ry, h1 - 1))
    rw = max(1, min(rw, w1 - rx))
    rh = max(1, min(rh, h1 - ry))

    mx, my, mw, mh = mask if mask else (-1, -1, 0, 0)

    changed_pixels = 0
    total_evaluated = 0
    min_x, max_x = w1, -1
    min_y, max_y = h1, -1
    color_tolerance = 15

    for y in range(ry, ry + rh):
        stride = y * w1 * 3
        is_mask_y = (my <= y < my + mh) if mask else False
        for x in range(rx, rx + rw):
            if is_mask_y and (mx <= x < mx + mw):
                continue
            total_evaluated += 1
            px = stride + x * 3
            diff = (
                abs(rgb1[px] - rgb2[px])
                + abs(rgb1[px + 1] - rgb2[px + 1])
                + abs(rgb1[px + 2] - rgb2[px + 2])
            )
            if diff > color_tolerance:
                changed_pixels += 1
                if x < min_x: min_x = x
                if x > max_x: max_x = x
                if y < min_y: min_y = y
                if y > max_y: max_y = y

    diff_ratio = (changed_pixels / total_evaluated) if total_evaluated > 0 else 0.0
    is_changed = diff_ratio >= threshold

    bbox = None
    if changed_pixels > 0:
        bbox = {
            "x": min_x,
            "y": min_y,
            "w": max_x - min_x + 1,
            "h": max_y - min_y + 1,
        }

    return {
        "ok": True,
        "action": "diff",
        "evidence": {
            "changed": is_changed,
            "difference": round(diff_ratio, 6),
            "changed_pixels": changed_pixels,
            "total_evaluated_pixels": total_evaluated,
            "bounding_box": bbox,
            "threshold": threshold,
            "verdict": "pass" if is_changed else "fail",
        }
    }


def run_diff(before_path, after_path, region_str=None, mask_str=None, threshold=0.001):
    try:
        res = compute_diff(before_path, after_path, region_str, mask_str, threshold)
        return emit(res)
    except Exception as e:
        return fail("diff", str(e))


# ------------------------------------------------------------------ macOS ----
def mac_open(app, args):
    cmd = ["open", "-a", app] + (["--args"] + args if args else [])
    rc, out = run(cmd)
    if rc != 0:
        return fail("open", f"could not start '{app}'", detail=out)
    return emit({"ok": True, "action": "open", "app": app, "evidence": {"launched": True}})


def mac_status(app):
    rc, out = run(["pgrep", "-fl", app])
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and "appctl" not in parts[1]:
            procs.append({"pid": int(parts[0]), "cmd": parts[1]})
    return emit({"ok": True, "action": "status", "app": app, "evidence": {
        "running": bool(procs), "processes": procs}})


def mac_quit(app, force):
    if not force:
        rc, _ = run(["osascript", "-e", f'quit app "{app}"'])
        if rc == 0:
            return emit({"ok": True, "action": "quit", "app": app,
                         "evidence": {"running": False, "method": "graceful"}})
    rc, out = run(["pkill", "-x", app] if not force else ["pkill", "-9", "-x", app])
    return emit({"ok": True, "action": "quit", "app": app,
                 "evidence": {"running": False, "method": "pkill"}})


def mac_focus(app):
    rc, out = run(["osascript", "-e", f'tell application "{app}" to activate'])
    if rc != 0:
        return fail("focus", f"could not activate '{app}'", detail=out)
    return emit({"ok": True, "action": "focus", "app": app,
                 "evidence": {"foreground": True}})


def mac_see(app, output_path=None):
    import tempfile
    output_path = output_path or os.path.join(tempfile.gettempdir(), f"see_{int(time.time()*1000)}.png")
    rc, out = run(["screencapture", "-x", output_path])
    if rc != 0:
        return fail("see", f"could not capture screenshot for '{app}'", detail=out)
    return emit({"ok": True, "action": "see", "app": app, "evidence": {"path": output_path}})


def mac_type(app, text):
    escaped = text.replace('"', '\\"')
    rc, out = run(["osascript", "-e", f'tell application "{app}" to activate',
                   "-e", f'tell application "System Events" to keystroke "{escaped}"'])
    if rc != 0:
        return fail("type", f"could not type into '{app}'", detail=out)
    return emit({"ok": True, "action": "type", "app": app, "evidence": {"typed": True, "chars": len(text)}})


def mac_key(app, key_name):
    rc, out = run(["osascript", "-e", f'tell application "{app}" to activate',
                   "-e", f'tell application "System Events" to keystroke "{key_name}"'])
    return emit({"ok": True, "action": "key", "app": app, "evidence": {"sent": True, "key": key_name}})


def mac_click(app, x, y, button="left"):
    return fail("click", "click action not yet implemented for macOS")


# ------------------------------------------------------------------ Linux ----
def lin_open(app, args):
    binary = shutil.which(app) or app
    try:
        p = subprocess.Popen([binary] + args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        return emit({"ok": True, "action": "open", "app": app,
                     "evidence": {"pid": p.pid}})
    except OSError as e:
        return fail("open", f"could not start '{app}'", detail=str(e))


def lin_status(app):
    rc, out = run(["pgrep", "-af", app])
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and "appctl" not in parts[1]:
            procs.append({"pid": int(parts[0]), "cmd": parts[1]})
    return emit({"ok": True, "action": "status", "app": app, "evidence": {
        "running": bool(procs), "processes": procs}})


def _live_pids(app):
    """PIDs matching app, excluding PID 1, ourselves, and anything appctl-related."""
    rc, out = run(["pgrep", "-f", app])
    me = os.getpid()
    live = []
    for x in out.split():
        if not x.isdigit():
            continue
        pid = int(x)
        if pid in (1, me):
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"appctl" not in f.read():
                    live.append(pid)
        except OSError:
            pass
    return live


def lin_quit(app, force):
    import signal
    targets = _live_pids(app)
    sig = signal.SIGKILL if force else signal.SIGTERM
    for pid in targets:
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    for _ in range(6):
        time.sleep(0.5)
        if not _live_pids(app):
            break
    still = _live_pids(app)
    return emit({"ok": True, "action": "quit", "app": app, "evidence": {
        "running": bool(still),
        "method": "SIGKILL" if force else "SIGTERM",
        "signalled": len(targets)}})


def lin_focus(app):
    if shutil.which("wmctrl"):
        rc, out = run(["wmctrl", "-a", app])
        if rc == 0:
            return emit({"ok": True, "action": "focus", "app": app,
                         "evidence": {"foreground": True, "via": "wmctrl"}})
    if shutil.which("xdotool"):
        rc, out = run(["xdotool", "search", "--name", app, "windowactivate"])
        if rc == 0:
            return emit({"ok": True, "action": "focus", "app": app,
                         "evidence": {"foreground": True, "via": "xdotool"}})
    return fail("focus", "no window manager tool (install wmctrl or xdotool)")


def lin_see(app, output_path=None):
    import tempfile
    output_path = output_path or os.path.join(tempfile.gettempdir(), f"see_{int(time.time()*1000)}.png")
    if shutil.which("grim"):
        rc, out = run(["grim", output_path])
    elif shutil.which("import"):
        rc, out = run(["import", "-window", "root", output_path])
    elif shutil.which("scrot"):
        rc, out = run(["scrot", output_path])
    else:
        return fail("see", "no screenshot utility found (install grim, scrot, or imagemagick)")
    if rc != 0:
        return fail("see", f"could not capture screenshot for '{app}'", detail=out)
    return emit({"ok": True, "action": "see", "app": app, "evidence": {"path": output_path}})


def lin_type(app, text):
    if shutil.which("xdotool"):
        rc, out = run(["xdotool", "type", "--", text])
        if rc == 0:
            return emit({"ok": True, "action": "type", "app": app, "evidence": {"typed": True, "chars": len(text)}})
    return fail("type", "no input tool (install xdotool)")


def lin_key(app, key_name):
    if shutil.which("xdotool"):
        rc, out = run(["xdotool", "key", key_name])
        if rc == 0:
            return emit({"ok": True, "action": "key", "app": app, "evidence": {"sent": True, "key": key_name}})
    return fail("key", "no input tool (install xdotool)")


def lin_click(app, x, y, button="left"):
    if shutil.which("xdotool"):
        btn = "1" if button == "left" else ("3" if button == "right" else "1 --repeat 2")
        rc, out = run(["xdotool", "mousemove", str(x), str(y), "click", btn])
        if rc == 0:
            return emit({"ok": True, "action": "click", "app": app, "evidence": {"clicked": True, "x": int(x), "y": int(y), "button": button}})
    return fail("click", "no input tool (install xdotool)")


# ------------------------------------------------------------------ dispatch -
HANDLERS = {
    "Windows": {
        "open": win_open, "focus": win_focus, "status": win_status,
        "list": lambda: win_list(), "quit": win_quit,
        "see": win_see, "type": win_type, "key": win_key, "click": win_click,
        "resolve": _win_resolve_target, "type_raw": _win_type_raw, "key_raw": _win_key_raw,
        "click_raw": _win_click_raw, "focus_raw": _win_focus_window, "see_raw": _win_see_raw,
    },
    "Darwin": {
        "open": mac_open, "focus": mac_focus, "status": mac_status,
        "list": lambda: mac_status(""), "quit": mac_quit,
        "see": mac_see, "type": mac_type, "key": mac_key, "click": mac_click,
    },
    "Linux": {
        "open": lin_open, "focus": lin_focus, "status": lin_status,
        "list": lambda: lin_status(""), "quit": lin_quit,
        "see": lin_see, "type": lin_type, "key": lin_key, "click": lin_click,
    },
}


def run_tools(app_filter=None):
    if not registry:
        return fail("tools", "registry module not available")
    tools = registry.list_tools(app_filter)
    return emit({
        "ok": True,
        "action": "tools",
        "filter": app_filter,
        "count": len(tools),
        "tools": tools,
    })


def run_schema(tool_id):
    if not registry:
        return fail("schema", "registry module not available")
    tool = registry.get(tool_id)
    if not tool:
        return fail("schema", f"unregistered tool: '{tool_id}'")
    return emit({
        "ok": True,
        "action": "schema",
        "tool_id": tool_id,
        "tool": tool,
    })


def run_exec(tool_id, args_json_str=None):
    if not registry:
        return fail("exec", "registry module not available")

    args = {}
    if args_json_str:
        try:
            args = json.loads(args_json_str)
        except Exception as e:
            return fail("exec", f"invalid JSON in --args-json: {e}")

    try:
        tool, validated_args = registry.validate_and_prepare(tool_id, args)
    except KeyError:
        return fail("exec", f"unregistered tool: '{tool_id}'")
    except SchemaValidationError as e:
        return fail("exec", f"schema validation failed for '{tool_id}': {e}")

    h = HANDLERS.get(OS)
    if not h:
        return fail("exec", f"unsupported OS: {OS}")

    action = tool.get("action")
    target_app = validated_args.get("app")

    if action == "open":
        return h["open"](validated_args["app"], validated_args.get("args", []))
    elif action == "focus":
        return h["focus"](validated_args["app"])
    elif action == "see":
        return h["see"](validated_args["app"], validated_args.get("output"))
    elif action == "diff":
        return run_diff(
            validated_args["before"],
            validated_args["after"],
            validated_args.get("region"),
            validated_args.get("mask"),
            validated_args.get("threshold", 0.001),
        )
    elif action == "type":
        return h["type"](validated_args["app"], validated_args["text"])
    elif action == "key":
        key_val = validated_args.get("key") or tool.get("params", {}).get("key")
        return h["key"](validated_args["app"], key_val)
    elif action == "click":
        return h["click"](validated_args["app"], validated_args["x"], validated_args["y"], validated_args.get("button", "left"))
    elif action == "status":
        return h["status"](validated_args["app"])
    elif action == "list":
        return h["list"]()
    elif action == "quit":
        return h["quit"](validated_args["app"], validated_args.get("force", False))
    elif action == "node":
        if not execute_macro_node:
            return fail("exec", "macro_node module not available")
        res = execute_macro_node(validated_args, h, diff_fn=compute_diff)
        return emit(res, code=0 if res.get("ok") else 1)
    elif action == "a11y_tree":
        if not run_a11y_tree:
            return fail("exec", "a11y module not available")
        res = run_a11y_tree(
            validated_args["app"],
            depth=validated_args.get("depth", 3),
            max_children=validated_args.get("max_children", 25),
            resolve_fn=h.get("resolve"),
        )
        return emit(res, code=0 if res.get("ok") else 1)
    elif action == "a11y_query":
        if not run_a11y_query:
            return fail("exec", "a11y module not available")
        res = run_a11y_query(
            validated_args["app"],
            role=validated_args.get("role"),
            name=validated_args.get("name"),
            automation_id=validated_args.get("id"),
            resolve_fn=h.get("resolve"),
        )
        return emit(res, code=0 if res.get("ok") else 1)
    elif action == "a11y_click":
        if not run_a11y_click:
            return fail("exec", "a11y module not available")
        res = run_a11y_click(
            validated_args["app"],
            role=validated_args.get("role"),
            name=validated_args.get("name"),
            automation_id=validated_args.get("id"),
            button=validated_args.get("button", "left"),
            resolve_fn=h.get("resolve"),
            click_fn=h.get("click_raw"),
            focus_fn=h.get("focus_raw"),
        )
        return emit(res, code=0 if res.get("ok") else 1)
    elif action == "macro":
        steps = tool.get("params", {}).get("steps", [])
        if "type_raw" in h and "key_raw" in h and "resolve" in h:
            target = h["resolve"](target_app)
            if not target:
                return fail("exec", f"no visible window for '{target_app}'")
            completed = 0
            for step in steps:
                s_action = step.get("action")
                if s_action == "key":
                    h["key_raw"](target, step.get("key"))
                elif s_action == "type":
                    tpl = step.get("text_template", "")
                    text = tpl.format(**validated_args)
                    h["type_raw"](target, text)
                elif s_action == "click":
                    if "click_raw" in h:
                        h["click_raw"](target, step["x"], step["y"], step.get("button", "left"))
                time.sleep(0.05)
                completed += 1
            return emit({
                "ok": True,
                "action": "exec",
                "tool_id": tool_id,
                "evidence": {
                    "macro_steps_completed": completed,
                    "app": target_app,
                    "window": target["title"],
                }
            })
        else:
            return fail("exec", f"macro execution not supported on OS: {OS}")
    elif action == "ocr":
        if not ocr_image:
            return fail("exec", "vision module not available")
        target = validated_args["target"]
        temp_file = None
        if not os.path.isfile(target):
            temp_file = os.path.abspath(f"temp_ocr_{int(time.time()*1000)}.png")
            h["see"](target, temp_file)
            target = temp_file
        try:
            res = ocr_image(target)
            return emit(res, code=0 if res.get("ok") else 1)
        finally:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
    elif action == "vision_find":
        if not run_vision_find:
            return fail("exec", "vision module not available")
        res = run_vision_find(
            validated_args["app"],
            validated_args["query"],
            resolve_fn=h.get("resolve"),
            see_fn=lambda a, o: h["see"](a, o),
        )
        return emit(res, code=0 if res.get("ok") else 1)
    elif action == "vision_click":
        if not run_vision_click:
            return fail("exec", "vision module not available")
        res = run_vision_click(
            validated_args["app"],
            validated_args["query"],
            button=validated_args.get("button", "left"),
            resolve_fn=h.get("resolve"),
            see_fn=lambda a, o: h["see"](a, o),
            click_fn=h.get("click_raw"),
            focus_fn=h.get("focus_raw"),
        )
        return emit(res, code=0 if res.get("ok") else 1)
    elif action == "route":
        if not LocalRouter:
            return fail("exec", "router module not available")
        r = LocalRouter()
        dec = r.route(validated_args["task"], validated_args.get("context", {}))
        return emit(dec.to_dict(), code=0 if dec.ok else 1)
    elif action == "benchmark":
        if not run_benchmark:
            return fail("exec", "router module not available")
        rep = run_benchmark()
        return emit(rep, code=0 if rep.get("ok") else 1)
    elif action == "browser_fork":
        if not fork_browser:
            return fail("exec", "browser module not available")
        res = fork_browser(
            browser=validated_args.get("browser"),
            url=validated_args.get("url"),
            headless=validated_args.get("headless", False),
            port=validated_args.get("port"),
            cookie_file=validated_args.get("cookie_file"),
            copy_profile=validated_args.get("copy_profile", True),
        )
        return emit(res, code=0 if res.get("ok") else 1)
    elif action == "browser_list":
        if not list_browser_workers:
            return fail("exec", "browser module not available")
        res = list_browser_workers()
        return emit(res, code=0 if res.get("ok") else 1)
    elif action == "browser_destroy":
        if not destroy_browser_worker:
            return fail("exec", "browser module not available")
        res = destroy_browser_worker(validated_args["worker_id"])
        return emit(res, code=0 if res.get("ok") else 1)
    elif action == "browser_status":
        if not browser_status:
            return fail("exec", "browser module not available")
        res = browser_status()
        return emit(res, code=0 if res.get("ok") else 1)
    elif action == "browser_bridge":
        if not bridge_browser:
            return fail("exec", "browser module not available")
        res = bridge_browser(
            url=validated_args.get("url"),
            cookie_file=validated_args.get("cookie_file"),
            headless=validated_args.get("headless", True),
            prefer_ephemeral=validated_args.get("prefer_ephemeral", True),
            sao_url=validated_args.get("sao_url"),
        )
        return emit(res, code=0 if res.get("ok") else 1)
    elif action == "browser_url":
        if not get_unified_browser_url:
            return fail("exec", "browser module not available")
        res = get_unified_browser_url(sao_url=validated_args.get("sao_url"))
        return emit(res, code=0 if res.get("ok") else 1)
    else:
        return fail("exec", f"unknown action '{action}' for tool '{tool_id}'")



def main():
    ap = argparse.ArgumentParser(
        prog="appctl",
        description="Cross-platform app lifecycle, observation & tool registry adapter (The Architect v0.3)",
    )
    sub = ap.add_subparsers(dest="action", required=True)

    p = sub.add_parser("open", help="start an application")
    p.add_argument("app")
    p.add_argument("--args", nargs=argparse.REMAINDER, default=[], help="arguments for the app")

    p = sub.add_parser("focus", help="bring app window to foreground")
    p.add_argument("app")

    p = sub.add_parser("see", help="capture window screenshot to PNG")
    p.add_argument("app")
    p.add_argument("--output", "-o", default=None, help="path to save PNG screenshot")

    p = sub.add_parser("diff", help="compare two PNG screenshots for pixel changes")
    p.add_argument("before", help="path to before PNG")
    p.add_argument("after", help="path to after PNG")
    p.add_argument("--region", default=None, help="bounding region x,y,w,h to evaluate")
    p.add_argument("--mask", default=None, help="bounding region x,y,w,h to ignore")
    p.add_argument("--threshold", type=float, default=0.001, help="minimum change ratio to pass (default: 0.001)")

    p = sub.add_parser("type", help="type text into application")
    p.add_argument("app")
    p.add_argument("text")

    p = sub.add_parser("key", help="send special key / key combo to application")
    p.add_argument("app")
    p.add_argument("key")

    p = sub.add_parser("click", help="click at coordinates (x, y) relative to application window")
    p.add_argument("app")
    p.add_argument("x", type=int, help="X coordinate relative to window top-left")
    p.add_argument("y", type=int, help="Y coordinate relative to window top-left")
    p.add_argument("--button", choices=["left", "right", "double"], default="left", help="mouse button action (default: left)")

    p = sub.add_parser("status", help="is the app running? (pid, window)")
    p.add_argument("app")

    sub.add_parser("list", help="list running GUI processes")

    p = sub.add_parser("quit", help="close the app")
    p.add_argument("app")
    p.add_argument("--force", action="store_true", help="kill instead of asking nicely")

    p = sub.add_parser("tools", help="list registered tools and schemas")
    p.add_argument("app", nargs="?", default=None, help="optional app name to filter")

    p = sub.add_parser("schema", help="display JSON schema for a registered tool")
    p.add_argument("tool_id", help="tool identifier (e.g. editor.save_all)")

    p = sub.add_parser("exec", help="validate arguments and execute a registered tool")
    p.add_argument("tool_id", help="tool identifier (e.g. editor.save_all)")
    p.add_argument("--args-json", default=None, help="JSON arguments matching tool schema")

    p = sub.add_parser("node", help="execute a verified macro-node contract")
    p.add_argument("spec", help="spec JSON string or file path")
    p.add_argument("--params-json", default=None, help="JSON parameters for text template interpolation")

    p = sub.add_parser("tree", help="dump semantic desktop DOM / accessibility tree")
    p.add_argument("app")
    p.add_argument("--depth", type=int, default=3, help="max hierarchy depth (default: 3)")
    p.add_argument("--max-children", type=int, default=25, help="max child nodes per parent (default: 25)")

    p = sub.add_parser("query", help="query semantic accessibility elements by role, name, or id")
    p.add_argument("app")
    p.add_argument("--role", default=None, help="control type filter (e.g. 'button')")
    p.add_argument("--name", default=None, help="element name regex/substring filter")
    p.add_argument("--id", default=None, help="automation ID regex/substring filter")

    p = sub.add_parser("a11y-click", help="click element semantically located by accessibility query")
    p.add_argument("app")
    p.add_argument("--role", default=None, help="control type filter (e.g. 'button')")
    p.add_argument("--name", default=None, help="element name regex/substring filter")
    p.add_argument("--id", default=None, help="automation ID regex/substring filter")
    p.add_argument("--button", choices=["left", "right", "double"], default="left", help="mouse button action (default: left)")

    p = sub.add_parser("ocr", help="run local OCR on application window or image file")
    p.add_argument("target", help="application name or image file path")

    p = sub.add_parser("vision-find", help="locate text query visually in app window or image")
    p.add_argument("target", help="application name or image file path")
    p.add_argument("query", help="text query to locate")

    p = sub.add_parser("vision-click", help="locate text query visually and click it")
    p.add_argument("app")
    p.add_argument("query")
    p.add_argument("--button", choices=["left", "right", "double"], default="left", help="mouse button action (default: left)")

    p = sub.add_parser("route", help="route a task through the local decision router")
    p.add_argument("task", help="task description to route")
    p.add_argument("--context-json", default=None, help="JSON context string")

    p = sub.add_parser("benchmark", help="run the sub-1GB local router benchmark")
    p.add_argument("--json", action="store_true", help="output JSON")

    p_b = sub.add_parser("browser", help="ephemeral browser worker & profile-forking adapter")
    b_sub = p_b.add_subparsers(dest="browser_action", required=True)

    p_bfork = b_sub.add_parser("fork", help="spawn ephemeral browser worker with isolated profile & credentials")
    p_bfork.add_argument("--browser", default=None, help="browser flavor: brave, chrome, chromium, edge, firefox")
    p_bfork.add_argument("--url", default=None, help="initial URL to navigate to")
    p_bfork.add_argument("--headless", action="store_true", help="run in background headless mode")
    p_bfork.add_argument("--port", type=int, default=None, help="remote CDP debugging port")
    p_bfork.add_argument("--cookie-file", default=None, help="path to JSON cookie export (e.g. sockmusegoogle.json)")
    p_bfork.add_argument("--no-copy-profile", action="store_true", help="skip copying master profile state")

    b_sub.add_parser("list", help="list active ephemeral browser workers")

    p_bdestroy = b_sub.add_parser("destroy", help="terminate worker and purge temporary profile directory")
    p_bdestroy.add_argument("worker_id", help="worker ID, PID, or 'all'")

    b_sub.add_parser("status", help="check installed browsers and SAO bridge status")

    p_bbridge = b_sub.add_parser("bridge", help="route browser request via SAO bridge or auto-offload to ephemeral worker")
    p_bbridge.add_argument("--url", default=None, help="target URL")
    p_bbridge.add_argument("--cookie-file", default=None, help="path to cookie export file to inject")
    p_bbridge.add_argument("--headless", action="store_true", default=True, help="run in background headless mode")
    p_bbridge.add_argument("--no-ephemeral", action="store_true", help="do not prefer ephemeral worker")
    p_bbridge.add_argument("--sao-url", default=None, help="custom SAO browser URL")

    p_burl = b_sub.add_parser("url", help="display the unified browser WebRTC & CDP URLs")
    p_burl.add_argument("--sao-url", default=None, help="custom SAO browser URL")

    a = ap.parse_args()

    h = HANDLERS.get(OS)
    if not h:
        fail(a.action, f"unsupported OS: {OS}")

    if a.action == "diff":
        run_diff(a.before, a.after, a.region, a.mask, a.threshold)
        return
    elif a.action == "tools":
        run_tools(a.app)
        return
    elif a.action == "schema":
        run_schema(a.tool_id)
        return
    elif a.action == "exec":
        run_exec(a.tool_id, a.args_json)
        return
    elif a.action == "node":
        if not execute_macro_node:
            fail("node", "macro_node module not available")
        params = json.loads(a.params_json) if a.params_json else None
        res = execute_macro_node(a.spec, h, diff_fn=compute_diff, params=params)
        emit(res, code=0 if res.get("ok") else 1)
        return
    elif a.action == "tree":
        if not run_a11y_tree:
            fail("tree", "a11y module not available")
        res = run_a11y_tree(a.app, depth=a.depth, max_children=a.max_children, resolve_fn=h.get("resolve"))
        emit(res, code=0 if res.get("ok") else 1)
        return
    elif a.action == "query":
        if not run_a11y_query:
            fail("query", "a11y module not available")
        res = run_a11y_query(a.app, role=a.role, name=a.name, automation_id=a.id, resolve_fn=h.get("resolve"))
        emit(res, code=0 if res.get("ok") else 1)
        return
    elif a.action == "a11y-click":
        if not run_a11y_click:
            fail("a11y-click", "a11y module not available")
        res = run_a11y_click(
            a.app,
            role=a.role,
            name=a.name,
            automation_id=a.id,
            button=a.button,
            resolve_fn=h.get("resolve"),
            click_fn=h.get("click_raw"),
            focus_fn=h.get("focus_raw"),
        )
        emit(res, code=0 if res.get("ok") else 1)
        return
    elif a.action == "ocr":
        if not ocr_image:
            fail("ocr", "vision module not available")
        target_path = a.target
        temp_file = None
        if not os.path.isfile(target_path):
            temp_file = os.path.abspath(f"temp_ocr_{int(time.time()*1000)}.png")
            h["see"](target_path, temp_file)
            target_path = temp_file
        try:
            res = ocr_image(target_path)
            emit(res, code=0 if res.get("ok") else 1)
        finally:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
        return
    elif a.action == "vision-find":
        if not run_vision_find:
            fail("vision-find", "vision module not available")
        res = run_vision_find(
            a.target,
            a.query,
            resolve_fn=h.get("resolve"),
            see_fn=lambda app, out: h["see"](app, out),
        )
        emit(res, code=0 if res.get("ok") else 1)
        return
    elif a.action == "vision-click":
        if not run_vision_click:
            fail("vision-click", "vision module not available")
        res = run_vision_click(
            a.app,
            a.query,
            button=a.button,
            resolve_fn=h.get("resolve"),
            see_fn=lambda app, out: h["see"](app, out),
            click_fn=h.get("click_raw"),
            focus_fn=h.get("focus_raw"),
        )
        emit(res, code=0 if res.get("ok") else 1)
        return
    elif a.action == "route":
        if not LocalRouter:
            fail("route", "router module not available")
        r = LocalRouter()
        ctx = json.loads(a.context_json) if a.context_json else {}
        dec = r.route(a.task, ctx)
        is_valid, err = r.validate_decision(dec)
        out = dec.to_dict()
        out["schema_valid"] = is_valid
        if err:
            out["schema_error"] = err
        emit(out, code=0 if dec.ok and is_valid else 1)
        return
    elif a.action == "benchmark":
        if not run_benchmark:
            fail("benchmark", "router module not available")
        rep = run_benchmark()
        emit(rep, code=0 if rep.get("ok") else 1)
        return
    elif a.action == "browser":
        if not fork_browser:
            fail("browser", "browser module not available")
        if a.browser_action == "fork":
            res = fork_browser(
                browser=a.browser,
                url=a.url,
                headless=a.headless,
                port=a.port,
                cookie_file=a.cookie_file,
                copy_profile=not a.no_copy_profile,
            )
            emit(res, code=0 if res.get("ok") else 1)
            return
        elif a.browser_action == "list":
            res = list_browser_workers()
            emit(res, code=0 if res.get("ok") else 1)
            return
        elif a.browser_action == "destroy":
            res = destroy_browser_worker(a.worker_id)
            emit(res, code=0 if res.get("ok") else 1)
            return
        elif a.browser_action == "status":
            res = browser_status()
            emit(res, code=0 if res.get("ok") else 1)
            return
        elif a.browser_action == "bridge":
            if not bridge_browser:
                fail("browser", "bridge module not available")
            res = bridge_browser(
                url=a.url,
                cookie_file=a.cookie_file,
                headless=a.headless,
                prefer_ephemeral=not a.no_ephemeral,
                sao_url=a.sao_url,
            )
            emit(res, code=0 if res.get("ok") else 1)
            return
        elif a.browser_action == "url":
            if not get_unified_browser_url:
                fail("browser", "browser module not available")
            res = get_unified_browser_url(sao_url=a.sao_url)
            emit(res, code=0 if res.get("ok") else 1)
            return


    if a.action == "list":
        h["list"]()
    elif a.action == "open":
        h["open"](a.app, [x for x in a.args if x != "--"])
    elif a.action == "quit":
        h["quit"](a.app, a.force)
    elif a.action == "see":
        h["see"](a.app, a.output)
    elif a.action == "type":
        h["type"](a.app, a.text)
    elif a.action == "key":
        h["key"](a.app, a.key)
    elif a.action == "click":
        h["click"](a.app, a.x, a.y, a.button)
    else:
        h[a.action](a.app)


if __name__ == "__main__":
    main()
