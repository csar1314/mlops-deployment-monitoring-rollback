import json
import math
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from pydantic import BaseModel, ConfigDict, Field

from .config import Policy, Settings
from .engine import DomainError, Engine
from .training import samples


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PredictionInput(Input):
    features: list[Annotated[float, Field(ge=0, le=20, allow_inf_nan=False)]] = Field(
        min_length=4, max_length=4
    )


class TrainingInput(Input):
    version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,39}$")
    algorithm: Literal["logistic", "forest"] = "logistic"


class DeployInput(Input):
    version: str
    experiment_id: str | None = None


class RollbackInput(Input):
    expected_deployment_id: str


class FeedbackInput(Input):
    request_id: str
    label: int = Field(ge=0, le=2, strict=True)


class FaultInput(Input):
    kind: Literal["none", "error", "latency", "accuracy"]
    duration_seconds: float = Field(60, ge=1, le=120)
    delay_ms: int = Field(350, ge=0, le=2000)
    experiment_id: str | None = None


class DemoInput(Input):
    kind: Literal["error", "latency", "accuracy"] = "error"


def create_app(settings=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        engine = Engine(settings)
        app.state.engine = engine
        app.state.window_gauges = {
            "error_rate": Gauge(
                "mlops_window_error_rate", "Current deployment window error rate", registry=engine.registry
            ),
            "p95_ms": Gauge(
                "mlops_window_p95_seconds", "Current deployment window P95 seconds", registry=engine.registry
            ),
            "accuracy": Gauge(
                "mlops_window_labeled_accuracy",
                "Current deployment labeled accuracy",
                registry=engine.registry,
            ),
            "count": Gauge(
                "mlops_window_samples", "Current deployment window sample count", registry=engine.registry
            ),
            "labeled_count": Gauge(
                "mlops_window_labels", "Current deployment labeled sample count", registry=engine.registry
            ),
        }
        app.state.policy_gauges = {
            key: Gauge("mlops_policy_" + name, "Configured rollback policy: " + key, registry=engine.registry)
            for key, name in {
                "error_rate_threshold": "error_rate_threshold",
                "p95_ms_threshold": "p95_seconds_threshold",
                "accuracy_floor": "accuracy_floor",
                "min_requests": "min_requests",
                "min_labels": "min_labels",
            }.items()
        }
        try:
            yield
        finally:
            engine.close()

    app = FastAPI(title="MLOps 部署、监控与自动回滚系统", version="1.0.0", lifespan=lifespan)

    def engine(request: Request):
        return request.app.state.engine

    def admin(x_admin_token: str = Header(default="")):
        if not secrets.compare_digest(x_admin_token.encode(), settings.token.encode()):
            raise DomainError("需要有效的管理令牌（X-Admin-Token）", 401)

    authorized = [Depends(admin)]

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Do not echo arbitrary request values (including NaN/Infinity) into JSON errors.
        return JSONResponse(
            {
                "detail": [
                    {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
                    for item in error.errors()
                ]
            },
            status_code=422,
        )

    @app.get("/health/live", tags=["健康检查"])
    def live():
        return {"status": "alive"}

    @app.get("/health/ready", tags=["健康检查"])
    def ready(service: Engine = Depends(engine)):
        status = service.status()
        return JSONResponse(
            {
                "ready": status["ready"],
                "version": status["active_version"],
                "deployment_id": status["deployment_id"],
            },
            status_code=200 if status["ready"] else 503,
        )

    @app.post("/api/predict", tags=["预测"])
    def predict(data: PredictionInput, service: Engine = Depends(engine)):
        result = service.predict(data.features)
        return JSONResponse(result, status_code=200 if result["success"] else 503)

    @app.post("/api/feedback", dependencies=authorized, tags=["预测"])
    def feedback(data: FeedbackInput, service: Engine = Depends(engine)):
        return service.feedback(data.request_id, data.label)

    @app.get("/api/samples", tags=["预测"])
    def sample_data():
        return samples()

    @app.get("/api/status", tags=["监控"])
    def status(service: Engine = Depends(engine)):
        return service.status()

    @app.get("/api/events", tags=["审计"])
    def events(limit: int = Query(100, ge=1, le=1000), service: Engine = Depends(engine)):
        return service.store.events(limit)

    @app.get("/api/history", tags=["监控"])
    def history(service: Engine = Depends(engine)):
        return list(
            reversed(
                service.store.rows(
                    "SELECT timestamp,latency_ms,success,version FROM requests ORDER BY seq DESC LIMIT 120"
                )
            )
        )

    @app.get("/api/models", tags=["模型管理"])
    def models(service: Engine = Depends(engine)):
        return service.store.models()

    @app.post("/api/models/train", dependencies=authorized, tags=["模型管理"])
    def train(data: TrainingInput, service: Engine = Depends(engine)):
        return service.train(data.version, data.algorithm)

    @app.post("/api/models/{version}/promote", dependencies=authorized, tags=["模型管理"])
    def promote(version: str, service: Engine = Depends(engine)):
        return service.promote(version)

    @app.get("/api/deployments", tags=["审计"])
    def deployments(service: Engine = Depends(engine)):
        return [
            json.loads(row["data"])
            for row in service.store.rows("SELECT data FROM deployments ORDER BY timestamp DESC LIMIT 100")
        ]

    @app.post("/api/deploy", dependencies=authorized, tags=["发布控制"])
    def deploy(data: DeployInput, service: Engine = Depends(engine)):
        return service.deploy(data.version, data.experiment_id)

    @app.post("/api/rollback", dependencies=authorized, tags=["发布控制"])
    def rollback(data: RollbackInput, service: Engine = Depends(engine)):
        return service.rollback(data.expected_deployment_id)

    @app.post("/api/recover", dependencies=authorized, tags=["发布控制"])
    def recover(data: DeployInput, service: Engine = Depends(engine)):
        return service.recover(data.version)

    @app.put("/api/policy", dependencies=authorized, tags=["发布控制"])
    def policy(data: Policy, service: Engine = Depends(engine)):
        return service.update_policy(data)

    @app.post("/api/control/evaluate", dependencies=authorized, tags=["发布控制"])
    def evaluate(service: Engine = Depends(engine)):
        return service.evaluate()

    @app.post("/api/fault", dependencies=authorized, tags=["受控演练"])
    def fault(data: FaultInput, service: Engine = Depends(engine)):
        return service.set_fault(**data.model_dump())

    @app.post("/api/demo/prepare", dependencies=authorized, tags=["受控演练"])
    def prepare_demo(data: DemoInput, service: Engine = Depends(engine)):
        return service.prepare_demo(data.kind)

    @app.post("/api/demo/{experiment_id}/finish", dependencies=authorized, tags=["受控演练"])
    def finish_demo(experiment_id: str, service: Engine = Depends(engine)):
        return service.finish_demo(experiment_id)

    @app.post("/api/demo/{experiment_id}/abort", dependencies=authorized, tags=["受控演练"])
    def abort_demo(experiment_id: str, service: Engine = Depends(engine)):
        return service.abort_demo(experiment_id)

    @app.get("/api/experiments", tags=["审计"])
    def experiments(service: Engine = Depends(engine)):
        return [
            json.loads(row["data"])
            for row in service.store.rows("SELECT data FROM experiments ORDER BY timestamp DESC LIMIT 30")
        ]

    @app.get("/api/experiments/{experiment_id}", tags=["审计"])
    def experiment_report(experiment_id: str, download: bool = False, service: Engine = Depends(engine)):
        report = service.store.experiment(experiment_id)
        if not report:
            raise DomainError("演练报告不存在", 404)
        headers = (
            {"Content-Disposition": f'attachment; filename="experiment-{report["id"]}.json"'}
            if download
            else {}
        )
        return JSONResponse(report, headers=headers)

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request, service: Engine = Depends(engine)):
        window = service.window()
        for name, gauge in request.app.state.window_gauges.items():
            value = window[name]
            if name == "accuracy" and window["labeled_count"] < service.policy.min_labels:
                value = None
            gauge.set(math.nan if value is None else value / 1000 if name == "p95_ms" else value)
        for key, gauge in request.app.state.policy_gauges.items():
            value = service.state["policy"][key]
            gauge.set(value / 1000 if key == "p95_ms_threshold" else value)
        return Response(generate_latest(service.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    static = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(
            static / "index.html",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
                "Referrer-Policy": "no-referrer",
            },
        )

    return app
