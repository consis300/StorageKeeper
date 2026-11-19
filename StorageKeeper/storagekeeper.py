"""
StorageKeeper - see everything that takes up space on your PC.

Desktop app: the engine runs inside the app and the UI opens in its own native
window (via pywebview / Microsoft Edge WebView2). GitHub Actions packages it
into a single StorageKeeper.exe - see README.md.
Everything stays on your computer - nothing is uploaded anywhere.
StorageKeeper is read-only: it never deletes or changes your files.
"""

import codecs
import heapq
import json
import os
import shutil
import string
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

IS_WIN = os.name == "nt"
if IS_WIN:
    import winreg

# When packaged by PyInstaller, bundled files live in sys._MEIPASS
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("STORAGEKEEPER_PORT", "0"))  # 0 = pick any free port
REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (junctions / symlinks)
NO_WINDOW = 0x08000000 if IS_WIN else 0

# --------------------------------------------------------------------------
# File categories
# --------------------------------------------------------------------------
CATEGORIES = {
    "Videos": "mp4 mkv avi mov wmv flv webm m4v mpg mpeg 3gp ts vob",
    "Images": "jpg jpeg png gif bmp tiff tif webp heic raw cr2 nef psd svg ico",
    "Music & Audio": "mp3 wav flac aac ogg m4a wma opus aiff mid midi",
    "Documents": "pdf doc docx xls xlsx ppt pptx txt rtf odt ods odp csv md epub",
    "Archives": "zip rar 7z tar gz bz2 xz iso cab tgz",
    "Programs": "exe dll msi sys ocx drv appx msix efi",
    "Game Data": "pak vpk bsa ba2 upk uasset forge wad bik ucas utoc assets resource bundle",
    "Code": "py js ts html css json xml java c cpp h cs go rs php rb sh bat ps1 ipynb",
    "System & Cache": "tmp log cache dat db sqlite etl evtx bak old dmp mui cat",
    "Virtual Disks": "vhd vhdx vmdk vdi qcow2 img",
}
EXT2CAT = {}
for _cat, _exts in CATEGORIES.items():
    for _e in _exts.split():
        EXT2CAT[_e] = _cat


def category_of(name):
    dot = name.rfind(".")
    if dot <= 0:
        return "Other"
    return EXT2CAT.get(name[dot + 1:].lower(), "Other")


def key_of(name):
    return name.lower() if IS_WIN else name


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------
class Node:
    __slots__ = ("name", "parent", "size", "files", "dirs", "children", "done")

    def __init__(self, name, parent):
        self.name = name
        self.parent = parent
        self.size = 0
        self.files = 0
        self.dirs = 0
        self.children = None
        self.done = False


