"""The platform's parameters: every key has a default, unknown keys are ignored (an older or newer release's)."""

from functools import lru_cache
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_TIMEZONE = "America/Mexico_City"


@lru_cache(maxsize=1)
def _timezones() -> frozenset[str]:
    return frozenset(available_timezones())  # the system's zones, or those `tzdata` ships where it has none


def is_timezone(name: str) -> bool:
    """An IANA zone, spelled exactly ("America/Bogota"): a case-insensitive disk would also open "america/bogota"
    here, and the server would then fail to."""
    return name in _timezones()


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class GeneralSettings(_Section):
    company_name: str = Field(default="", max_length=200)  # the issuer of the certificates ("" = the platform)
    timezone: str = DEFAULT_TIMEZONE  # where dates are written (and, later, where "today" starts)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        if not is_timezone(value):
            raise ValueError("Zona horaria desconocida")
        return value


class EvaluationDefaults(_Section):
    """What a new evaluation starts with (by hand or drafted by the AI); each one can change them."""

    # From 1, like an evaluation's own: 0 would read as "unset" (and fall back to 70) on older rows.
    passing_score: int = Field(default=70, ge=1, le=100)
    max_attempts: int = Field(default=3, ge=1, le=10)


class CertificateSettings(_Section):
    enabled: bool = True  # issued by themselves on completion (a course can opt in or out)
    validity_months: int | None = Field(default=None, ge=1, le=120)  # None: they never expire
    min_score: int | None = Field(default=None, ge=0, le=100)  # average best quiz score a course certificate needs
    signer_name: str = Field(default="", max_length=200)
    signer_title: str = Field(default="", max_length=200)


class PlatformSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    general: GeneralSettings = Field(default_factory=GeneralSettings)
    evaluations: EvaluationDefaults = Field(default_factory=EvaluationDefaults)
    certificates: CertificateSettings = Field(default_factory=CertificateSettings)
