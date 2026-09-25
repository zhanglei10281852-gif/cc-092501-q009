from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.hydro.schemas import (
    ComparisonRequest,
    EndmemberCreate,
    InversionRequest,
    SampleCreate,
    TransportRequest,
    WellCreate,
)
from app.hydro.service import HydroService

router=APIRouter(prefix="/api/hydro",tags=["地下水科学计算"])

def service()->HydroService: return HydroService()

@router.post("/wells",status_code=201)
def create_well(payload:WellCreate):
    try: return service().create_well(payload.model_dump())
    except Exception as exc:
        if "UNIQUE" in str(exc).upper(): raise HTTPException(409,"井点编码已存在") from exc
        raise

@router.get("/wells/{well_id}")
def get_well(well_id:int):
    value=service().get_well(well_id)
    if value is None: raise HTTPException(404,"井点不存在")
    return value

@router.delete("/wells/{well_id}")
def delete_well(well_id:int):
    try: service().delete_well(well_id); return {"message":"井点已删除"}
    except KeyError as exc: raise HTTPException(404,"井点不存在") from exc

@router.post("/endmembers",status_code=201)
def create_endmember(payload:EndmemberCreate): return service().create_endmember(payload.model_dump())

@router.post("/wells/{well_id}/samples",status_code=201)
def add_sample(well_id:int,payload:SampleCreate):
    try: return service().add_sample(well_id,payload.model_dump())
    except KeyError as exc: raise HTTPException(404,"井点不存在") from exc

@router.post("/samples/{sample_id}/inversions",status_code=202)
def enqueue_inversion(sample_id:int,payload:InversionRequest):
    try: return service().enqueue_inversion(sample_id,payload.model_dump())
    except KeyError as exc: raise HTTPException(404,"样本不存在") from exc
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc

@router.post("/inversions/{task_id}/run")
def run_inversion(task_id:int,worker_id:str=Query(...,min_length=1)):
    try: return service().run_inversion(task_id,worker_id)
    except KeyError as exc: raise HTTPException(404,"任务不存在") from exc
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc

@router.post("/wells/{well_id}/transport",status_code=201)
def run_transport(well_id:int,payload:TransportRequest):
    try: return service().run_transport(well_id,payload.model_dump())
    except KeyError as exc: raise HTTPException(404,"井点不存在") from exc


# --------------------------------------------------------------------
# 模型版本结果比较
# --------------------------------------------------------------------

_COMPARISON_ERRORS = {
    "inversion_not_found": (404, "反演任务不存在"),
    "transport_not_found": (404, "迁移计算不存在"),
    "datasets_do_not_match": (422, "两个任务不属于同一数据集，无法比较"),
    "inversion_not_done": (422, "反演任务尚未完成，无法比较"),
    "cannot_compare_task_with_itself": (422, "不能与自身比较，请选择两个不同的模型版本"),
}


def _comparison_http_error(exc: ValueError | KeyError) -> HTTPException:
    code = str(exc).strip("'")
    status, message = _COMPARISON_ERRORS.get(code, (422, str(exc)))
    return HTTPException(status, message)


@router.post("/comparisons/inversions", status_code=201)
def compare_inversions(payload: ComparisonRequest):
    try:
        return service().compare_inversions(
            payload.baseline_task_id, payload.candidate_task_id, payload.options()
        )
    except (KeyError, ValueError) as exc:
        raise _comparison_http_error(exc) from exc


@router.post("/comparisons/transport", status_code=201)
def compare_transport(payload: ComparisonRequest):
    try:
        return service().compare_transport_runs(
            payload.baseline_task_id, payload.candidate_task_id, payload.options()
        )
    except (KeyError, ValueError) as exc:
        raise _comparison_http_error(exc) from exc


@router.get("/comparisons")
def list_comparisons(
    kind: str | None = Query(default=None, pattern="^(inversion|transport)$"),
    dataset_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
):
    return service().list_comparisons(
        kind=kind, dataset_id=dataset_id, offset=(page - 1) * size, limit=size
    )


@router.get("/comparisons/{report_id}")
def get_comparison(report_id: int):
    report = service().get_comparison(report_id)
    if report is None:
        raise HTTPException(404, "差异报告不存在")
    return report
