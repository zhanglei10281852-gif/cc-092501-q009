from __future__ import annotations


def create_well(client, code="W-C01"):
    response = client.post("/api/hydro/wells", json={"code": code, "name": "对比试验井", "latitude": 35.1, "longitude": 116.2, "aquifer": "浅层孔隙含水层", "screen_depth_m": 42})
    assert response.status_code == 201, response.text
    return response.json()


def create_endmember(client, name, d18o, d2h, solute, uncertainty=0.1, version="v1"):
    response = client.post("/api/hydro/endmembers", json={"name": name, "isotope_d18o": d18o, "isotope_d2h": d2h, "solute_mg_l": solute, "uncertainty": uncertainty, "version": version})
    assert response.status_code == 201, response.text
    return response.json()


def create_sample(client, well_id, code="S-C01"):
    response = client.post(f"/api/hydro/wells/{well_id}/samples", json={"sample_code": code, "sampled_at": "2026-09-24T08:00:00+00:00", "isotope_d18o": -7.5, "isotope_d2h": -52.5, "solute_mg_l": 30, "detection_limit": 0.1, "measurement_error": 0.05})
    assert response.status_code == 201, response.text
    return response.json()


def run_inversion(client, sample_id, endmember_ids, model_version):
    task = client.post(f"/api/hydro/samples/{sample_id}/inversions", json={"endmember_ids": endmember_ids, "max_iterations": 1000, "tolerance": 1e-10, "model_version": model_version})
    assert task.status_code == 202, task.text
    done = client.post(f"/api/hydro/inversions/{task.json()['id']}/run?worker_id=test")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"
    return done.json()