class Scanner:
    def __init__(self):
        self.lock = threading.Lock()
        self.roots = {}  # normalized root path -> Node
        self.root_paths = {}  # normalized -> display path
        self.top = []  # heap of (size, path, mtime, atime)
        self.cats = {}
        self.exts = {}
        self.status = {"running": False, "roots": [], "files": 0, "dirs": 0,
                       "bytes": 0, "current": "", "started": 0, "finished": 0,
                       "errors": 0, "cancel": False}

    # -- path helpers --
    @staticmethod
    def norm(path):
        p = os.path.abspath(path)
        if IS_WIN:
            p = p.lower()
        return p.rstrip("\\/") or p

    def find(self, path):
        """Return the scan Node for a path, or None if it was never scanned."""
        np = self.norm(path)
        best = None
        for rp in self.roots:
            if np == rp or np.startswith(rp.rstrip("\\/") + os.sep):
                if best is None or len(rp) > len(best):
                    best = rp
        if best is None:
            return None
        node = self.roots[best]
        rest = np[len(best):].strip("\\/")
        if rest:
            for part in rest.split(os.sep):
                if not node.children or part not in node.children:
                    return None
                node = node.children[part]
        return node

    # -- scanning --
    def start(self, roots):
        with self.lock:
            if self.status["running"]:
                return False
            self.status.update(running=True, roots=roots, files=0, dirs=0, bytes=0,
                               current="", started=time.time(), finished=0,
                               errors=0, cancel=False)
            # A fresh scan of everything resets the global stats.
            self.top, self.cats, self.exts = [], {}, {}
            for r in roots:
                n = self.norm(r)
                # drop older roots that are inside / contain the new root
                for old in list(self.roots):
                    if old == n or old.startswith(n.rstrip("\\/") + os.sep) or \
                       n.startswith(old.rstrip("\\/") + os.sep):
                        self.roots.pop(old, None)
                        self.root_paths.pop(old, None)
        threading.Thread(target=self._run, args=(roots,), daemon=True).start()
        return True

    def cancel(self):
        self.status["cancel"] = True

    def _run(self, roots):
        try:
            for r in roots:
                if self.status["cancel"]:
                    break
                self._scan_root(r)
        finally:
            self.status["running"] = False
            self.status["finished"] = time.time()
            self.status["current"] = ""

    def _scan_root(self, root):
        n = self.norm(root)
        rootnode = Node(root, None)
        with self.lock:
            self.roots[n] = rootnode
            self.root_paths[n] = root
        st = self.status
        top = self.top
        cats, exts = self.cats, self.exts
        stack = [(root, rootnode)]
        last_update = 0
        while stack:
            if st["cancel"]:
                return
            path, node = stack.pop()
            now = time.time()
            if now - last_update > 0.15:
                st["current"] = path
                last_update = now
            fsize = 0
            fcount = 0
            try:
                with os.scandir(path) as it:
                    for e in it:
                        try:
                            if e.is_symlink():
                                continue
                            s = e.stat(follow_symlinks=False)
                            if IS_WIN and getattr(s, "st_file_attributes", 0) & REPARSE:
                                continue  # skip junctions (avoid double counting)
                            if e.is_dir(follow_symlinks=False):
                                child = Node(e.name, node)
                                if node.children is None:
                                    node.children = {}
                                node.children[key_of(e.name)] = child
                                stack.append((e.path, child))
                                st["dirs"] += 1
                            else:
                                sz = s.st_size
                                fsize += sz
                                fcount += 1
                                c = category_of(e.name)
                                cs = cats.get(c)
                                if cs is None:
                                    cats[c] = [sz, 1]
                                else:
                                    cs[0] += sz
                                    cs[1] += 1
                                dot = e.name.rfind(".")
                                ext = e.name[dot + 1:].lower() if dot > 0 else "(none)"
                                es = exts.get(ext)
                                if es is None:
                                    exts[ext] = [sz, 1]
                                else:
                                    es[0] += sz
                                    es[1] += 1
                                if len(top) < 500:
                                    heapq.heappush(top, (sz, e.path, s.st_mtime, s.st_atime))
                                elif sz > top[0][0]:
                                    heapq.heapreplace(top, (sz, e.path, s.st_mtime, s.st_atime))
                        except OSError:
                            st["errors"] += 1
            except OSError:
                st["errors"] += 1
            st["files"] += fcount
            st["bytes"] += fsize
            node.files = fcount
            # roll sizes up the tree so totals are live during the scan
            p = node
            while p is not None:
                p.size += fsize
                p = p.parent
            if node.children:
                q = node
                k = len(node.children)
                while q is not None:
                    q.dirs += k
                    q = q.parent
            node.done = True


SCANNER = Scanner()

# --------------------------------------------------------------------------
# Drives & folder listing
# --------------------------------------------------------------------------

def get_drives():
    drives = []
    if IS_WIN:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i):
                root = f"{letter}:\\"
                try:
                    u = shutil.disk_usage(root)
                except OSError:
                    continue
                label = ""
                try:
                    buf = ctypes.create_unicode_buffer(261)
                    ctypes.windll.kernel32.GetVolumeInformationW(root, buf, 261, None, None, None, None, 0)
                    label = buf.value
                except Exception:
                    pass
                drives.append({"path": root, "label": label or "Local Disk",
                               "total": u.total, "used": u.used, "free": u.free})
    else:
        u = shutil.disk_usage("/")
        drives.append({"path": "/", "label": "Root", "total": u.total, "used": u.used, "free": u.free})
        home = os.path.expanduser("~")
        drives.append({"path": home, "label": "Home", "total": u.total, "used": u.used, "free": u.free})
    return drives


