from __future__ import annotations

from app.hydro import comparison as engine


def _setup_inversion_runs(client, well, *, candidate_endmembers=None, model_v2="mix-2"):
    e1 = client.post("/api/hydro/endmembers", json={"name": "山区降水", "isotope_d18o": -10, "isotope_d2h": -70, "solute_mg_l": 10, "uncertainty": 0.1, "version": "v1"}).json()
    e2 = client.post("/api/hydro/endmembers", json={"name": "河流渗漏", "isotope_d18o": -5, "isotope_d2h": -35, "solute_mg_l": 50, "uncertainty": 0.2, "version": "v1"}).json()
    sample = client.post(f"/api/hydro/wells/{well['id']}/samples", json={"sample_code": "S-100", "sampled_at": "2026-09-24T08:00:00+00:00", "isotope_d18o": -7.5, "isotope_d2h": -52.5, "solute_mg_l": 30}).json()
    base = _run_inversion(client, sample, [e1["id"], e2["id"]], "mix-1")
    if candidate_endmembers is None:
        candidate_endmembers = [e1["id"], e2["id"]]
    cand = _run_inversion(client, sample, candidate_endmembers, model_v2)
    return sample, base, cand


def _run_inversion(client, sample, endmember_ids, model_version):
    task = client.post(
        f"/api/hydro/samples/{sample['id']}/inversions",
        json={"endmember_ids": endmember_ids, "max_iterations": 1000, "tolerance": 1e-10, "model_version": model_version},
    )
    assert task.status_code == 202, task.text
    done = client.post(f"/api/hydro/inversions/{task.json()['id']}/run?worker_id=test")
    assert done.status_code == 200, done.text
    return done.json()


def _create_well(client, code="W-100"):
    response = client.post("/api/hydro/wells", json={"code": code, "name": "比较监测井", "latitude": 35.1, "longitude": 116.2, "aquifer": "浅层", "screen_depth_m": 42})
    assert response.status_code == 201, response.text
    return response.json()


def test_inversion_comparison_endmember_alignment_and_metrics(client):
    well = _create_well(client)
    sample, base, cand = _setup_inversion_runs(client, well)

    response = client.post(
        "/api/hydro/comparisons/inversions",
        json={"baseline_task_id": base["id"], "candidate_task_id": cand["id"], "uncertainty_band": 0.05},
    )
    assert response.status_code == 201, response.text
    report = response.json()
    assert report["kind"] == "inversion"
    assert report["dataset_id"] == sample["id"]
    assert report["baseline"]["model_version"] == "mix-1"
    assert report["candidate"]["model_version"] == "mix-2"
    assert report["comparability"] == "comparable"
    assert report["incomparable"] == []
    assert len(report["input_fingerprint"]) == 64
    assert report["created_at"]

    fractions = report["details"]["fractions"]
    assert len(fractions) == 2
    # 明细按基线端元 id 升序稳定排列。
    baseline_ids = [row["endmember"]["baseline_endmember_id"] for row in fractions]
    assert baseline_ids == sorted(baseline_ids)
    for row in fractions:
        assert set(("absolute_difference", "signed_difference", "relative_difference")) <= set(row)
        overlap = row["interval_overlap"]
        assert overlap["overlaps"] is True
        assert overlap["jaccard"] == 1.0
    metrics = {row["metric"]: row for row in report["summary"]["metrics"]}
    assert "rmse" in metrics and "mass_balance" in metrics
    assert metrics["rmse"]["absolute_difference"] == 0.0
    assert report["details"]["convergence"]["baseline"]["converged"] is not None


def test_inversion_comparison_reuses_report_for_identical_request(client):
    well = _create_well(client, "W-101")
    _, base, cand = _setup_inversion_runs(client, well)
    payload = {"baseline_task_id": base["id"], "candidate_task_id": cand["id"], "uncertainty_band": 0.1}

    first = client.post("/api/hydro/comparisons/inversions", json=payload)
    assert first.status_code == 201
    second = client.post("/api/hydro/comparisons/inversions", json=payload)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["created_at"] == first.json()["created_at"]
    assert second.json()["input_fingerprint"] == first.json()["input_fingerprint"]

    # 参数顺序/空值不影响指纹与复用。
    reordered = {"candidate_task_id": cand["id"], "baseline_task_id": base["id"], "uncertainty_band": 0.1, "thresholds": {"rmse_max": None}}
    third = client.post("/api/hydro/comparisons/inversions", json=reordered)
    assert third.json()["id"] == first.json()["id"]

    # 不同阈值选项生成独立报告。
    other = client.post(
        "/api/hydro/comparisons/inversions",
        json={"baseline_task_id": base["id"], "candidate_task_id": cand["id"], "thresholds": {"rmse_max": 0.5}},
    )
    assert other.json()["id"] != first.json()["id"]


