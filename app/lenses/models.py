"""
Shared Pydantic models for lens results.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Finding(BaseModel):
    severity: Severity
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    # Populated by the Regression Lens to cite the violated PROJECT_RULES.md section
    rule_section: Optional[str] = None


class LensResult(BaseModel):
    lens_name: str
    passed: bool
    findings: list[Finding] = Field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.WARNING)