def list_dir(path):
    path = os.path.abspath(path)
    node = SCANNER.find(path)
    items = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    is_link = e.is_symlink()
                    s = e.stat(follow_symlinks=False)
                    reparse = IS_WIN and getattr(s, "st_file_attributes", 0) & REPARSE
                    is_dir = e.is_dir(follow_symlinks=False)
                    item = {"name": e.name, "path": e.path, "type": "dir" if is_dir else "file",
                            "mtime": s.st_mtime, "atime": s.st_atime,
                            "link": bool(is_link or reparse)}
                    if is_dir:
                        child = node.children.get(key_of(e.name)) if node and node.children else None
                        if child is not None:
                            item.update(size=child.size, files=child.files, dirs=child.dirs, scanned=True,
                                        kids=top_children(child, 12))
                        else:
                            item.update(size=None, scanned=False)
                    else:
                        item.update(size=s.st_size, category=category_of(e.name), scanned=True)
                    items.append(item)
                except OSError:
                    items.append({"name": e.name, "path": e.path, "type": "locked", "size": None})
    except PermissionError:
        return {"path": path, "error": "Access denied - try running StorageKeeper as administrator.",
                "items": [], "parent": os.path.dirname(path)}
    except OSError as ex:
        return {"path": path, "error": str(ex), "items": [], "parent": os.path.dirname(path)}
    parent = os.path.dirname(path.rstrip("\\/")) if path.rstrip("\\/") else None
    if IS_WIN and len(path) <= 3:
        parent = None
    return {"path": path, "parent": parent if parent and parent != path else None, "items": items,
            "scanned": node is not None, "total": node.size if node else None,
            "usage": disk_usage_safe(path)}


def top_children(node, n):
    if not node.children:
        return []
    kids = sorted(node.children.values(), key=lambda c: c.size, reverse=True)[:n]
    return [{"name": c.name, "size": c.size} for c in kids if c.size > 0]


def disk_usage_safe(path):
    try:
        u = shutil.disk_usage(path)
        return {"total": u.total, "used": u.used, "free": u.free}
    except OSError:
        return None


def folder_size(path, limit_s=60):
    total, count, t0 = 0, 0, time.time()
    stack = [path]
    while stack and time.time() - t0 < limit_s:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        s = e.stat(follow_symlinks=False)
                        if IS_WIN and getattr(s, "st_file_attributes", 0) & REPARSE:
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += s.st_size
                            count += 1
                    except OSError:
                        pass
        except OSError:
            pass
    return total, count, bool(stack)


# --------------------------------------------------------------------------
# Windows registry helpers
# --------------------------------------------------------------------------

def reg_values(hive, subkey, wow=0):
    out = {}
    if not IS_WIN:
        return out
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | wow) as k:
            i = 0
            while True:
                try:
                    name, val, typ = winreg.EnumValue(k, i)
                except OSError:
                    break
                out[name] = val
                i += 1
    except OSError:
        pass
    return out


def reg_subkeys(hive, subkey, wow=0):
    out = []
    if not IS_WIN:
        return out
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | wow) as k:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    return out


def filetime_to_unix(ft):
    if not ft:
        return None
    t = ft / 10_000_000 - 11644473600
    return t if t > 0 else None


KNOWN_FOLDERS = {
    "{6D809377-6AF0-444B-8957-A3773F02200E}": os.environ.get("ProgramW6432", r"C:\Program Files"),
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": os.environ.get("SystemRoot", r"C:\Windows"),
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
    "{D65231B0-B2F1-4857-A4CE-A8E7C6EA7D27}": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "SysWOW64"),
    "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}": os.environ.get("LOCALAPPDATA", ""),
    "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}": os.environ.get("APPDATA", ""),
    "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}": os.environ.get("ProgramData", r"C:\ProgramData"),
}


def userassist_entries():
    """Last-run times Windows records for programs you launch (HKCU UserAssist)."""
    entries = {}
    if not IS_WIN:
        return entries
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
    for guid in reg_subkeys(winreg.HKEY_CURRENT_USER, base):
        vals = reg_values(winreg.HKEY_CURRENT_USER, base + "\\" + guid + "\\Count")
        for name, data in vals.items():
            try:
                path = codecs.decode(name, "rot_13")
            except Exception:
                continue
            if not isinstance(data, (bytes, bytearray)) or len(data) < 68:
                continue
            ft = struct.unpack_from("<Q", data, 60)[0]
            t = filetime_to_unix(ft)
            if not t:
                continue
            for g, real in KNOWN_FOLDERS.items():
                if path.upper().startswith(g) and real:
                    path = real + path[len(g):]
                    break
            key = path.lower()
            if key not in entries or entries[key] < t:
                entries[key] = t
    return entries