def test_inversion_comparison_flags_endmembers_present_on_one_side(client):
    well = _create_well(client, "W-102")
    e1 = client.post("/api/hydro/endmembers", json={"name": "山区降水", "isotope_d18o": -10, "isotope_d2h": -70, "solute_mg_l": 10, "uncertainty": 0.1, "version": "v1"}).json()
    e2 = client.post("/api/hydro/endmembers", json={"name": "河流渗漏", "isotope_d18o": -5, "isotope_d2h": -35, "solute_mg_l": 50, "uncertainty": 0.2, "version": "v1"}).json()
    extra = client.post("/api/hydro/endmembers", json={"name": "古地下水", "isotope_d18o": -12, "isotope_d2h": -85, "solute_mg_l": 120, "uncertainty": 0.3, "version": "v2"}).json()
    sample = client.post(f"/api/hydro/wells/{well['id']}/samples", json={"sample_code": "S-102", "sampled_at": "2026-09-24T08:00:00+00:00", "isotope_d18o": -7.5, "isotope_d2h": -52.5, "solute_mg_l": 30}).json()
    base = _run_inversion(client, sample, [e1["id"], e2["id"]], "mix-1")
    cand3 = _run_inversion(client, sample, [e1["id"], e2["id"], extra["id"]], "mix-3")

    response = client.post(
        "/api/hydro/comparisons/inversions",
        json={"baseline_task_id": base["id"], "candidate_task_id": cand3["id"]},
    )
    assert response.status_code == 201
    report = response.json()
    assert report["comparability"] == "partially_comparable"
    fields = {(item["field"], item["reason"]) for item in report["incomparable"]}
    assert any(reason == "endmember_only_in_candidate" for _, reason in fields)
    # 共同的两个端元仍然完成了数值比较。
    assert len(report["details"]["fractions"]) == 2


def test_inversion_comparison_requires_same_sample(client):
    well = _create_well(client, "W-103")
    _, base, _ = _setup_inversion_runs(client, well)
    other_sample = client.post(f"/api/hydro/wells/{well['id']}/samples", json={"sample_code": "S-101", "sampled_at": "2026-09-25T08:00:00+00:00", "isotope_d18o": -8, "isotope_d2h": -50, "solute_mg_l": 31}).json()
    e_ids = [1, 2]
    task = client.post(f"/api/hydro/samples/{other_sample['id']}/inversions", json={"endmember_ids": e_ids, "model_version": "mix-2"})
    cand = client.post(f"/api/hydro/inversions/{task.json()['id']}/run?worker_id=test").json()

    response = client.post("/api/hydro/comparisons/inversions", json={"baseline_task_id": base["id"], "candidate_task_id": cand["id"]})
    assert response.status_code == 422
    assert "同一数据集" in response.json()["detail"]


def test_inversion_comparison_flags_changed_endmember_definition(client):
    from app.database import get_connection

    well = _create_well(client, "W-105")
    e1 = client.post("/api/hydro/endmembers", json={"name": "山区降水", "isotope_d18o": -10, "isotope_d2h": -70, "solute_mg_l": 10, "uncertainty": 0.1, "version": "v1"}).json()
    e2 = client.post("/api/hydro/endmembers", json={"name": "河流渗漏", "isotope_d18o": -5, "isotope_d2h": -35, "solute_mg_l": 50, "uncertainty": 0.2, "version": "v1"}).json()
    sample = client.post(f"/api/hydro/wells/{well['id']}/samples", json={"sample_code": "S-105", "sampled_at": "2026-09-24T08:00:00+00:00", "isotope_d18o": -7.5, "isotope_d2h": -52.5, "solute_mg_l": 30}).json()
    base = _run_inversion(client, sample, [e1["id"], e2["id"]], "mix-1")

    # 基线入队后修改端元定义，候选任务快照到新的定义。
    get_connection().execute("UPDATE hydro_endmembers SET isotope_d18o=-8 WHERE id=?", (e1["id"],))
    cand = _run_inversion(client, sample, [e1["id"], e2["id"]], "mix-2")

    report = client.post(
        "/api/hydro/comparisons/inversions",
        json={"baseline_task_id": base["id"], "candidate_task_id": cand["id"]},
    ).json()
    assert report["comparability"] == "partially_comparable"
    flagged = [item for item in report["incomparable"] if item["reason"] == "endmember_definition_mismatch"]
    assert len(flagged) == 1
    assert flagged[0]["field"] == f"fraction.{e1['id']}"
    assert flagged[0]["detail"]["parameter"] == "isotope_d18o"


