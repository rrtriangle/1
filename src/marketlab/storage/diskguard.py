"""저장공간 관리: 예산 계산, 위험 판정, 외장 SSD 권고, 쓰기 가능 여부 확인.

원칙
- 내장 SSD 여유 공간은 항상 min_free_gb(기본 20GB) 이상 유지한다.
- 프로젝트 추가 사용 예산은 project_budget_gb(기본 30GB)이며,
  실제 여유가 기준(60GB)보다 적으면 비례 축소한다.
- 위험 상태이면 신규 대용량 다운로드를 멈추고 reports/storage_advice.md 를 만든다.
- 사용자 승인 없이 어떤 파일도 삭제하지 않는다.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import Settings, get_settings

GB = 1024 ** 3


class StorageUnavailable(RuntimeError):
    """저장장치가 분리되었거나 쓰기 권한이 없음. 즉시 쓰기를 중단해야 한다."""


class StorageRisk(RuntimeError):
    """예산 초과 또는 여유 공간 부족 예상."""

    def __init__(self, assessment: "Assessment"):
        super().__init__("; ".join(assessment.reasons))
        self.assessment = assessment


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _existing_parent(path: Path) -> Path:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def is_external_volume(path: Path) -> bool:
    """macOS: /Volumes/<이름> 아래이면서 부팅 볼륨이 아닌 경우 외장으로 본다."""
    rp = Path(os.path.realpath(_existing_parent(path)))
    parts = rp.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        boot = Path("/").resolve()
        try:
            return os.stat(rp).st_dev != os.stat(boot).st_dev
        except OSError:
            return True
    return False


def ensure_writable(path: Path, settings: Settings | None = None) -> None:
    """데이터 디렉터리의 마운트·쓰기 가능 여부를 확인한다. 실패 시 StorageUnavailable."""
    s = settings or get_settings()
    rp = Path(os.path.expanduser(str(path)))
    parts = Path(os.path.realpath(rp)).parts if rp.exists() else rp.parts
    if s.get("storage.require_mounted_volume", True) and len(parts) >= 3 and parts[1] == "Volumes":
        vol = Path("/", parts[1], parts[2])
        if not vol.exists() or not os.path.ismount(vol):
            raise StorageUnavailable(f"외장 저장장치가 연결되어 있지 않음: {vol}")
    try:
        rp.mkdir(parents=True, exist_ok=True)
        probe = rp / f".write_probe_{os.getpid()}"
        with open(probe, "wb") as fh:
            fh.write(b"ok")
            fh.flush()
            os.fsync(fh.fileno())
        probe.unlink()
    except OSError as e:
        raise StorageUnavailable(f"데이터 디렉터리에 쓸 수 없음: {rp} ({e})") from e


@dataclass
class Assessment:
    data_dir: str
    external: bool
    disk_total_gb: float
    disk_free_gb: float
    project_used_gb: float
    budget_gb: float
    min_free_gb: float
    planned_additional_gb: float
    peak_temp_gb: float
    free_after_gb: float
    project_after_gb: float
    risk: bool
    reasons: list[str] = field(default_factory=list)
    recommendation: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def project_usage_bytes(settings: Settings | None = None) -> int:
    s = settings or get_settings()
    seen: set[str] = set()
    total = 0
    for p in (s.paths.data, s.paths.logs, s.paths.reports, s.paths.models):
        rp = os.path.realpath(p)
        if rp in seen:
            continue
        seen.add(rp)
        total += dir_size(Path(rp))
    return total


def effective_budget_gb(free_gb: float, used_gb: float, external: bool, settings: Settings) -> float:
    budget = float(settings.get("storage.project_budget_gb", 30))
    baseline = float(settings.get("storage.assumed_free_gb_baseline", 60))
    min_free = float(settings.get("storage.min_free_gb", 20))
    if external:
        # 외장 SSD: 내부 20GB 규칙 대신 장치 용량의 5% 여유만 남긴다.
        return max(0.0, used_gb + free_gb * 0.95)
    if free_gb + used_gb < baseline:
        budget = budget * max(0.0, (free_gb + used_gb)) / baseline
    return max(0.0, min(budget, used_gb + free_gb - min_free))


def recommend_ssd(total_needed_gb: float) -> str:
    """실측 필요량 기준 외장 SSD 권고. 향후 머신러닝 산출물 여유 2배를 고려한다."""
    with_growth = total_needed_gb * 2.0
    if with_growth <= 400:
        return "500GB"
    return "1TB"


def assess(planned_additional_bytes: int = 0, peak_temp_bytes: int = 0,
           settings: Settings | None = None) -> Assessment:
    s = settings or get_settings()
    data_dir = s.paths.data
    external = is_external_volume(data_dir)
    usage = shutil.disk_usage(_existing_parent(data_dir))
    free_gb = usage.free / GB
    used_gb = project_usage_bytes(s) / GB
    min_free = float(s.get("storage.min_free_gb", 20))
    budget = effective_budget_gb(free_gb, used_gb, external, s)
    add_gb = planned_additional_bytes / GB
    temp_gb = peak_temp_bytes / GB
    free_after = free_gb - add_gb
    project_after = used_gb + add_gb
    reasons: list[str] = []
    if project_after > budget:
        reasons.append(f"예상 프로젝트 사용량 {project_after:.1f}GB 가 예산 {budget:.1f}GB 를 초과")
    if not external and free_after - temp_gb < min_free:
        reasons.append(f"작업 중/후 내장 SSD 여유가 {free_after - temp_gb:.1f}GB 로 최소 {min_free:.0f}GB 미만이 될 것으로 예상")
    if external and free_after - temp_gb < usage.total / GB * 0.05:
        reasons.append("외장 저장장치 여유 공간 부족 예상")
    risk = bool(reasons)
    rec = ""
    if risk:
        rec = recommend_ssd(project_after + temp_gb)
    return Assessment(
        data_dir=str(data_dir), external=external, disk_total_gb=usage.total / GB,
        disk_free_gb=free_gb, project_used_gb=used_gb, budget_gb=budget, min_free_gb=min_free,
        planned_additional_gb=add_gb, peak_temp_gb=temp_gb, free_after_gb=free_after,
        project_after_gb=project_after, risk=risk, reasons=reasons, recommendation=rec,
    )


def notify_macos(title: str, message: str) -> bool:
    """실행 중일 때만 가능한 macOS 알림. 실패해도 예외를 던지지 않는다."""
    if platform.system() != "Darwin" or not shutil.which("osascript"):
        return False
    msg = message.replace('"', "'")[:240]
    ttl = title.replace('"', "'")[:80]
    try:
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{ttl}"'],
                       check=False, timeout=10, capture_output=True)
        return True
    except Exception:
        return False


def write_storage_advice(a: Assessment, context: str, settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    s.paths.reports.mkdir(parents=True, exist_ok=True)
    path = s.paths.reports / "storage_advice.md"
    need_ssd = a.risk
    lines = [
        "# 저장공간 경고 및 외장 SSD 권고",
        "",
        f"- 작성 시각: {datetime.now().isoformat(timespec='seconds')}",
        f"- 발생 작업: {context}",
        f"- 데이터 경로: `{a.data_dir}` ({'외장' if a.external else '내장'} 저장장치)",
        f"- 디스크 전체 {a.disk_total_gb:.1f}GB / 현재 여유 {a.disk_free_gb:.1f}GB",
        f"- 현재 프로젝트 사용량 {a.project_used_gb:.2f}GB / 유효 예산 {a.budget_gb:.1f}GB",
        f"- 계획된 추가 사용량 {a.planned_additional_gb:.2f}GB, 최대 임시공간 {a.peak_temp_gb:.2f}GB",
        f"- 작업 후 예상 여유 {a.free_after_gb:.1f}GB, 프로젝트 예상 총량 {a.project_after_gb:.2f}GB",
        "",
        "## 판정",
        "",
    ]
    if need_ssd:
        lines += [f"- 위험: {r}" for r in a.reasons]
        lines += [
            "",
            "신규 대용량 다운로드를 일시 중단했습니다. 지금까지 수집한 데이터와 진행 상태는 저장되어 있어",
            "공간을 확보하거나 데이터 경로를 옮긴 뒤 같은 명령으로 이어서 재개할 수 있습니다.",
            "",
            "## 권고",
            "",
            f"- 외장 SSD 필요: 예 (권장 용량 **{a.recommendation}**)",
            f"- 최소 필요 여유 공간: 약 {a.project_after_gb + a.peak_temp_gb:.0f}GB (+ 향후 머신러닝 산출물 여유)",
            "- 외장 SSD 연결 후 `.env` 의 `MARKETLAB_DATA_DIR` 를 외장 경로로 바꾸고",
            "  `./scripts/move_data.command <새 경로>` 로 기존 데이터를 옮기세요 (소스코드 수정 불필요).",
            "- 프로그램은 사용자 승인 없이 파일을 삭제하거나 저장장치를 구매하지 않습니다.",
        ]
    else:
        lines += ["- 현재 계획은 예산과 최소 여유 공간 조건을 만족합니다. 외장 SSD 구매가 필요하지 않습니다."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def raise_alert(level: str, kind: str, message: str) -> None:
    """보류 경고를 카탈로그에 남긴다(다음 실행·대시보드에서 다시 표시)."""
    from .catalog import catalog

    try:
        with catalog() as con:
            con.execute("INSERT INTO pending_alerts VALUES (now(), ?, ?, ?, FALSE)", [level, kind, message])
    except Exception:
        pass
    notify_macos("market-research-lab", message)


class DiskGuard:
    """대용량 작업 중 주기적으로 호출해 공간·쓰기 가능 여부를 확인한다."""

    def __init__(self, context: str, planned_additional_bytes: int, peak_temp_bytes: int = 0,
                 settings: Settings | None = None):
        self.s = settings or get_settings()
        self.context = context
        self.planned = planned_additional_bytes
        self.peak_temp = peak_temp_bytes
        self.start_used = project_usage_bytes(self.s)
        self.growth_factor = float(self.s.get("storage.growth_alert_factor", 1.5))

    def preflight(self) -> Assessment:
        ensure_writable(self.s.paths.data, self.s)
        a = assess(self.planned, self.peak_temp, self.s)
        if a.risk:
            write_storage_advice(a, self.context, self.s)
            raise_alert("error", "storage", f"[{self.context}] 저장공간 위험: {'; '.join(a.reasons)}")
            raise StorageRisk(a)
        return a

    def checkpoint(self, written_so_far: int = 0) -> None:
        """쓰기 직전에 호출. 장치 분리·공간 부족·급격한 증가 시 예외."""
        ensure_writable(self.s.paths.data, self.s)
        remaining = max(0, self.planned - written_so_far)
        a = assess(remaining, self.peak_temp, self.s)
        grown = project_usage_bytes(self.s) - self.start_used
        if self.planned > 0 and grown > self.planned * self.growth_factor + 512 * 1024 ** 2:
            a.risk = True
            a.reasons.append(f"디스크 사용량이 예상보다 급격히 증가 ({grown / GB:.2f}GB > 계획 {self.planned / GB:.2f}GB)")
            a.recommendation = a.recommendation or recommend_ssd(a.project_after_gb)
        if a.risk:
            write_storage_advice(a, self.context, self.s)
            raise_alert("error", "storage", f"[{self.context}] 저장공간 위험으로 중단: {'; '.join(a.reasons)}")
            raise StorageRisk(a)