def prefetch_entries():
    """Prefetch files are touched each time a program runs (needs admin to read)."""
    out = {}
    pf = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Prefetch")
    try:
        with os.scandir(pf) as it:
            for e in it:
                if e.name.lower().endswith(".pf") and "-" in e.name:
                    exe = e.name.rsplit("-", 1)[0].lower()
                    t = e.stat().st_mtime
                    if out.get(exe, 0) < t:
                        out[exe] = t
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# Installed applications
# --------------------------------------------------------------------------

def clean_exe_path(s):
    if not s:
        return ""
    s = s.strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        s = s[1:end] if end > 0 else s[1:]
    else:
        low = s.lower()
        i = low.find(".exe")
        if i > 0:
            s = s[:i + 4]
        s = s.split(",")[0]
    return os.path.expandvars(s.strip())


def exes_in(folder, max_depth=2, cap=60):
    found = []
    stack = [(folder, 0)]
    while stack and len(found) < cap:
        p, d = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if d < max_depth:
                                stack.append((e.path, d + 1))
                        elif e.name.lower().endswith(".exe"):
                            found.append((e.path, e.stat().st_atime))
                    except OSError:
                        pass
        except OSError:
            pass
    return found


APPS_CACHE = {"t": 0, "data": None}


def get_apps():
    if APPS_CACHE["data"] is not None and time.time() - APPS_CACHE["t"] < 120:
        return APPS_CACHE["data"]
    apps = []
    if not IS_WIN:
        APPS_CACHE.update(t=time.time(), data={"apps": [], "note": "Installed-app list is Windows only."})
        return APPS_CACHE["data"]
    ua = userassist_entries()
    pf = prefetch_entries()
    seen = set()
    sources = [
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    ]
    for hive, base, wow in sources:
        for sub in reg_subkeys(hive, base, wow):
            v = reg_values(hive, base + "\\" + sub, wow)
            name = v.get("DisplayName")
            if not name or v.get("SystemComponent") == 1 or v.get("ParentKeyName"):
                continue
            if str(v.get("ReleaseType", "")).lower() in ("update", "hotfix", "security update"):
                continue
            dedupe = (name.lower(), str(v.get("DisplayVersion", "")))
            if dedupe in seen:
                continue
            seen.add(dedupe)
            loc = os.path.expandvars(str(v.get("InstallLocation") or "")).strip().strip('"').rstrip("\\")
            icon = clean_exe_path(str(v.get("DisplayIcon") or ""))
            if not loc and icon.lower().endswith(".exe") and os.path.isfile(icon):
                loc = os.path.dirname(icon)
            if loc and not os.path.isdir(loc):
                loc = ""
            size = None
            size_src = None
            if loc:
                node = SCANNER.find(loc)
                if node is not None and node.size:
                    size, size_src = node.size, "scanned"
            if size is None and v.get("EstimatedSize"):
                try:
                    size, size_src = int(v["EstimatedSize"]) * 1024, "reported"
                except (TypeError, ValueError):
                    pass
            # --- last used ---
            last, last_src = None, None
            exes = []
            if loc:
                exes = exes_in(loc)
            elif icon.lower().endswith(".exe"):
                try:
                    exes = [(icon, os.stat(icon).st_atime)]
                except OSError:
                    pass
            if loc:
                lp = loc.lower() + "\\"
                for p, t in ua.items():
                    if p.startswith(lp) and (last is None or t > last):
                        last, last_src = t, "Windows launch history"
            for p, _ in exes:
                t = ua.get(p.lower())
                if t and (last is None or t > last):
                    last, last_src = t, "Windows launch history"
                t = pf.get(os.path.basename(p).lower())
                if t and (last is None or t > last):
                    last, last_src = t, "Prefetch"
            if last is None and exes:
                at = max(t for _, t in exes)
                if at:
                    last, last_src = at, "file access time (estimate)"
            install_date = None
            d = str(v.get("InstallDate") or "")
            if len(d) == 8 and d.isdigit():
                try:
                    install_date = datetime(int(d[:4]), int(d[4:6]), int(d[6:])).timestamp()
                except ValueError:
                    pass
            apps.append({"name": name, "version": v.get("DisplayVersion", ""),
                         "publisher": v.get("Publisher", ""), "location": loc,
                         "size": size, "sizeSource": size_src, "lastUsed": last,
                         "lastUsedSource": last_src, "installed": install_date,
                         "scope": "Current user" if hive == winreg.HKEY_CURRENT_USER else "All users"})
    APPS_CACHE.update(t=time.time(), data={"apps": apps, "prefetch": bool(pf)})
    return APPS_CACHE["data"]