def run_transport(client, well_id, model_version, **overrides):
    payload = {"source_concentration": 100, "distance_m": 100, "velocity_m_day": 2, "dispersion_m2_day": 5, "decay_per_day": 0.01, "duration_days": 100, "step_days": 5, "model_version": model_version}
    payload.update(overrides)
    response = client.post(f"/api/hydro/wells/{well_id}/transport", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_inversion_comparison_reports_fraction_and_residual_changes(client):
    well = create_well(client)
    e1 = create_endmember(client, "山区降水", -10, -70, 10)
    e2 = create_endmember(client, "河流渗漏", -5, -35, 50)
    e3 = create_endmember(client, "侧向径流", -8, -55, 25)
    sample = create_sample(client, well["id"])
    baseline = run_inversion(client, sample["id"], [e1["id"], e2["id"]], "mix-1")
    candidate = run_inversion(client, sample["id"], [e1["id"], e2["id"], e3["id"]], "mix-2")
    response = client.post("/api/hydro/comparisons", json={"comparison_type": "inversion", "baseline_id": baseline["id"], "candidate_id": candidate["id"]})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reused"] is False
    assert body["status"] == "partial"
    assert body["input_fingerprint"]
    assert body["created_at"] == body["report"]["generated_at"]
    report = body["report"]
    assert report["baseline"]["model_version"] == "mix-1"
    assert report["candidate"]["model_version"] == "mix-2"
    names = [entry["name"] for entry in report["fractions"]["endmembers"]]
    assert names == sorted(names) == ["山区降水", "河流渗漏"]
    for entry in report["fractions"]["endmembers"]:
        assert entry["abs_diff"] >= 0
        assert entry["rel_diff"] is not None
        assert 0 <= entry["interval_overlap"] <= 1
        assert len(entry["baseline_interval"]) == 2
    assert report["fractions"]["max_abs_diff"] > 0
    assert report["fractions"]["significant_shifts"], "端元比例变化超过默认阈值应进入显著变化列表"
    incompatible_fields = {item["field"] for item in report["incompatible_fields"]}
    assert "fractions.侧向径流" in incompatible_fields
    assert report["residuals"]["rmse"]["baseline"] >= 0
    assert report["residuals"]["rmse"]["abs_diff"] >= 0
    observables = [entry["observable"] for entry in report["residuals"]["observables"]]
    assert observables == ["isotope_d18o", "isotope_d2h", "solute_mg_l"]
    assert body["summary"]["baseline_model_version"] == "mix-1"
    assert body["summary"]["incompatible_count"] == 1


def test_identical_model_inputs_yield_done_status(client):
    well = create_well(client, "W-C02")
    e1 = create_endmember(client, "山区降水", -10, -70, 10)
    e2 = create_endmember(client, "河流渗漏", -5, -35, 50)
    sample = create_sample(client, well["id"], "S-C02")
    baseline = run_inversion(client, sample["id"], [e1["id"], e2["id"]], "mix-1")
    candidate = run_inversion(client, sample["id"], [e1["id"], e2["id"]], "mix-1r")
    response = client.post("/api/hydro/comparisons", json={"comparison_type": "inversion", "baseline_id": baseline["id"], "candidate_id": candidate["id"]})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "done"
    assert body["report"]["incompatible_fields"] == []
    assert body["report"]["fractions"]["max_abs_diff"] == 0
    assert body["report"]["residuals"]["rmse"]["delta"] == 0


def test_comparison_reuses_existing_report(client):
    well = create_well(client, "W-C03")
    e1 = create_endmember(client, "山区降水", -10, -70, 10)
    e2 = create_endmember(client, "河流渗漏", -5, -35, 50)
    sample = create_sample(client, well["id"], "S-C03")
    baseline = run_inversion(client, sample["id"], [e1["id"], e2["id"]], "mix-1")
    candidate = run_inversion(client, sample["id"], [e1["id"], e2["id"]], "mix-2")
    payload = {"comparison_type": "inversion", "baseline_id": baseline["id"], "candidate_id": candidate["id"]}
    first = client.post("/api/hydro/comparisons", json=payload)
    assert first.status_code == 201, first.text
    second = client.post("/api/hydro/comparisons", json=payload)
    assert second.status_code == 201, second.text
    assert second.json()["reused"] is True
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["created_at"] == first.json()["created_at"]
    assert second.json()["input_fingerprint"] == first.json()["input_fingerprint"]
    listing = client.get("/api/hydro/comparisons")
    assert len(listing.json()["items"]) == 1, "重复请求不应产生新报告"


def test_inversion_comparison_rejects_mismatched_datasets(client):
    well = create_well(client, "W-C04")
    e1 = create_endmember(client, "山区降水", -10, -70, 10)
    e2 = create_endmember(client, "河流渗漏", -5, -35, 50)
    first_sample = create_sample(client, well["id"], "S-C04")
    second_sample = create_sample(client, well["id"], "S-C05")
    baseline = run_inversion(client, first_sample["id"], [e1["id"], e2["id"]], "mix-1")
    candidate = run_inversion(client, second_sample["id"], [e1["id"], e2["id"]], "mix-2")
    response = client.post("/api/hydro/comparisons", json={"comparison_type": "inversion", "baseline_id": baseline["id"], "candidate_id": candidate["id"]})
    assert response.status_code == 422
    assert "同一数据集" in response.json()["detail"]


def test_inversion_comparison_requires_completed_tasks(client):
    well = create_well(client, "W-C05")
    e1 = create_endmember(client, "山区降水", -10, -70, 10)
    e2 = create_endmember(client, "河流渗漏", -5, -35, 50)
    sample = create_sample(client, well["id"], "S-C06")
    queued = client.post(f"/api/hydro/samples/{sample['id']}/inversions", json={"endmember_ids": [e1["id"], e2["id"]], "model_version": "mix-1"})
    assert queued.status_code == 202
    done = run_inversion(client, sample["id"], [e1["id"], e2["id"]], "mix-2")
    response = client.post("/api/hydro/comparisons", json={"comparison_type": "inversion", "baseline_id": queued.json()["id"], "candidate_id": done["id"]})
    assert response.status_code == 422
    assert "完成" in response.json()["detail"]


def test_transport_comparison_reports_peak_arrival_curve_and_threshold(client):
    well = create_well(client, "W-C06")
    baseline = run_transport(client, well["id"], "ade-1")
    candidate = run_transport(client, well["id"], "ade-2", velocity_m_day=4, decay_per_day=0.05)
    response = client.post("/api/hydro/comparisons", json={"comparison_type": "transport", "baseline_id": baseline["id"], "candidate_id": candidate["id"], "thresholds": {"concentration": 0.5, "arrival_time_days": 40}})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "done"
    report = body["report"]
    assert report["peak"]["concentration"]["candidate"] < report["peak"]["concentration"]["baseline"]
    assert report["peak"]["concentration"]["rel_diff"] < 0
    assert report["arrival_time_days"]["baseline"] == 50
    assert report["arrival_time_days"]["candidate"] == 25
    assert report["arrival_time_days"]["delta"] == -25
    curve = report["curve"]
    assert curve["common_points"] == 20
    assert curve["time_range"] == [5, 100]
    assert curve["max_abs_diff"] > 0
    assert [point["time_days"] for point in curve["series"]] == sorted(point["time_days"] for point in curve["series"])
    threshold = report["threshold"]
    assert threshold["concentration_threshold"] == 0.5
    assert threshold["threshold_source"] == "request"
    assert threshold["baseline_interval"] and threshold["candidate_interval"]
    assert 0 <= threshold["interval_overlap_ratio"] <= 1
    assert threshold["first_crossing_delta"] is not None
    assert threshold["arrival_time_limit_days"] == 40
    assert threshold["baseline_within_limit"] is False
    assert threshold["candidate_within_limit"] is True
    assert threshold["arrival_compliance_changed"] is True
    assert body["summary"]["peak_concentration_rel_diff"] < 0
    assert body["summary"]["arrival_time_delta"] == -25


def test_transport_comparison_uses_default_threshold_and_marks_disjoint_axes(client):
    well = create_well(client, "W-C07")
    baseline = run_transport(client, well["id"], "ade-1", duration_days=50, step_days=5)
    candidate = run_transport(client, well["id"], "ade-2", duration_days=180, step_days=60)
    response = client.post("/api/hydro/comparisons", json={"comparison_type": "transport", "baseline_id": baseline["id"], "candidate_id": candidate["id"]})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "partial"
    report = body["report"]
    assert report["curve"]["common_points"] == 0
    assert report["curve"]["series"] == []
    incompatible = {item["field"]: item["reason"] for item in report["incompatible_fields"]}
    assert "curve" in incompatible
    assert report["peak"]["concentration"]["abs_diff"] >= 0, "曲线不可比时峰值与到达时间仍应可比较"
    threshold = report["threshold"]
    assert threshold["threshold_source"] == "default_half_baseline_peak"
    assert threshold["concentration_threshold"] > 0


def test_transport_comparison_rejects_cross_well(client):
    first_well = create_well(client, "W-C08")
    second_well = create_well(client, "W-C09")
    baseline = run_transport(client, first_well["id"], "ade-1")
    candidate = run_transport(client, second_well["id"], "ade-2")
    response = client.post("/api/hydro/comparisons", json={"comparison_type": "transport", "baseline_id": baseline["id"], "candidate_id": candidate["id"]})
    assert response.status_code == 422
    assert "同一数据集" in response.json()["detail"]


def test_comparison_list_and_detail_are_stably_ordered(client):
    well = create_well(client, "W-C10")
    e1 = create_endmember(client, "山区降水", -10, -70, 10)
    e2 = create_endmember(client, "河流渗漏", -5, -35, 50)
    sample = create_sample(client, well["id"], "S-C07")
    first = run_inversion(client, sample["id"], [e1["id"], e2["id"]], "mix-1")
    second = run_inversion(client, sample["id"], [e1["id"], e2["id"]], "mix-2")
    third = run_inversion(client, sample["id"], [e1["id"], e2["id"]], "mix-3")
    for baseline, candidate in ((first, second), (first, third), (second, third)):
        created = client.post("/api/hydro/comparisons", json={"comparison_type": "inversion", "baseline_id": baseline["id"], "candidate_id": candidate["id"]})
        assert created.status_code == 201, created.text
    transport = run_transport(client, well["id"], "ade-1")
    transport2 = run_transport(client, well["id"], "ade-2", velocity_m_day=4)
    created = client.post("/api/hydro/comparisons", json={"comparison_type": "transport", "baseline_id": transport["id"], "candidate_id": transport2["id"]})
    assert created.status_code == 201, created.text
    listing = client.get("/api/hydro/comparisons")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [item["id"] for item in items] == sorted(item["id"] for item in items)
    assert len(items) == 4
    for item in items:
        assert "report" not in item, "列表应只返回摘要"
        assert item["summary"]["baseline_model_version"]
        assert item["input_fingerprint"]
    inversions = client.get("/api/hydro/comparisons", params={"comparison_type": "inversion"})
    assert [item["comparison_type"] for item in inversions.json()["items"]] == ["inversion"] * 3
    detail = client.get(f"/api/hydro/comparisons/{items[0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["report"]["comparison_type"] == "inversion"
    assert detail.json()["report"]["fractions"]["endmembers"]
    missing = client.get("/api/hydro/comparisons/9999")
    assert missing.status_code == 404


def test_comparison_with_missing_task_returns_404(client):
    response = client.post("/api/hydro/comparisons", json={"comparison_type": "inversion", "baseline_id": 9998, "candidate_id": 9999})
    assert response.status_code == 404
