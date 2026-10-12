from marketlab.storage import diskguard as dg


def test_budget_shrinks_when_free_space_small(isolated):
    s = isolated
    # 여유 40GB(기준 60GB 미만) → 30GB × 40/60 = 20GB, 그리고 40-20=20GB 상한
    assert abs(dg.effective_budget_gb(40, 0, False, s) - 20.0) < 1e-9
    # 여유 25GB → 최소 여유 20GB 를 남겨야 하므로 5GB
    assert abs(dg.effective_budget_gb(25, 0, False, s) - 5.0) < 1e-9
    # 충분하면 30GB
    assert dg.effective_budget_gb(200, 0, False, s) == 30.0


def test_risk_when_plan_exceeds_budget(isolated):
    a = dg.assess(planned_additional_bytes=10_000 * dg.GB)
    assert a.risk and a.recommendation in ("500GB", "1TB")
    p = dg.write_storage_advice(a, "test")
    assert "외장 SSD 필요: 예" in p.read_text(encoding="utf-8")


def test_no_risk_for_small_plan(isolated):
    a = dg.assess(planned_additional_bytes=1024)
    if a.disk_free_gb > a.min_free_gb + 1:
        assert not a.risk


def test_ssd_recommendation_by_measured_need():
    assert dg.recommend_ssd(150) == "500GB"
    assert dg.recommend_ssd(260) == "1TB"


def test_unwritable_dir_raises(isolated, tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    import os
    if os.geteuid() == 0:   # root 는 권한 무시 → 마운트 검사로 대체
        import pytest
        with pytest.raises(dg.StorageUnavailable):
            dg.ensure_writable(__import__("pathlib").Path("/Volumes/__marketlab_missing__/data"))
        return
    import pytest
    with pytest.raises(dg.StorageUnavailable):
        dg.ensure_writable(ro / "sub")


def test_guard_preflight_blocks_and_records_alert(isolated):
    import pytest
    from marketlab.storage.catalog import catalog
    g = dg.DiskGuard("test-large", planned_additional_bytes=50_000 * dg.GB)
    with pytest.raises(dg.StorageRisk):
        g.preflight()
    with catalog(read_only=True) as con:
        assert con.execute("SELECT count(*) FROM pending_alerts WHERE kind='storage'").fetchone()[0] >= 1