# --------------------------------------------------------------------------
# Startup programs
# --------------------------------------------------------------------------

def get_startup():
    items = []
    if not IS_WIN:
        auto = os.path.expanduser("~/.config/autostart")
        try:
            for f in os.listdir(auto):
                items.append({"name": f, "command": os.path.join(auto, f), "location": "~/.config/autostart",
                              "enabled": True, "scope": "Current user"})
        except OSError:
            pass
        return {"items": items}
    HK, HL = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
    approved_base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"

    def approved(hive, sub, name):
        data = reg_values(hive, approved_base + "\\" + sub).get(name)
        if isinstance(data, (bytes, bytearray)) and data:
            return not (data[0] & 1)
        return True

    runs = [
        (HK, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, "Run", "Current user", "Registry (HKCU Run)"),
        (HL, r"Software\Microsoft\Windows\CurrentVersion\Run", winreg.KEY_WOW64_64KEY, "Run", "All users", "Registry (HKLM Run)"),
        (HL, r"Software\Microsoft\Windows\CurrentVersion\Run", winreg.KEY_WOW64_32KEY, "Run32", "All users", "Registry (HKLM Run, 32-bit)"),
    ]
    seen = set()
    for hive, key, wow, appr, scope, loc in runs:
        for name, cmd in reg_values(hive, key, wow).items():
            sig = (name.lower(), str(cmd).lower())
            if sig in seen:
                continue
            seen.add(sig)
            items.append({"name": name, "command": str(cmd), "location": loc, "scope": scope,
                          "enabled": approved(hive, appr, name), "exe": clean_exe_path(str(cmd))})
    folders = [
        (os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup"), HK, "Current user"),
        (os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs\StartUp"), HL, "All users"),
    ]
    for folder, hive, scope in folders:
        try:
            for f in os.listdir(folder):
                if f.lower() == "desktop.ini":
                    continue
                items.append({"name": os.path.splitext(f)[0], "command": os.path.join(folder, f),
                              "location": "Startup folder", "scope": scope,
                              "enabled": approved(hive, "StartupFolder", f), "exe": os.path.join(folder, f)})
        except OSError:
            pass
    for it in items:
        try:
            it["size"] = os.path.getsize(it["exe"]) if it.get("exe") and os.path.isfile(it["exe"]) else None
        except OSError:
            it["size"] = None
    return {"items": items}


# --------------------------------------------------------------------------
# Running processes
# --------------------------------------------------------------------------
PS_SCRIPT = r"""
$ErrorActionPreference='SilentlyContinue'
Get-Process | ForEach-Object {
  $st = $null; try { $st = $_.StartTime.ToString('o') } catch {}
  [pscustomobject]@{ n=$_.ProcessName; id=$_.Id; mem=$_.WorkingSet64; cpu=$_.CPU;
    path=$_.Path; title=$_.MainWindowTitle; desc=$_.Description; start=$st }
} | ConvertTo-Json -Compress
"""


def _psutil_procs():
    import psutil  # optional dependency
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_times", "exe", "create_time"]):
        i = p.info
        procs.append({"name": i.get("name"), "pid": i.get("pid"),
                      "mem": i["memory_info"].rss if i.get("memory_info") else 0,
                      "cpu": sum(i["cpu_times"][:2]) if i.get("cpu_times") else None,
                      "path": i.get("exe") or "", "title": "", "desc": "",
                      "start": i.get("create_time")})
    return procs


def get_processes():
    if IS_WIN:
        # 1) PowerShell: includes window titles + friendly descriptions
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT],
                                 capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW)
            data = json.loads(out.stdout or "[]")
            if isinstance(data, dict):
                data = [data]
            procs = []
            for d in data:
                st = None
                if d.get("start"):
                    try:
                        st = datetime.fromisoformat(d["start"][:26]).timestamp()
                    except ValueError:
                        pass
                procs.append({"name": d.get("n"), "pid": d.get("id"), "mem": d.get("mem") or 0,
                              "cpu": d.get("cpu"), "path": d.get("path") or "",
                              "title": d.get("title") or "", "desc": d.get("desc") or "", "start": st})
            if procs:
                return {"procs": procs, "source": "powershell"}
        except Exception:
            pass
    # 2) psutil
    try:
        return {"procs": _psutil_procs(), "source": "psutil"}
    except Exception:
        pass
    # 3) tasklist (Windows) / /proc (Linux)
    procs = []
    if IS_WIN:
        try:
            import csv
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                                 timeout=20, creationflags=NO_WINDOW)
            for row in csv.reader(out.stdout.splitlines()):
                if len(row) >= 5:
                    mem = int("".join(ch for ch in row[4] if ch.isdigit()) or 0) * 1024
                    procs.append({"name": row[0], "pid": int(row[1]), "mem": mem, "cpu": None,
                                  "path": "", "title": "", "desc": "", "start": None})
            return {"procs": procs, "source": "tasklist"}
        except Exception as ex:
            return {"procs": [], "error": str(ex)}
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/status") as f:
                    st = dict(l.split(":", 1) for l in f if ":" in l)
                rss = int(st.get("VmRSS", "0 kB").split()[0]) * 1024
                procs.append({"name": st["Name"].strip(), "pid": int(pid), "mem": rss, "cpu": None,
                              "path": "", "title": "", "desc": "", "start": None})
            except Exception:
                pass
    return {"procs": procs, "source": "proc"}


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

