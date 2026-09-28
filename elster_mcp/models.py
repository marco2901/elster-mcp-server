"""Strikte Eingabevalidierung für alle Tools, die Daten ins Portal schreiben."""

from __future__ import annotations

import math
import re
from datetime import date

from pydantic import BaseModel, field_validator

from .constants import COMPUTED_KZ, EUR_FIELDS, INPUT_TAX_KZ, KENNZIFFERN

_MAX_AMOUNT = 100_000_000  # Plausibilitätsgrenze gegen Tippfehler (z. B. Cent statt Euro)
_PERIOD_RE = re.compile(r"^(Q[1-4]|0?[1-9]|1[0-2])$")


def _check_year(year: int) -> int:
    if not 2000 <= year <= date.today().year + 1:
        raise ValueError(f"Unplausibles Jahr: {year}")
    return year


def _check_amount(key: str, value: float) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{key}: Betrag ist keine endliche Zahl")
    if abs(value) > _MAX_AMOUNT:
        raise ValueError(f"{key}: Betrag {value} überschreitet die Plausibilitätsgrenze")
    return round(value, 2)


def normalize_period(period: int | str) -> str:
    """``3`` → ``"03"``, ``"Q2"`` → ``"42"`` (ELSTER-Zeitraumcode)."""
    p = str(period).strip().upper()
    if not _PERIOD_RE.match(p):
        raise ValueError(f"Ungültiger Zeitraum: {period!r} (erlaubt: 1-12 oder Q1-Q4)")
    if p.startswith("Q"):
        return f"4{p[1]}"
    return f"{int(p):02d}"


class UstvaReport(BaseModel):
    year: int
    period: int | str
    report: dict[str, float]

    @field_validator("year")
    @classmethod
    def _year(cls, v: int) -> int:
        return _check_year(v)

    @field_validator("period")
    @classmethod
    def _period(cls, v: int | str) -> int | str:
        normalize_period(v)
        return v

    @field_validator("report")
    @classmethod
    def _report(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("report ist leer")
        clean: dict[str, float] = {}
        for raw_key, amount in v.items():
            key = raw_key.removeprefix("Kz").removeprefix("KZ").lstrip("0") or "0"
            if key in COMPUTED_KZ:
                raise ValueError(f"Kz{key} ({COMPUTED_KZ[key]}) berechnet ELSTER selbst – bitte nicht angeben")
            if key not in KENNZIFFERN:
                raise ValueError(f"Unbekannte Kennziffer {raw_key!r} – siehe elster_kennziffern_list")
            amount = _check_amount(f"Kz{key}", float(amount))
            if key in INPUT_TAX_KZ and amount < 0:
                raise ValueError(f"Vorsteuer Kz{key} ist negativ ({amount}) – Vorzeichenfehler?")
            clean[key] = amount
        return clean

    @property
    def elster_period(self) -> str:
        return normalize_period(self.period)


class EurData(BaseModel):
    year: int
    data: dict[str, float]

    @field_validator("year")
    @classmethod
    def _year(cls, v: int) -> int:
        return _check_year(v)

    @field_validator("data")
    @classmethod
    def _data(cls, v: dict[str, float]) -> dict[str, float]:
        unknown = sorted(set(v) - EUR_FIELDS)
        if unknown:
            raise ValueError(f"Unbekannte EÜR-Felder: {unknown}. Erlaubt: {sorted(EUR_FIELDS)}")
        return {k: _check_amount(k, float(a)) for k, a in v.items()}


class EstData(BaseModel):
    year: int
    data: dict[str, float | str] = {}

    @field_validator("year")
    @classmethod
    def _year(cls, v: int) -> int:
        return _check_year(v)

    @field_validator("data")
    @classmethod
    def _data(cls, v: dict[str, float | str]) -> dict[str, float | str]:
        for key, value in v.items():
            if not re.fullmatch(r"[A-Za-z0-9_.\-]{2,80}", key):
                raise ValueError(f"Ungültiger Feld-Hinweis {key!r}")
            if isinstance(value, str) and len(value) > 500:
                raise ValueError(f"Wert für {key} ist zu lang")
            if isinstance(value, float):
                _check_amount(key, value)
        return v
