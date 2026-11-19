# 🪐 StorageKeeper

**See everything that takes up space on your PC, in a space-themed desktop app.**

![icon](assets/icon.png)

## Features

| Section | What it shows |
|---|---|
| **Command Deck** | Every drive shown as a planet with a usage ring. One-click **Scan entire PC**, space used by file type (videos, games, images…), and your biggest files. |
| **Star Map** | Browse **every folder and file** on your computer with exact sizes, % share, last-opened and modified dates, and an interactive sunburst map. Click to fly into any folder. |
| **Giants** | The 500 largest files on your PC, filterable by type. |
| **App Fleet** | Every installed application with its size, the **date it was last used**, its install date, and flags for apps you haven't used in 6+ months. |
| **Launch Sequence** | Every program that starts with Windows, and whether it's enabled or disabled. |
| **Live Telemetry** | Every application and process running right now, with live RAM usage. |

StorageKeeper is **read-only**. It never deletes or changes anything. The 👁 button opens any item in File Explorer so you can decide for yourself. Nothing leaves your computer.

## Download & install

1. Go to the **Releases** page of this repo (right-hand side of the repo page).
2. Download **`StorageKeeper.exe`** and double-click it.
3. Windows may show *"Windows protected your PC"* because the app isn't code-signed. Click **More info → Run anyway**.

**Tip:** right-click → **Run as administrator** to scan protected system folders and get more accurate "last used" dates.

## How the .exe gets built (GitHub Actions)

`.github/workflows/build.yml` builds the app automatically on GitHub's Windows servers:

- **Every push to `main`:** open the **Actions** tab, pick the latest run, and download **StorageKeeper-Windows** under *Artifacts*.
- **Every release:** go to **Releases → Draft a new release**, create a tag like `v1.0.0`, and publish it. About 3 minutes later `StorageKeeper.exe` is attached to that release for anyone to download.

## Run from source (optional)

You'll need Python 3.10+ from python.org. Double-click `run-from-source.bat`, or run:

```
pip install -r requirements.txt
python storagekeeper.py
```

## How "last used" is worked out

StorageKeeper checks, in this order:
1. **Windows launch history** (UserAssist), which records when you started programs from the Start menu, taskbar or desktop.
2. **Windows Prefetch**, which is updated every time a program runs. This needs administrator rights.
3. **File access time** of the app's .exe, used as an estimate when nothing else is available.

The source of each date is shown under it in the App Fleet.

## Tech

Python (standard library + `pywebview` + `psutil`). The UI is HTML/CSS/JS rendered in a native window with Microsoft Edge WebView2, which is built into Windows 10/11. It's packaged into one `.exe` with PyInstaller.