def test_compare_task_with_itself_and_missing_task(client):
    well = _create_well(client, "W-104")
    _, base, _ = _setup_inversion_runs(client, well)
    same = client.post("/api/hydro/comparisons/inversions", json={"baseline_task_id": base["id"], "candidate_task_id": base["id"]})
    assert same.status_code == 422
    missing = client.post("/api/hydro/comparisons/inversions", json={"baseline_task_id": base["id"], "candidate_task_id": 9999})
    assert missing.status_code == 404

def test_transport_comparison_unifies_time_axis_and_thresholds(client):
    well = _create_well(client, "W-200")
    base = client.post(f"/api/hydro/wells/{well['id']}/transport", json={"source_concentration": 100, "distance_m": 100, "velocity_m_day": 2, "dispersion_m2_day": 5, "decay_per_day": 0.01, "duration_days": 100, "step_days": 5, "model_version": "ade-1"}).json()
    # 更细的采样网格：物理参数一致，仅步长不同，峰值在离散点上会略有差异。
    cand = client.post(f"/api/hydro/wells/{well['id']}/transport", json={"source_concentration": 100, "distance_m": 100, "velocity_m_day": 2, "dispersion_m2_day": 5, "decay_per_day": 0.01, "duration_days": 100, "step_days": 2, "model_version": "ade-2"}).json()

    response = client.post(
        "/api/hydro/comparisons/transport",
        json={
            "baseline_task_id": base["id"],
            "candidate_task_id": cand["id"],
            "uncertainty_band": 0.05,
            "align_step_days": 10,
            "thresholds": {"concentration_limit": 0.05, "arrival_time_max": 60},
        },
    )
    assert response.status_code == 201, response.text
    report = response.json()
    assert report["kind"] == "transport"
    assert report["comparability"] == "comparable"
    axis = report["details"]["time_axis"]
    assert axis["common_domain"] == [5.0, 100.0]
    assert axis["point_count"] == 10
    assert report["details"]["series"][0]["time_days"] == 5.0
    metrics = {row["metric"]: row for row in report["summary"]["metrics"]}
    assert metrics["arrival_time_days"]["absolute_difference"] == 0.0
    assert metrics["arrival_time_days"]["threshold_crossing"]["change"] == "stays_within"
    peak = metrics["peak_concentration"]
    assert peak["absolute_difference"] >= 0.0
    assert peak["interval_overlap"]["overlaps"] is True
    crossing = metrics["peak_concentration"]["threshold_crossing"]
    assert crossing["change"] in ("improved", "worsened", "stays_within", "stays_violated")
    first_exceedance = report["summary"]["first_exceedance"]
    assert first_exceedance["limit"] == 0.05
    assert first_exceedance["change"] in ("unchanged", "earlier", "later")


def test_transport_comparison_flags_physical_parameter_mismatch(client):
    well = _create_well(client, "W-201")
    base = client.post(f"/api/hydro/wells/{well['id']}/transport", json={"source_concentration": 100, "distance_m": 100, "velocity_m_day": 2, "dispersion_m2_day": 5, "duration_days": 100, "step_days": 5, "model_version": "ade-1"}).json()
    cand = client.post(f"/api/hydro/wells/{well['id']}/transport", json={"source_concentration": 100, "distance_m": 100, "velocity_m_day": 4, "dispersion_m2_day": 5, "duration_days": 100, "step_days": 5, "model_version": "ade-2"}).json()

    response = client.post("/api/hydro/comparisons/transport", json={"baseline_task_id": base["id"], "candidate_task_id": cand["id"]})
    assert response.status_code == 201
    report = response.json()
    assert report["comparability"] == "partially_comparable"
    reasons = {(item["field"], item["reason"]) for item in report["incomparable"]}
    assert ("arrival_time_days", "physical_parameter_mismatch") in reasons
    assert ("peak_concentration", "physical_parameter_mismatch") in reasons
    assert ("concentration_curve", "physical_parameter_mismatch") in reasons
    mismatched = {item["parameter"] for item in report["parameter_mismatches"]}
    assert "velocity_m_day" in mismatched
    assert report["details"]["series"] == []


