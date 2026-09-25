from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.database import get_connection, transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS hydro_wells (
 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 latitude REAL NOT NULL, longitude REAL NOT NULL, aquifer TEXT NOT NULL, screen_depth_m REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_endmembers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, isotope_d18o REAL NOT NULL,
 isotope_d2h REAL NOT NULL, solute_mg_l REAL NOT NULL, uncertainty REAL NOT NULL,
 version TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at TEXT NOT NULL,
 UNIQUE(name,version)
);
CREATE TABLE IF NOT EXISTS hydro_samples (
 id INTEGER PRIMARY KEY AUTOINCREMENT, well_id INTEGER NOT NULL REFERENCES hydro_wells(id) ON DELETE RESTRICT,
 sample_code TEXT NOT NULL UNIQUE, sampled_at TEXT NOT NULL, isotope_d18o REAL, isotope_d2h REAL,
 solute_mg_l REAL, detection_limit REAL NOT NULL, measurement_error REAL NOT NULL,
 quality_status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_inversions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id INTEGER NOT NULL REFERENCES hydro_samples(id) ON DELETE RESTRICT,
 task_key TEXT NOT NULL UNIQUE, model_version TEXT NOT NULL, method TEXT NOT NULL,
 input_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 worker_id TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_transport_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, well_id INTEGER NOT NULL REFERENCES hydro_wells(id) ON DELETE RESTRICT,
 task_key TEXT NOT NULL UNIQUE, model_version TEXT NOT NULL, input_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_comparisons (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT NOT NULL UNIQUE,
 comparison_type TEXT NOT NULL CHECK(comparison_type IN ('inversion','transport')),
 baseline_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
 input_fingerprint TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'done' CHECK(status IN ('done','partial','incompatible')),
 summary_json TEXT NOT NULL DEFAULT '{}', report_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, resource_type TEXT NOT NULL, resource_id INTEGER,
 action TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hydro_samples_well ON hydro_samples(well_id,sampled_at);
CREATE INDEX IF NOT EXISTS idx_hydro_inversions_status ON hydro_inversions(status,created_at);
CREATE INDEX IF NOT EXISTS idx_hydro_comparisons_type ON hydro_comparisons(comparison_type,id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _round(value: float) -> float:
    return round(float(value), 8)


def _rel_diff(delta: float, baseline: float) -> float | None:
    if baseline is None or abs(baseline) <= 1e-15: return None
    return _round(delta / abs(baseline))


def _metric_diff(baseline: float, candidate: float) -> dict[str, Any]:
    delta = float(candidate) - float(baseline)
    return {"baseline": _round(baseline), "candidate": _round(candidate), "delta": _round(delta),
            "abs_diff": _round(abs(delta)), "rel_diff": _rel_diff(delta, float(baseline))}


def _interval_overlap(first: list[float], second: list[float]) -> float:
    intersection = max(0.0, min(first[1], second[1]) - max(first[0], second[0]))
    union = max(first[1], second[1]) - min(first[0], second[0])
    return 1.0 if union <= 1e-15 else _round(intersection / union)


class HydroService:
    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    def create_well(self, payload: dict[str, Any], actor: str = "researcher") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO hydro_wells(code,name,latitude,longitude,aquifer,screen_depth_m,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (payload["code"],payload["name"],payload["latitude"],payload["longitude"],payload["aquifer"],payload["screen_depth_m"],now,now))
            well_id = cursor.lastrowid
            connection.execute("INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) VALUES('well',?,?,?,?,?)", (well_id,"create",actor,json.dumps(payload,ensure_ascii=False),now))
            return dict(connection.execute("SELECT * FROM hydro_wells WHERE id=?",(well_id,)).fetchone())

    def get_well(self, well_id: int) -> dict[str, Any] | None:
        well = self.connection.execute("SELECT * FROM hydro_wells WHERE id=?",(well_id,)).fetchone()
        if well is None: return None
        result = dict(well)
        result["samples"] = [dict(r) for r in self.connection.execute("SELECT * FROM hydro_samples WHERE well_id=? ORDER BY sampled_at,id",(well_id,)).fetchall()]
        return result

    def delete_well(self, well_id: int) -> bool:
        with transaction(immediate=True) as connection:
            cursor = connection.execute("DELETE FROM hydro_wells WHERE id=?",(well_id,))
            if cursor.rowcount == 0: raise KeyError("well_not_found")
            return True

    def create_endmember(self, payload: dict[str, Any]) -> dict[str, Any]:
        now=_now()
        with transaction(immediate=True) as connection:
            cursor=connection.execute("INSERT INTO hydro_endmembers(name,isotope_d18o,isotope_d2h,solute_mg_l,uncertainty,version,created_at) VALUES(?,?,?,?,?,?,?)",(payload["name"],payload["isotope_d18o"],payload["isotope_d2h"],payload["solute_mg_l"],payload["uncertainty"],payload["version"],now))
            return dict(connection.execute("SELECT * FROM hydro_endmembers WHERE id=?",(cursor.lastrowid,)).fetchone())

    def add_sample(self, well_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if self.connection.execute("SELECT id FROM hydro_wells WHERE id=?",(well_id,)).fetchone() is None: raise KeyError("well_not_found")
        values=[payload.get("isotope_d18o"),payload.get("isotope_d2h"),payload.get("solute_mg_l")]
        quality="usable" if sum(v is not None for v in values)>=2 else "incomplete"
        now=_now()
        with transaction(immediate=True) as connection:
            cursor=connection.execute("INSERT INTO hydro_samples(well_id,sample_code,sampled_at,isotope_d18o,isotope_d2h,solute_mg_l,detection_limit,measurement_error,quality_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(well_id,payload["sample_code"],payload["sampled_at"],payload.get("isotope_d18o"),payload.get("isotope_d2h"),payload.get("solute_mg_l"),payload["detection_limit"],payload["measurement_error"],quality,now))
            return dict(connection.execute("SELECT * FROM hydro_samples WHERE id=?",(cursor.lastrowid,)).fetchone())

    def _project_simplex(self, values: list[float]) -> list[float]:
        clipped=[max(0.0,v) for v in values]
        total=sum(clipped)
        return [1/len(values)]*len(values) if total<=1e-15 else [v/total for v in clipped]

    def solve_mixture(self, sample: sqlite3.Row, endmembers: list[sqlite3.Row], max_iterations: int, tolerance: float) -> dict[str, Any]:
        observed=[sample["isotope_d18o"],sample["isotope_d2h"],sample["solute_mg_l"]]
        active=[i for i,v in enumerate(observed) if v is not None]
        if len(active)<2: raise ValueError("insufficient_measurements")
        fractions=[1/len(endmembers)]*len(endmembers)
        scale=[20.0,100.0,max(1.0,float(sample["solute_mg_l"] or 1))]
        rate=0.08
        last=float("inf")
        for iteration in range(max_iterations):
            predicted=[sum(fractions[j]*[e["isotope_d18o"],e["isotope_d2h"],e["solute_mg_l"]][k] for j,e in enumerate(endmembers)) for k in range(3)]
            residual=[(predicted[k]-float(observed[k]))/scale[k] if k in active else 0.0 for k in range(3)]
            objective=sum(r*r for r in residual)+((sum(fractions)-1.0)*10)**2
            if abs(last-objective)<tolerance: break
            last=objective
            gradient=[]
            for e in endmembers:
                vector=[e["isotope_d18o"],e["isotope_d2h"],e["solute_mg_l"]]
                gradient.append(2*sum(residual[k]*vector[k]/scale[k] for k in active))
            fractions=self._project_simplex([f-rate*g for f,g in zip(fractions,gradient)])
        predicted=[sum(fractions[j]*[e["isotope_d18o"],e["isotope_d2h"],e["solute_mg_l"]][k] for j,e in enumerate(endmembers)) for k in range(3)]
        rmse=math.sqrt(sum(((predicted[k]-float(observed[k]))/scale[k])**2 for k in active)/len(active))
        return {"fractions":[{"endmember_id":e["id"],"name":e["name"],"fraction":round(f,8)} for e,f in zip(endmembers,fractions)],"mass_balance":round(sum(fractions),10),"predicted":predicted,"rmse":rmse,"iterations":iteration+1,"converged":abs(last-objective)<tolerance}

    def enqueue_inversion(self, sample_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        sample=self.connection.execute("SELECT * FROM hydro_samples WHERE id=?",(sample_id,)).fetchone()
        if sample is None: raise KeyError("sample_not_found")
        ids=sorted(set(payload["endmember_ids"]))
        endmembers=self.connection.execute(f"SELECT * FROM hydro_endmembers WHERE active=1 AND id IN ({','.join('?' for _ in ids)}) ORDER BY id",ids).fetchall()
        if len(endmembers)!=len(ids): raise ValueError("endmember_not_found")
        input_data={**payload,"endmember_ids":ids,"sample":dict(sample),"endmembers":[dict(e) for e in endmembers]}
        key=_digest(input_data); now=_now()
        with transaction(immediate=True) as connection:
            old=connection.execute("SELECT * FROM hydro_inversions WHERE task_key=?",(key,)).fetchone()
            if old: return dict(old)
            cursor=connection.execute("INSERT INTO hydro_inversions(sample_id,task_key,model_version,method,input_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(sample_id,key,payload["model_version"],payload["method"],json.dumps(input_data,ensure_ascii=False),now,now))
            return dict(connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(cursor.lastrowid,)).fetchone())

    def run_inversion(self, task_id: int, worker_id: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            task=connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(task_id,)).fetchone()
            if task is None: raise KeyError("task_not_found")
            if task["status"]=="done": return dict(task)
            connection.execute("UPDATE hydro_inversions SET status='running',attempts=attempts+1,worker_id=?,updated_at=? WHERE id=?",(worker_id,_now(),task_id))
        data=json.loads(task["input_json"])
        sample=self.connection.execute("SELECT * FROM hydro_samples WHERE id=?",(task["sample_id"],)).fetchone()
        ids=data["endmember_ids"]
        endmembers=self.connection.execute(f"SELECT * FROM hydro_endmembers WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY id",ids).fetchall()
        try: result=self.solve_mixture(sample,endmembers,data["max_iterations"],data["tolerance"])
        except Exception as exc:
            with transaction(immediate=True) as connection: connection.execute("UPDATE hydro_inversions SET status='failed',error=?,updated_at=? WHERE id=?",(str(exc),_now(),task_id))
            raise
        with transaction(immediate=True) as connection:
            connection.execute("UPDATE hydro_inversions SET status='done',result_json=?,error='',updated_at=? WHERE id=?",(json.dumps(result,ensure_ascii=False),_now(),task_id))
            return dict(connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(task_id,)).fetchone())

    def run_transport(self, well_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if self.connection.execute("SELECT id FROM hydro_wells WHERE id=?",(well_id,)).fetchone() is None: raise KeyError("well_not_found")
        key=_digest({"well_id":well_id,**payload}); now=_now()
        old=self.connection.execute("SELECT * FROM hydro_transport_runs WHERE task_key=?",(key,)).fetchone()
        if old: return dict(old)
        points=[]; t=payload["step_days"]
        while t<=payload["duration_days"]+1e-12:
            d=payload["dispersion_m2_day"]; x=payload["distance_m"]; v=payload["velocity_m_day"]
            c=payload["source_concentration"]*math.exp(-((x-v*t)**2)/(4*d*t))*math.exp(-payload["decay_per_day"]*t)/math.sqrt(4*math.pi*d*t)
            points.append({"time_days":round(t,8),"concentration":c}); t+=payload["step_days"]
        peak=max(points,key=lambda p:p["concentration"])
        result={"points":points,"peak":peak,"arrival_time_days":payload["distance_m"]/payload["velocity_m_day"],"model_version":payload["model_version"]}
        with transaction(immediate=True) as connection:
            cursor=connection.execute("INSERT INTO hydro_transport_runs(well_id,task_key,model_version,input_json,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(well_id,key,payload["model_version"],json.dumps(payload,ensure_ascii=False),"done",json.dumps(result,ensure_ascii=False),now,now))
            return dict(connection.execute("SELECT * FROM hydro_transport_runs WHERE id=?",(cursor.lastrowid,)).fetchone())

    def create_comparison(self, payload: dict[str, Any], actor: str = "researcher") -> dict[str, Any]:
        comparison_type=payload["comparison_type"]
        table="hydro_inversions" if comparison_type=="inversion" else "hydro_transport_runs"
        baseline=self.connection.execute(f"SELECT * FROM {table} WHERE id=?",(payload["baseline_id"],)).fetchone()
        candidate=self.connection.execute(f"SELECT * FROM {table} WHERE id=?",(payload["candidate_id"],)).fetchone()
        if baseline is None or candidate is None: raise KeyError("comparison_task_not_found")
        if baseline["status"]!="done" or candidate["status"]!="done": raise ValueError("两个计算任务都必须完成后才能比较")
        base_input,cand_input=json.loads(baseline["input_json"]),json.loads(candidate["input_json"])
        base_result,cand_result=json.loads(baseline["result_json"]),json.loads(candidate["result_json"])
        thresholds=payload.get("thresholds") or {}
        fingerprint=_digest({"comparison_type":comparison_type,"baseline_id":payload["baseline_id"],"candidate_id":payload["candidate_id"],"baseline_input":base_input,"candidate_input":cand_input,"baseline_result":base_result,"candidate_result":cand_result,"thresholds":thresholds})
        existing=self.connection.execute("SELECT * FROM hydro_comparisons WHERE task_key=?",(fingerprint,)).fetchone()
        if existing: return self._comparison_payload(existing,reused=True)
        if comparison_type=="inversion":
            if baseline["sample_id"]!=candidate["sample_id"]: raise ValueError("两次反演基于不同样本，不属于同一数据集，无法比较")
            report,summary,status=self._compare_inversion(baseline,candidate,base_input,cand_input,base_result,cand_result,thresholds)
        else:
            if baseline["well_id"]!=candidate["well_id"]: raise ValueError("两次迁移计算基于不同井点，不属于同一数据集，无法比较")
            report,summary,status=self._compare_transport(baseline,candidate,base_result,cand_result,thresholds)
        now=_now()
        report["generated_at"]=now
        with transaction(immediate=True) as connection:
            cursor=connection.execute("INSERT INTO hydro_comparisons(task_key,comparison_type,baseline_id,candidate_id,input_fingerprint,status,summary_json,report_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(fingerprint,comparison_type,payload["baseline_id"],payload["candidate_id"],fingerprint,status,json.dumps(summary,ensure_ascii=False),json.dumps(report,ensure_ascii=False),now))
            comparison_id=cursor.lastrowid
            connection.execute("INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) VALUES('comparison',?,?,?,?,?)",(comparison_id,"create",actor,json.dumps({"task_key":fingerprint,"comparison_type":comparison_type,"baseline_id":payload["baseline_id"],"candidate_id":payload["candidate_id"]},ensure_ascii=False),now))
            row=connection.execute("SELECT * FROM hydro_comparisons WHERE id=?",(comparison_id,)).fetchone()
            return self._comparison_payload(row,reused=False)

    def _comparison_payload(self, row: sqlite3.Row, reused: bool) -> dict[str, Any]:
        return {"id":row["id"],"comparison_type":row["comparison_type"],"baseline_id":row["baseline_id"],"candidate_id":row["candidate_id"],"status":row["status"],"input_fingerprint":row["input_fingerprint"],"summary":json.loads(row["summary_json"]),"report":json.loads(row["report_json"]),"created_at":row["created_at"],"reused":reused}

    def get_comparison(self, comparison_id: int) -> dict[str, Any] | None:
        row=self.connection.execute("SELECT * FROM hydro_comparisons WHERE id=?",(comparison_id,)).fetchone()
        if row is None: return None
        payload=self._comparison_payload(row,reused=False)
        del payload["reused"]
        return payload

    def list_comparisons(self, comparison_type: str | None = None) -> list[dict[str, Any]]:
        if comparison_type:
            rows=self.connection.execute("SELECT * FROM hydro_comparisons WHERE comparison_type=? ORDER BY id",(comparison_type,)).fetchall()
        else:
            rows=self.connection.execute("SELECT * FROM hydro_comparisons ORDER BY id").fetchall()
        return [{"id":row["id"],"comparison_type":row["comparison_type"],"baseline_id":row["baseline_id"],"candidate_id":row["candidate_id"],"status":row["status"],"input_fingerprint":row["input_fingerprint"],"summary":json.loads(row["summary_json"]),"created_at":row["created_at"]} for row in rows]

    def _compare_inversion(self, baseline: sqlite3.Row, candidate: sqlite3.Row, base_input: dict[str, Any], cand_input: dict[str, Any], base_result: dict[str, Any], cand_result: dict[str, Any], thresholds: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
        incompatible: list[dict[str, str]]=[]
        def index_fractions(result: dict[str, Any]) -> tuple[dict[str, float], set[str]]:
            mapping: dict[str, float]={}; duplicates: set[str]=set()
            for item in result.get("fractions") or []:
                if item["name"] in mapping: duplicates.add(item["name"])
                mapping[item["name"]]=item["fraction"]
            return mapping,duplicates
        base_map,base_dup=index_fractions(base_result)
        cand_map,cand_dup=index_fractions(cand_result)
        for name in sorted(base_dup): incompatible.append({"field":f"fractions.{name}","reason":"端元名称在基准版本结果中重复"})
        for name in sorted(cand_dup): incompatible.append({"field":f"fractions.{name}","reason":"端元名称在候选版本结果中重复"})
        for name in sorted(set(base_map)-set(cand_map)): incompatible.append({"field":f"fractions.{name}","reason":"仅基准版本包含该端元"})
        for name in sorted(set(cand_map)-set(base_map)): incompatible.append({"field":f"fractions.{name}","reason":"仅候选版本包含该端元"})
        base_unc={e["name"]:float(e["uncertainty"]) for e in base_input.get("endmembers") or []}
        cand_unc={e["name"]:float(e["uncertainty"]) for e in cand_input.get("endmembers") or []}
        common=sorted((set(base_map)&set(cand_map))-base_dup-cand_dup)
        entries=[]
        for name in common:
            fb,fc=float(base_map[name]),float(cand_map[name])
            base_interval=[max(0.0,fb-base_unc.get(name,0.0)),min(1.0,fb+base_unc.get(name,0.0))]
            cand_interval=[max(0.0,fc-cand_unc.get(name,0.0)),min(1.0,fc+cand_unc.get(name,0.0))]
            entries.append({"name":name,**_metric_diff(fb,fc),"baseline_interval":[_round(v) for v in base_interval],"candidate_interval":[_round(v) for v in cand_interval],"interval_overlap":_interval_overlap(base_interval,cand_interval)})
        shift_limit=float(thresholds.get("fraction_shift",0.1))
        significant=sorted((e for e in entries if e["abs_diff"]>=shift_limit),key=lambda e:(-e["abs_diff"],e["name"]))
        max_fraction=max((e["abs_diff"] for e in entries),default=None)
        mean_fraction=_round(sum(e["abs_diff"] for e in entries)/len(entries)) if entries else None
        sample=base_input.get("sample") or {}
        base_pred,cand_pred=base_result.get("predicted") or [],cand_result.get("predicted") or []
        observable_entries=[]
        for index,observable in enumerate(["isotope_d18o","isotope_d2h","solute_mg_l"]):
            observed=sample.get(observable)
            if observed is None:
                incompatible.append({"field":f"residuals.{observable}","reason":"样本缺少该观测值"}); continue
            if index>=len(base_pred) or index>=len(cand_pred):
                incompatible.append({"field":f"residuals.{observable}","reason":"结果缺少对应预测值"}); continue
            base_residual=float(base_pred[index])-float(observed)
            cand_residual=float(cand_pred[index])-float(observed)
            magnitude=_metric_diff(abs(base_residual),abs(cand_residual))
            observable_entries.append({"observable":observable,"observed":float(observed),"baseline_residual":_round(base_residual),"candidate_residual":_round(cand_residual),"abs_residual":magnitude})
        rmse_entry=None
        if "rmse" in base_result and "rmse" in cand_result:
            rmse_entry=_metric_diff(float(base_result["rmse"]),float(cand_result["rmse"]))
        else:
            incompatible.append({"field":"residuals.rmse","reason":"结果缺少拟合残差指标"})
        solver={"baseline_iterations":base_result.get("iterations"),"candidate_iterations":cand_result.get("iterations"),"baseline_converged":base_result.get("converged"),"candidate_converged":cand_result.get("converged")}
        if "mass_balance" in base_result and "mass_balance" in cand_result:
            solver["mass_balance"]=_metric_diff(float(base_result["mass_balance"]),float(cand_result["mass_balance"]))
        status="incompatible" if not entries and rmse_entry is None and not observable_entries else ("partial" if incompatible else "done")
        report={"comparison_type":"inversion","baseline":{"task_id":baseline["id"],"model_version":baseline["model_version"],"result_digest":_digest(base_result)},"candidate":{"task_id":candidate["id"],"model_version":candidate["model_version"],"result_digest":_digest(cand_result)},"dataset":{"sample_id":baseline["sample_id"],"sample_code":sample.get("sample_code")},"fractions":{"endmembers":entries,"max_abs_diff":max_fraction,"mean_abs_diff":mean_fraction,"shift_threshold":shift_limit,"significant_shifts":[{"name":e["name"],"delta":e["delta"],"abs_diff":e["abs_diff"]} for e in significant]},"residuals":{"rmse":rmse_entry,"observables":observable_entries,"solver":solver},"incompatible_fields":incompatible}
        summary={"baseline_model_version":baseline["model_version"],"candidate_model_version":candidate["model_version"],"max_fraction_abs_diff":max_fraction,"mean_fraction_abs_diff":mean_fraction,"rmse_delta":rmse_entry["delta"] if rmse_entry else None,"rmse_rel_diff":rmse_entry["rel_diff"] if rmse_entry else None,"significant_shift_count":len(significant),"incompatible_count":len(incompatible)}
        return report,summary,status

    def _compare_transport(self, baseline: sqlite3.Row, candidate: sqlite3.Row, base_result: dict[str, Any], cand_result: dict[str, Any], thresholds: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
        incompatible: list[dict[str, str]]=[]
        base_points,cand_points=base_result.get("points") or [],cand_result.get("points") or []
        base_axis={round(p["time_days"],6):p["concentration"] for p in base_points}
        cand_axis={round(p["time_days"],6):p["concentration"] for p in cand_points}
        common_times=sorted(set(base_axis)&set(cand_axis))
        series=[{"time_days":t,"baseline":_round(base_axis[t]),"candidate":_round(cand_axis[t]),"delta":_round(cand_axis[t]-base_axis[t]),"abs_diff":_round(abs(cand_axis[t]-base_axis[t])),"rel_diff":_rel_diff(cand_axis[t]-base_axis[t],base_axis[t])} for t in common_times]
        if series:
            curve={"common_points":len(series),"baseline_points":len(base_points),"candidate_points":len(cand_points),"time_range":[series[0]["time_days"],series[-1]["time_days"]],"max_abs_diff":max(s["abs_diff"] for s in series),"mean_abs_diff":_round(sum(s["abs_diff"] for s in series)/len(series)),"rmse":_round(math.sqrt(sum(s["delta"]**2 for s in series)/len(series))),"series":series}
        else:
            curve={"common_points":0,"baseline_points":len(base_points),"candidate_points":len(cand_points),"time_range":None,"max_abs_diff":None,"mean_abs_diff":None,"rmse":None,"series":[]}
            incompatible.append({"field":"curve","reason":"统一时间轴后无共同采样点"})
        base_peak,cand_peak=base_result.get("peak") or {},cand_result.get("peak") or {}
        peak={"baseline":base_peak,"candidate":cand_peak,"concentration":_metric_diff(float(base_peak["concentration"]),float(cand_peak["concentration"])),"time_days":_metric_diff(float(base_peak["time_days"]),float(cand_peak["time_days"]))}
        arrival=_metric_diff(float(base_result["arrival_time_days"]),float(cand_result["arrival_time_days"]))
        explicit_limit=thresholds.get("concentration")
        threshold_value=float(explicit_limit) if explicit_limit is not None else _round(0.5*float(base_peak["concentration"]))
        def above_interval(points: list[dict[str, Any]]) -> list[float] | None:
            times=[p["time_days"] for p in points if p["concentration"]>=threshold_value]
            return [_round(min(times)),_round(max(times))] if times else None
        base_interval,cand_interval=above_interval(base_points),above_interval(cand_points)
        if base_interval is None and cand_interval is None:
            incompatible.append({"field":"threshold_interval","reason":"两个版本的浓度均未达到阈值"})
            overlap_days=overlap_ratio=None
        elif base_interval is None or cand_interval is None:
            overlap_days,overlap_ratio=0.0,0.0
        else:
            overlap_days=_round(max(0.0,min(base_interval[1],cand_interval[1])-max(base_interval[0],cand_interval[0])))
            overlap_ratio=_interval_overlap(base_interval,cand_interval)
        def first_crossing(points: list[dict[str, Any]]) -> float | None:
            times=[p["time_days"] for p in points if p["concentration"]>=threshold_value]
            return _round(min(times)) if times else None
        base_first,cand_first=first_crossing(base_points),first_crossing(cand_points)
        threshold_section={"concentration_threshold":threshold_value,"threshold_source":"request" if explicit_limit is not None else "default_half_baseline_peak","baseline_interval":base_interval,"candidate_interval":cand_interval,"interval_overlap_days":overlap_days,"interval_overlap_ratio":overlap_ratio,"baseline_first_crossing_days":base_first,"candidate_first_crossing_days":cand_first,"first_crossing_delta":_round(cand_first-base_first) if base_first is not None and cand_first is not None else None,"baseline_exceeds":base_interval is not None,"candidate_exceeds":cand_interval is not None,"exceedance_changed":(base_interval is None)!=(cand_interval is None)}
        arrival_limit=thresholds.get("arrival_time_days")
        if arrival_limit is not None:
            base_within=float(base_result["arrival_time_days"])<=float(arrival_limit)
            cand_within=float(cand_result["arrival_time_days"])<=float(arrival_limit)
            threshold_section["arrival_time_limit_days"]=float(arrival_limit)
            threshold_section["baseline_within_limit"]=base_within
            threshold_section["candidate_within_limit"]=cand_within
            threshold_section["arrival_compliance_changed"]=base_within!=cand_within
        status="partial" if incompatible else "done"
        report={"comparison_type":"transport","baseline":{"task_id":baseline["id"],"model_version":baseline["model_version"],"result_digest":_digest(base_result)},"candidate":{"task_id":candidate["id"],"model_version":candidate["model_version"],"result_digest":_digest(cand_result)},"dataset":{"well_id":baseline["well_id"]},"peak":peak,"arrival_time_days":arrival,"curve":curve,"threshold":threshold_section,"incompatible_fields":incompatible}
        summary={"baseline_model_version":baseline["model_version"],"candidate_model_version":candidate["model_version"],"peak_concentration_delta":peak["concentration"]["delta"],"peak_concentration_rel_diff":peak["concentration"]["rel_diff"],"arrival_time_delta":arrival["delta"],"curve_max_abs_diff":curve["max_abs_diff"],"interval_overlap_ratio":overlap_ratio,"exceedance_changed":threshold_section["exceedance_changed"],"incompatible_count":len(incompatible)}
        return report,summary,status
