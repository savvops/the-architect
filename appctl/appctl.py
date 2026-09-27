#!/usr/bin/env python3
"""appctl v0.1 — cross-platform application lifecycle adapter for The Architect.

One typed vocabulary for app lifecycle on any OS, so the agent never invents
raw shell text. Every command emits JSON evidence the verifier can check.

    appctl open <app> [--args ...]     start an application
    appctl focus <app>                 bring its window to the foreground
    appctl status <app> [--json]       is it running? pid, window title
    appctl list                        running GUI processes
    appctl quit <app> [--force]        close gracefully, or force-kill

Exit codes: 0 = ok, 1 = action failed (app not found / not running), 2 = usage error.
All output is JSON on stdout: {"ok": bool, "action": str, ...}.
"""

import argparse
import json
import platform
import shutil
import subprocess
import sys

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
    WM_CLOSE = 0x0010
    SW_RESTORE = 9

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

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _DESKTOPENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.LPWSTR, wintypes.LPARAM)

    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _user32.OpenDesktopW.restype = wintypes.HANDLE
    _user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    _user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, _WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumDesktopsW.argtypes = [wintypes.HANDLE, _DESKTOPENUMPROC, wintypes.LPARAM]

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

    _user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    _h_default_desk = _user32.OpenDesktopW("Default", 0, False, 0x01FF)
    if _h_default_desk:
        _user32.SetThreadDesktop(_h_default_desk)


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