def test_transport_comparison_non_overlapping_domains(client):
    well = _create_well(client, "W-202")
    base = client.post(f"/api/hydro/wells/{well['id']}/transport", json={"source_concentration": 100, "distance_m": 100, "velocity_m_day": 2, "dispersion_m2_day": 5, "duration_days": 100, "step_days": 10, "model_version": "ade-1"}).json()
    cand = client.post(f"/api/hydro/wells/{well['id']}/transport", json={"source_concentration": 100, "distance_m": 100, "velocity_m_day": 2, "dispersion_m2_day": 5, "duration_days": 5, "step_days": 1, "model_version": "ade-2"}).json()

    response = client.post("/api/hydro/comparisons/transport", json={"baseline_task_id": base["id"], "candidate_task_id": cand["id"]})
    assert response.status_code == 201
    report = response.json()
    fields = {item["field"]: item["reason"] for item in report["incomparable"]}
    assert fields["concentration_curve"] == "non_overlapping_time_axis"
    # 标量指标不受时间轴重叠影响。
    metrics = {row["metric"] for row in report["summary"]["metrics"]}
    assert "arrival_time_days" in metrics and "peak_concentration" in metrics


def test_comparison_list_is_stable_sorted_and_detail_fetch(client):
    well = _create_well(client, "W-300")
    _, inv_base, inv_cand = _setup_inversion_runs(client, well)
    tr_base = client.post(f"/api/hydro/wells/{well['id']}/transport", json={"source_concentration": 100, "distance_m": 100, "velocity_m_day": 2, "dispersion_m2_day": 5, "duration_days": 100, "step_days": 5, "model_version": "ade-1"}).json()
    tr_cand = client.post(f"/api/hydro/wells/{well['id']}/transport", json={"source_concentration": 100, "distance_m": 100, "velocity_m_day": 2, "dispersion_m2_day": 5, "duration_days": 100, "step_days": 2, "model_version": "ade-2"}).json()
    inv_report = client.post("/api/hydro/comparisons/inversions", json={"baseline_task_id": inv_base["id"], "candidate_task_id": inv_cand["id"]}).json()
    tr_report = client.post("/api/hydro/comparisons/transport", json={"baseline_task_id": tr_base["id"], "candidate_task_id": tr_cand["id"]}).json()

    listing = client.get("/api/hydro/comparisons")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 2
    # 稳定排序：kind 升序（inversion 在 transport 前），同 kind 内按 dataset_id、id。
    assert [row["kind"] for row in body["data"]] == ["inversion", "transport"]
    for row in body["data"]:
        assert "details" not in row and "series" not in row
        assert row["input_fingerprint"] and row["created_at"]
        assert {"id", "baseline", "candidate", "comparability", "largest_change"} <= set(row)

    filtered = client.get("/api/hydro/comparisons?kind=transport")
    assert [row["kind"] for row in filtered.json()["data"]] == ["transport"]

    detail = client.get(f"/api/hydro/comparisons/{tr_report['id']}")
    assert detail.status_code == 200
    assert detail.json()["details"]["series"]
    assert client.get("/api/hydro/comparisons/9999").status_code == 404
    assert inv_report["id"] != tr_report["id"]


def test_interval_overlap_engine_primitives():
    disjoint = engine.interval_overlap([0.0, 1.0], [2.0, 3.0])
    assert disjoint["overlaps"] is False
    assert disjoint["overlap_length"] == 0.0
    assert disjoint["jaccard"] == 0.0

    partial = engine.interval_overlap([0.0, 10.0], [5.0, 15.0])
    assert partial["overlaps"] is True
    assert partial["overlap_length"] == 5.0
    assert abs(partial["jaccard"] - 1.0 / 3.0) < 1e-12
    # 较窄区间 [5,15] 与 [0,10] 等宽，覆盖率 50%。
    assert abs(partial["overlap_ratio"] - 0.5) < 1e-12

    crossing = engine._threshold_change(8.0, 12.0, 10.0, "max")
    assert crossing["change"] == "worsened"
    crossing = engine._threshold_change(12.0, 8.0, 10.0, "max")
    assert crossing["change"] == "improved"
    crossing = engine._threshold_change(80.0, 20.0, 50.0, "min")
    assert crossing["change"] == "worsened"

    zero_base = engine.compare_scalar("x", 0.0, 1.0)
    assert zero_base["absolute_difference"] == 1.0
    assert zero_base["relative_difference"] is None
    assert zero_base["reason"] == "relative_difference_undefined_zero_baseline"
