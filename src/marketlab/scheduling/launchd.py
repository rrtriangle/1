"""macOS launchd 자동 갱신.

동작 방식 (정직한 설명)
- launchd 는 Mac 이 켜져 있고 사용자가 로그인한 동안에만 작업을 실행한다. 클라우드처럼 항상 돌지 않는다.
- StartCalendarInterval 시각에 Mac 이 잠자기 중이면, 깨어난 직후 놓친 실행을 한 번 수행한다(launchd 기본 동작).
  전원이 꺼져 있었다면 실행되지 않으며, 다음 로그인 시 RunAtLoad 로 한 번 실행되어 밀린 갱신을 복구한다.
- 네트워크가 없으면 갱신을 건너뛰고 다음 예정 시각에 다시 시도한다.
- 중복 실행은 파일 잠금으로 막는다.
"""
from __future__ import annotations

import os
import platform
import plistlib
import subprocess
import sys
from pathlib import Path

from ..config import get_settings


def plist_path() -> Path:
    label = get_settings().get("schedule.label", "com.marketlab.update")
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def build_plist(times: list[str] | None = None) -> dict:
    s = get_settings()
    times = times or s.get("schedule.times", ["07:30", "19:30"])
    intervals = []
    for t in times:
        hh, mm = map(int, t.split(":"))
        intervals.append({"Hour": hh, "Minute": mm})
    root = s.paths.root
    py = Path(sys.executable)
    logs = s.paths.logs
    logs.mkdir(parents=True, exist_ok=True)
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"}
    if os.environ.get("MARKETLAB_DATA_DIR"):
        env["MARKETLAB_DATA_DIR"] = os.environ["MARKETLAB_DATA_DIR"]
    return {
        "Label": s.get("schedule.label", "com.marketlab.update"),
        "ProgramArguments": [str(py), "-m", "marketlab.cli.main", "update", "--all", "--scheduled"],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": intervals,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
        "EnvironmentVariables": env,
    }


def install(times: list[str] | None = None) -> str:
    if platform.system() != "Darwin":
        raise RuntimeError("launchd 는 macOS 에서만 사용할 수 있습니다.")
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(p)], capture_output=True)
    with open(p, "wb") as fh:
        plistlib.dump(build_plist(times), fh)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap 실패: {r.stderr.strip()}")
    return str(p)


def uninstall() -> str:
    p = plist_path()
    if platform.system() == "Darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)], capture_output=True)
    if p.exists():
        p.unlink()
    return str(p)


def status() -> dict:
    p = plist_path()
    out = {"plist": str(p), "installed": p.exists()}
    if p.exists():
        with open(p, "rb") as fh:
            pl = plistlib.load(fh)
        out["times"] = [f"{i['Hour']:02d}:{i['Minute']:02d}" for i in pl.get("StartCalendarInterval", [])]
    if platform.system() == "Darwin":
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{get_settings().get('schedule.label')}"],
                           capture_output=True, text=True)
        out["loaded"] = r.returncode == 0
        for line in r.stdout.splitlines():
            if "last exit code" in line or "state =" in line:
                out.setdefault("launchctl", []).append(line.strip())
    return out