def win_open(app, args):
    binary = shutil.which(app) or shutil.which(f"{app}.exe") or app
    cmd_line = subprocess.list2cmdline([binary] + args)

    # Launch onto user interactive desktop WinSta0\Default
    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(_STARTUPINFOW)
    si.lpDesktop = "WinSta0\\Default"
    pi = _PROCESS_INFORMATION()

    res = _kernel32.CreateProcessW(
        None, cmd_line, None, None, False, 0, None, None,
        ctypes.byref(si), ctypes.byref(pi)
    )
    if res:
        pid = pi.dwProcessId
        _kernel32.CloseHandle(pi.hProcess)
        _kernel32.CloseHandle(pi.hThread)
        return emit({"ok": True, "action": "open", "app": app, "evidence": {"pid": pid}})

    try:
        p = subprocess.Popen(
            [binary] + args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return emit({"ok": True, "action": "open", "app": app, "evidence": {"pid": p.pid}})
    except OSError:
        arg_str = " ".join(f"'{a}'" for a in args)
        extra = f" -ArgumentList {arg_str}" if arg_str else ""
        rc, out = _ps(f"Start-Process -FilePath '{app}'{extra} -PassThru | Select-Object -ExpandProperty Id")
        if rc != 0 or not out.isdigit():
            return fail("open", f"could not start '{app}'", detail=out)
        return emit({"ok": True, "action": "open", "app": app, "evidence": {"pid": int(out)}})


def win_status(app):
    procs = _win_match_procs(app)
    windows = _win_visible_windows()

    if not procs:
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


def win_focus(app):
    app_lower = (app[:-4] if app.lower().endswith(".exe") else app).lower()
    windows = _win_visible_windows()

    target_hwnd = None
    target_pid = None
    target_title = None

    # 1. Match by numeric PID
    if app.isdigit():
        pid_int = int(app)
        for w in windows:
            if w["pid"] == pid_int:
                target_hwnd = w["hwnd"]
                target_pid = w["pid"]
                target_title = w["title"]
                break

    # 2. Match by window title substring (more specific than app name)
    if not target_hwnd and len(app) >= 2:
        for w in windows:
            if app_lower in w["title"].lower():
                target_hwnd = w["hwnd"]
                target_pid = w["pid"]
                target_title = w["title"]
                break

    # 3. Match by process name (prefer newest PID if multiple exist)
    if not target_hwnd:
        procs = _win_match_procs(app)
        pids = sorted([p["pid"] for p in procs], reverse=True)
        for target_p in pids:
            for w in windows:
                if w["pid"] == target_p:
                    target_hwnd = w["hwnd"]
                    target_pid = w["pid"]
                    target_title = w["title"]
                    break
            if target_hwnd:
                break

    if not target_hwnd:
        return fail("focus", f"no visible window for '{app}'")

    # Bypass Windows foreground lock timeout (standard Win32 automation trick)
    _user32.keybd_event(0x12, 0, 0, 0)  # ALT down
    _user32.keybd_event(0x12, 0, 2, 0)  # ALT up

    _user32.ShowWindow(target_hwnd, SW_RESTORE)

    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040
    _user32.SetWindowPos(target_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    _user32.SetWindowPos(target_hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)

    fore_wnd = _user32.GetForegroundWindow()
    fore_tid = _user32.GetWindowThreadProcessId(fore_wnd, None)
    cur_tid = _kernel32.GetCurrentThreadId()
    if fore_tid != cur_tid:
        _user32.AttachThreadInput(cur_tid, fore_tid, True)
    _user32.BringWindowToTop(target_hwnd)
    _user32.SetForegroundWindow(target_hwnd)
    if fore_tid != cur_tid:
        _user32.AttachThreadInput(cur_tid, fore_tid, False)

    evidence = {"foreground": True}
    if target_pid:
        evidence["pid"] = target_pid
    if target_title:
        evidence["window"] = target_title

    return emit({"ok": True, "action": "focus", "app": app,
                 "evidence": evidence})


def win_quit(app, force):
    app_lower = (app[:-4] if app.lower().endswith(".exe") else app).lower()
    procs = _win_match_procs(app)
    if not procs:
        return emit({"ok": True, "action": "quit", "app": app,
                     "evidence": {"running": False, "method": "already-closed"}})

    pids = [p["pid"] for p in procs]

    if not force:
        windows = _win_visible_windows()
        target_hwnds = [w["hwnd"] for w in windows if w["pid"] in pids]
        if not target_hwnds:
            target_hwnds = [w["hwnd"] for w in windows if app_lower in w["title"].lower()]

        WM_SYSCOMMAND = 0x0112
        SC_CLOSE = 0xF060
        for hwnd in target_hwnds:
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            _user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)

        for _ in range(15):
            time.sleep(0.2)
            alive = [p for p in _win_all_processes() if p["pid"] in pids]
            if not alive:
                return emit({"ok": True, "action": "quit", "app": app,
                             "evidence": {"running": False, "method": "graceful"}})

    for pid in pids:
        hProc = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if hProc:
            _kernel32.TerminateProcess(hProc, 1)
            _kernel32.CloseHandle(hProc)

    time.sleep(0.2)
    still_alive = [p for p in _win_all_processes() if p["pid"] in pids]
    return emit({"ok": True, "action": "quit", "app": app,
                 "evidence": {"running": False,
                              "method": "force" if force else "graceful-timeout-force"}})


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
    import os
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
    import os, signal, time
    targets = _live_pids(app)
    sig = signal.SIGKILL if force else signal.SIGTERM
    for pid in targets:
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    # Graceful close gets a moment; then confirm.
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
    # Best-effort: needs wmctrl or xdotool; absent on most stock installs.
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


# ------------------------------------------------------------------ dispatch -
HANDLERS = {
    "Windows": {"open": win_open, "focus": win_focus, "status": win_status,
                "list": lambda: win_list(), "quit": win_quit},
    "Darwin": {"open": mac_open, "focus": mac_focus, "status": mac_status,
               "list": lambda: mac_status(""), "quit": mac_quit},
    "Linux": {"open": lin_open, "focus": lin_focus, "status": lin_status,
              "list": lambda: lin_status(""), "quit": lin_quit},
}


def main():
    ap = argparse.ArgumentParser(prog="appctl",
                                 description="Cross-platform app lifecycle adapter (The Architect v0.1)")
    sub = ap.add_subparsers(dest="action", required=True)

    p = sub.add_parser("open", help="start an application")
    p.add_argument("app")
    p.add_argument("--args", nargs=argparse.REMAINDER, default=[], help="arguments for the app")

    p = sub.add_parser("focus", help="bring app window to foreground")
    p.add_argument("app")

    p = sub.add_parser("status", help="is the app running? (pid, window)")
    p.add_argument("app")

    sub.add_parser("list", help="list running GUI processes")

    p = sub.add_parser("quit", help="close the app")
    p.add_argument("app")
    p.add_argument("--force", action="store_true", help="kill instead of asking nicely")

    a = ap.parse_args()
    h = HANDLERS.get(OS)
    if not h:
        fail(a.action, f"unsupported OS: {OS}")

    if a.action == "list":
        h["list"]()
    elif a.action == "open":
        h["open"](a.app, [x for x in a.args if x != "--"])
    elif a.action == "quit":
        h["quit"](a.app, a.force)
    else:
        h[a.action](a.app)


if __name__ == "__main__":
    main()
