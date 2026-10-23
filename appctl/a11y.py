"""
The Architect - Accessibility Adapter / Desktop DOM (Phase 6: Tier A - Accessibility Adapter)
Windows UIAutomation (UIA) COM-based semantic DOM adapter in pure Python ctypes.
Extracts semantic tree (roles, names, IDs, bounding boxes), queries semantic elements,
and actuates semantic element clicks without hardcoded pixel coordinates.
Zero external pip dependencies (pure Python standard library).
"""

import ctypes
from ctypes import wintypes, byref, c_void_p, c_int, c_long, POINTER, Structure
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    _ole32 = ctypes.windll.ole32
    _user32 = ctypes.windll.user32
    _oleaut32 = ctypes.windll.oleaut32
    _oleaut32.SysFreeString.argtypes = [c_void_p]
    _oleaut32.SysFreeString.restype = None

    UIA_InvokePatternId = 10000

    class GUID(Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_byte * 8),
        ]

    try:
        from appctl.appctl import RECT
    except ImportError:
        try:
            from appctl import RECT
        except ImportError:
            class RECT(Structure):
                _fields_ = [
                    ("left", c_long),
                    ("top", c_long),
                    ("right", c_long),
                    ("bottom", c_long),
                ]

    CLSID_CUIAutomation8 = GUID(
        0xE22AD333, 0xB25F, 0x460C, (ctypes.c_byte * 8)(0x83, 0xD0, 0x05, 0x81, 0x10, 0x73, 0x95, 0xC9)
    )
    CLSID_CUIAutomation = GUID(
        0xFF48DBA4, 0x60EF, 0x4201, (ctypes.c_byte * 8)(0xAA, 0x87, 0x54, 0x50, 0x3E, 0x0F, 0x59, 0x4E)
    )
    IID_IUIAutomation = GUID(
        0x30CBE57D, 0xD9D0, 0x452A, (ctypes.c_byte * 8)(0xAB, 0x13, 0x7A, 0xC5, 0xAC, 0x48, 0x25, 0xEE)
    )


