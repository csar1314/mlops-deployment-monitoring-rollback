import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_seconds: float = Field(30, ge=2, le=3600)
    min_requests: int = Field(12, ge=2, le=10000)
    min_labels: int = Field(12, ge=2, le=10000)
    error_rate_threshold: float = Field(0.25, gt=0, le=1)
    p95_ms_threshold: float = Field(180, ge=10, le=60000)
    accuracy_floor: float = Field(0.8, gt=0, le=1)
    consecutive_breaches: int = Field(3, ge=1, le=30)
    min_breach_seconds: float = Field(2, ge=0, le=300)
    cooldown_seconds: float = Field(8, ge=0, le=3600)
    promotion_min_requests: int = Field(20, ge=2, le=10000)


@dataclass
class Settings:
    data_dir: Path
    token: str = ""
    demo_enabled: bool = False
    controller_enabled: bool = True
    interval: float = 1.0
    bootstrap: bool = True

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.getenv("MLOPS_DATA_DIR", "runtime")).resolve(),
            token=os.getenv("MLOPS_ADMIN_TOKEN", ""),
            demo_enabled=os.getenv("MLOPS_ENABLE_DEMO", "0") == "1",
            controller_enabled=os.getenv("MLOPS_CONTROLLER", "1") == "1",
            interval=float(os.getenv("MLOPS_CONTROLLER_INTERVAL", "1")),
        )

    def ensure_token(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        token_file = self.data_dir / "admin-token.txt"
        if not self.token:
            self.token = token_file.read_text().strip() if token_file.exists() else secrets.token_urlsafe(32)
        token_file.write_text(self.token, encoding="utf-8")
        try:
            token_file.chmod(0o600)
        except OSError:
            pass
        return self.token