def reveal(path):
    if IS_WIN:
        if os.path.isdir(path):
            subprocess.Popen(["explorer", os.path.normpath(path)])
        else:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])


def status_payload():
    st = dict(SCANNER.status)
    st["elapsed"] = (st["finished"] or time.time()) - st["started"] if st["started"] else 0
    st["scannedRoots"] = list(SCANNER.root_paths.values())
    return st


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self.send_json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "ui.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/info":
                self.send_json({"platform": sys.platform, "windows": IS_WIN,
                                "home": os.path.expanduser("~"), "admin": is_admin(),
                                "computer": os.environ.get("COMPUTERNAME") or os.uname().nodename if not IS_WIN else os.environ.get("COMPUTERNAME", "PC")})
            elif u.path == "/api/drives":
                self.send_json(get_drives())
            elif u.path == "/api/list":
                self.send_json(list_dir(q.get("path") or os.path.expanduser("~")))
            elif u.path == "/api/status":
                self.send_json(status_payload())
            elif u.path == "/api/top":
                n = int(q.get("n", 200))
                top = sorted(SCANNER.top, reverse=True)[:n]
                self.send_json([{"size": s, "path": p, "name": os.path.basename(p), "mtime": m, "atime": a,
                                 "category": category_of(p)} for s, p, m, a in top])
            elif u.path == "/api/types":
                cats = sorted(([k, v[0], v[1]] for k, v in SCANNER.cats.items()), key=lambda x: -x[1])
                exts = sorted(([k, v[0], v[1]] for k, v in SCANNER.exts.items()), key=lambda x: -x[1])[:40]
                self.send_json({"categories": cats, "extensions": exts})
            elif u.path == "/api/apps":
                if q.get("refresh"):
                    APPS_CACHE["data"] = None
                self.send_json(get_apps())
            elif u.path == "/api/startup":
                self.send_json(get_startup())
            elif u.path == "/api/processes":
                self.send_json(get_processes())
            elif u.path == "/api/foldersize":
                total, count, partial = folder_size(q["path"])
                self.send_json({"size": total, "files": count, "partial": partial})
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as ex:
            self.send_json({"error": str(ex)}, 500)

    def do_POST(self):
        if not self._host_ok():
            return self.send_json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/api/scan":
            roots = body.get("roots") or [d["path"] for d in get_drives()]
            ok = SCANNER.start(roots)
            APPS_CACHE["data"] = None
            self.send_json({"started": ok})
        elif u.path == "/api/cancel":
            SCANNER.cancel()
            self.send_json({"ok": True})
        elif u.path == "/api/reveal":
            try:
                reveal(body["path"])
                self.send_json({"ok": True})
            except Exception as ex:
                self.send_json({"error": str(ex)}, 500)
        elif u.path == "/api/taskmgr":
            if IS_WIN:
                subprocess.Popen(["taskmgr", "/0", "/" + body.get("tab", "startup")])
            self.send_json({"ok": IS_WIN})
        else:
            self.send_json({"error": "not found"}, 404)


def is_admin():
    if IS_WIN:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if "--server" in sys.argv:  # dev mode: engine only
        print("StorageKeeper engine:", url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    try:
        import webview  # pywebview -> native desktop window
    except ImportError:
        print("pywebview not installed - opening in your browser instead:", url)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    webview.create_window("StorageKeeper", url, width=1400, height=900,
                          min_size=(1000, 650), background_color="#05060f")
    webview.start()


if __name__ == "__main__":
    main()