class UIAClient:
    """Pure ctypes Windows UIAutomation client."""

    def __init__(self):
        if not IS_WINDOWS:
            raise NotImplementedError("UIAutomation adapter is only supported on Windows")

        _ole32.CoInitialize(None)
        self.pUIA = c_void_p()

        # Try CUIAutomation8 first (Windows 8/10/11), fallback to CUIAutomation
        hr = _ole32.CoCreateInstance(
            byref(CLSID_CUIAutomation8), None, 1, byref(IID_IUIAutomation), byref(self.pUIA)
        )
        if hr != 0 or not self.pUIA:
            hr = _ole32.CoCreateInstance(
                byref(CLSID_CUIAutomation), None, 1, byref(IID_IUIAutomation), byref(self.pUIA)
            )
            if hr != 0 or not self.pUIA:
                raise RuntimeError(f"Failed to create UIAutomation COM instance: hr={hex(hr & 0xFFFFFFFF)}")

        uia_vt = ctypes.cast(ctypes.cast(self.pUIA, POINTER(c_void_p)).contents, POINTER(c_void_p))
        self.fn_ElementFromHandle = ctypes.WINFUNCTYPE(c_long, c_void_p, wintypes.HWND, POINTER(c_void_p))(
            uia_vt[6]
        )
        self.fn_get_ControlViewWalker = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_void_p))(uia_vt[14])

        self.pWalker = c_void_p()
        hr = self.fn_get_ControlViewWalker(self.pUIA, byref(self.pWalker))
        if hr != 0 or not self.pWalker:
            raise RuntimeError(f"Failed to obtain ControlViewWalker: hr={hex(hr & 0xFFFFFFFF)}")

        walker_vt = ctypes.cast(ctypes.cast(self.pWalker, POINTER(c_void_p)).contents, POINTER(c_void_p))
        self.fn_GetFirstChildElement = ctypes.WINFUNCTYPE(c_long, c_void_p, c_void_p, POINTER(c_void_p))(
            walker_vt[4]
        )
        self.fn_GetNextSiblingElement = ctypes.WINFUNCTYPE(c_long, c_void_p, c_void_p, POINTER(c_void_p))(
            walker_vt[6]
        )

    def _release_com(self, p_unknown: c_void_p) -> None:
        if p_unknown and p_unknown.value:
            vt = ctypes.cast(ctypes.cast(p_unknown, POINTER(c_void_p)).contents, POINTER(c_void_p))
            release_fn = ctypes.WINFUNCTYPE(c_long, c_void_p)(vt[2])
            release_fn(p_unknown)

    def get_element_info(self, pElem: c_void_p) -> Optional[Dict[str, Any]]:
        if not pElem or not pElem.value:
            return None
        evt = ctypes.cast(ctypes.cast(pElem, POINTER(c_void_p)).contents, POINTER(c_void_p))

        get_CurrentProcessId = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_int))(evt[20])
        get_CurrentLocalizedControlType = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_void_p))(
            evt[22]
        )
        get_CurrentName = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_void_p))(evt[23])
        get_CurrentIsEnabled = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(wintypes.BOOL))(evt[28])
        get_CurrentAutomationId = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_void_p))(evt[29])
        get_CurrentClassName = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_void_p))(evt[30])
        get_CurrentBoundingRectangle = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(RECT))(evt[43])

        pid = c_int()
        get_CurrentProcessId(pElem, byref(pid))

        nm = c_void_p()
        get_CurrentName(pElem, byref(nm))
        name_str = ctypes.wstring_at(nm.value) if nm.value else ""
        if nm.value:
            _oleaut32.SysFreeString(nm)

        lct = c_void_p()
        get_CurrentLocalizedControlType(pElem, byref(lct))
        role_str = ctypes.wstring_at(lct.value) if lct.value else ""
        if lct.value:
            _oleaut32.SysFreeString(lct)

        aid = c_void_p()
        get_CurrentAutomationId(pElem, byref(aid))
        aid_str = ctypes.wstring_at(aid.value) if aid.value else ""
        if aid.value:
            _oleaut32.SysFreeString(aid)

        cls = c_void_p()
        get_CurrentClassName(pElem, byref(cls))
        cls_str = ctypes.wstring_at(cls.value) if cls.value else ""
        if cls.value:
            _oleaut32.SysFreeString(cls)

        en = wintypes.BOOL()
        get_CurrentIsEnabled(pElem, byref(en))

        rc = RECT()
        get_CurrentBoundingRectangle(pElem, byref(rc))

        w = rc.right - rc.left
        h = rc.bottom - rc.top

        return {
            "name": name_str,
            "role": role_str,
            "id": aid_str,
            "class": cls_str,
            "enabled": bool(en.value),
            "bounds": [rc.left, rc.top, w, h],
        }

    def build_tree(
        self, pElem: c_void_p, current_depth: int = 0, max_depth: int = 3, max_children: int = 25
    ) -> Dict[str, Any]:
        info = self.get_element_info(pElem) or {}
        node: Dict[str, Any] = dict(info)

        if current_depth >= max_depth:
            return node

        children = []
        child = c_void_p()
        hr = self.fn_GetFirstChildElement(self.pWalker, pElem, byref(child))
        count = 0

        while child and child.value and count < max_children:
            child_node = self.build_tree(
                child, current_depth + 1, max_depth=max_depth, max_children=max_children
            )
            children.append(child_node)
            count += 1

            nxt = c_void_p()
            self.fn_GetNextSiblingElement(self.pWalker, child, byref(nxt))
            self._release_com(child)
            child = nxt

        if child and child.value:
            self._release_com(child)

        node["children"] = children
        return node

    def query_tree(
        self,
        pElem: c_void_p,
        role: Optional[str] = None,
        name: Optional[str] = None,
        automation_id: Optional[str] = None,
        max_results: int = 50,
        current_depth: int = 0,
        max_depth: int = 8,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        if current_depth > max_depth or not pElem or not pElem.value:
            return results

        info = self.get_element_info(pElem)
        if info:
            match = True
            if role and not re.search(role, info.get("role", ""), re.IGNORECASE):
                match = False
            if name and not re.search(name, info.get("name", ""), re.IGNORECASE):
                match = False
            if automation_id and not re.search(automation_id, info.get("id", ""), re.IGNORECASE):
                match = False

            # Require at least one non-empty search criteria or return all
            if (role or name or automation_id) and match:
                results.append(dict(info))
                if len(results) >= max_results:
                    return results

        child = c_void_p()
        self.fn_GetFirstChildElement(self.pWalker, pElem, byref(child))
        while child and child.value and len(results) < max_results:
            sub = self.query_tree(
                child,
                role=role,
                name=name,
                automation_id=automation_id,
                max_results=max_results - len(results),
                current_depth=current_depth + 1,
                max_depth=max_depth,
            )
            results.extend(sub)
            nxt = c_void_p()
            self.fn_GetNextSiblingElement(self.pWalker, child, byref(nxt))
            self._release_com(child)
            child = nxt

        if child and child.value:
            self._release_com(child)

        return results

    def find_element(
        self,
        pElem: c_void_p,
        role: Optional[str] = None,
        name: Optional[str] = None,
        automation_id: Optional[str] = None,
        current_depth: int = 0,
        max_depth: int = 8,
    ) -> Optional[c_void_p]:
        """Traverse and return the first COM element matching role, name, or automation ID."""
        if current_depth > max_depth or not pElem or not pElem.value:
            return None

        info = self.get_element_info(pElem)
        if info:
            match = True
            if role and not re.search(role, info.get("role", ""), re.IGNORECASE):
                match = False
            if name and not re.search(name, info.get("name", ""), re.IGNORECASE):
                match = False
            if automation_id and not re.search(automation_id, info.get("id", ""), re.IGNORECASE):
                match = False

            if (role or name or automation_id) and match:
                evt = ctypes.cast(ctypes.cast(pElem, POINTER(c_void_p)).contents, POINTER(c_void_p))
                addref_fn = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(evt[1])
                addref_fn(pElem)
                return pElem

        child = c_void_p()
        self.fn_GetFirstChildElement(self.pWalker, pElem, byref(child))
        while child and child.value:
            found = self.find_element(
                child,
                role=role,
                name=name,
                automation_id=automation_id,
                current_depth=current_depth + 1,
                max_depth=max_depth,
            )
            if found:
                self._release_com(child)
                return found

            nxt = c_void_p()
            self.fn_GetNextSiblingElement(self.pWalker, child, byref(nxt))
            self._release_com(child)
            child = nxt

        if child and child.value:
            self._release_com(child)

        return None

    def invoke_element(self, pElem: c_void_p) -> bool:
        """Attempt true semantic actuation via IUIAutomationInvokePattern (Tier A)."""
        if not pElem or not pElem.value:
            return False
        try:
            evt = ctypes.cast(ctypes.cast(pElem, POINTER(c_void_p)).contents, POINTER(c_void_p))
            fn_GetCurrentPattern = ctypes.WINFUNCTYPE(c_long, c_void_p, c_int, POINTER(c_void_p))(evt[16])
            pPat = c_void_p()
            hr = fn_GetCurrentPattern(pElem, UIA_InvokePatternId, byref(pPat))
            if hr != 0 or not pPat or not pPat.value:
                return False

            try:
                p_vt = ctypes.cast(ctypes.cast(pPat, POINTER(c_void_p)).contents, POINTER(c_void_p))
                fn_Invoke = ctypes.WINFUNCTYPE(c_long, c_void_p)(p_vt[3])
                hr_invoke = fn_Invoke(pPat)
                return hr_invoke == 0
            finally:
                self._release_com(pPat)
        except Exception:
            return False


_GLOBAL_UIA: Optional[UIAClient] = None


def get_uia_client() -> UIAClient:
    global _GLOBAL_UIA
    if _GLOBAL_UIA is None:
        _GLOBAL_UIA = UIAClient()
    return _GLOBAL_UIA


def get_a11y_tree(
    hwnd: int, depth: int = 3, max_children: int = 25
) -> Dict[str, Any]:
    """Retrieve accessibility tree for a given window handle."""
    if not IS_WINDOWS:
        return {"error": "Accessibility tree is only supported on Windows"}

    client = get_uia_client()
    pElem = c_void_p()
    hr = client.fn_ElementFromHandle(client.pUIA, hwnd, byref(pElem))
    if hr != 0 or not pElem:
        return {"error": f"Failed to get element for HWND {hwnd}: hr={hex(hr & 0xFFFFFFFF)}"}

    try:
        tree = client.build_tree(pElem, current_depth=0, max_depth=depth, max_children=max_children)
        return tree
    finally:
        client._release_com(pElem)


def query_a11y_elements(
    hwnd: int,
    role: Optional[str] = None,
    name: Optional[str] = None,
    automation_id: Optional[str] = None,
    max_results: int = 50,
) -> List[Dict[str, Any]]:
    """Query accessibility elements matching role, name, or automation ID."""
    if not IS_WINDOWS:
        return []

    client = get_uia_client()
    pElem = c_void_p()
    hr = client.fn_ElementFromHandle(client.pUIA, hwnd, byref(pElem))
    if hr != 0 or not pElem:
        return []

    try:
        matches = client.query_tree(
            pElem,
            role=role,
            name=name,
            automation_id=automation_id,
            max_results=max_results,
        )

        # Get window position to compute relative coordinates (using DWM attribute for exact frame)
        wrect = RECT()
        _dwmapi = ctypes.windll.dwmapi
        if _dwmapi.DwmGetWindowAttribute(hwnd, 9, byref(wrect), ctypes.sizeof(RECT)) != 0:
            _user32.GetWindowRect(hwnd, byref(wrect))
        win_left = wrect.left
        win_top = wrect.top

        for item in matches:
            b = item.get("bounds", [0, 0, 0, 0])
            screen_cx = b[0] + b[2] // 2
            screen_cy = b[1] + b[3] // 2
            item["center"] = [screen_cx, screen_cy]
            item["relative_center"] = [screen_cx - win_left, screen_cy - win_top]

        return matches
    finally:
        client._release_com(pElem)


def run_a11y_tree(app: str, depth: int = 3, max_children: int = 25, resolve_fn=None) -> Dict[str, Any]:
    """Inspect and return accessibility tree as an Architect JSON payload."""
    if not resolve_fn:
        return {"ok": False, "action": "tree", "error": "resolve_fn required"}

    target = resolve_fn(app)
    if not target:
        return {
            "ok": False,
            "action": "tree",
            "error": f"no visible window for '{app}'",
            "evidence": {"app": app},
        }

    tree = get_a11y_tree(target["hwnd"], depth=depth, max_children=max_children)
    if "error" in tree:
        return {
            "ok": False,
            "action": "tree",
            "error": tree["error"],
            "evidence": {"app": app, "pid": target["pid"], "window": target["title"]},
        }

    return {
        "ok": True,
        "action": "tree",
        "app": app,
        "evidence": {
            "pid": target["pid"],
            "window": target["title"],
            "tree": tree,
        },
    }


def run_a11y_query(
    app: str,
    role: Optional[str] = None,
    name: Optional[str] = None,
    automation_id: Optional[str] = None,
    resolve_fn=None,
) -> Dict[str, Any]:
    """Query semantic elements and return matches with bounding boxes."""
    if not resolve_fn:
        return {"ok": False, "action": "query", "error": "resolve_fn required"}

    target = resolve_fn(app)
    if not target:
        return {
            "ok": False,
            "action": "query",
            "error": f"no visible window for '{app}'",
            "evidence": {"app": app},
        }

    elements = query_a11y_elements(
        target["hwnd"], role=role, name=name, automation_id=automation_id
    )

    return {
        "ok": True,
        "action": "query",
        "app": app,
        "evidence": {
            "pid": target["pid"],
            "window": target["title"],
            "query": {"role": role, "name": name, "id": automation_id},
            "count": len(elements),
            "elements": elements,
        },
    }


def run_a11y_click(
    app: str,
    role: Optional[str] = None,
    name: Optional[str] = None,
    automation_id: Optional[str] = None,
    button: str = "left",
    resolve_fn=None,
    click_fn=None,
    focus_fn=None,
) -> Dict[str, Any]:
    """Semantically find an element and click its center without hardcoded pixel coordinates."""
    if not resolve_fn or not click_fn:
        return {"ok": False, "action": "a11y-click", "error": "resolve_fn and click_fn required"}

    target = resolve_fn(app)
    if not target:
        return {
            "ok": False,
            "action": "a11y-click",
            "error": f"no visible window for '{app}'",
            "evidence": {"app": app},
        }

    elements = query_a11y_elements(
        target["hwnd"], role=role, name=name, automation_id=automation_id
    )

    if not elements:
        return {
            "ok": False,
            "action": "a11y-click",
            "error": f"no element matched query (role='{role}', name='{name}', id='{automation_id}') in '{app}'",
            "evidence": {
                "app": app,
                "pid": target["pid"],
                "window": target["title"],
                "query": {"role": role, "name": name, "id": automation_id},
            },
        }

    # Pick the first matching element
    matched = elements[0]
    rel_center = matched.get("relative_center", [0, 0])
    rx, ry = rel_center[0], rel_center[1]

    # Try Tier A semantic actuation via IUIAutomationInvokePattern first (for primary click)
    invoked = False
    if IS_WINDOWS and button == "left":
        try:
            client = get_uia_client()
            pRoot = c_void_p()
            hr = client.fn_ElementFromHandle(client.pUIA, target["hwnd"], byref(pRoot))
            if hr == 0 and pRoot and pRoot.value:
                try:
                    pMatch = client.find_element(
                        pRoot, role=role, name=name, automation_id=automation_id
                    )
                    if pMatch and pMatch.value:
                        try:
                            invoked = client.invoke_element(pMatch)
                        finally:
                            client._release_com(pMatch)
                finally:
                    client._release_com(pRoot)
        except Exception:
            invoked = False

    if invoked:
        return {
            "ok": True,
            "action": "a11y-click",
            "app": app,
            "evidence": {
                "pid": target["pid"],
                "window": target["title"],
                "matched_element": matched,
                "relative_coordinates": {"x": rx, "y": ry},
                "button": button,
                "clicked": True,
                "invoked": True,
                "method": "InvokePattern",
                "total_matches": len(elements),
            },
        }

    # Focus target application
    if focus_fn:
        try:
            focus_fn(target["hwnd"])
        except Exception:
            try:
                focus_fn(app)
            except Exception:
                pass
        time.sleep(0.05)

    # Click the element center
    click_fn(target, rx, ry, button=button)

    return {
        "ok": True,
        "action": "a11y-click",
        "app": app,
        "evidence": {
            "pid": target["pid"],
            "window": target["title"],
            "matched_element": matched,
            "relative_coordinates": {"x": rx, "y": ry},
            "button": button,
            "clicked": True,
            "invoked": False,
            "method": "coordinate_click",
            "total_matches": len(elements),
        },
    }
