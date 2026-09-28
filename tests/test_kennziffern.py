"""UStVA-Kennziffern nach Formular 2026."""

import pytest
from pydantic import ValidationError

from elster_mcp import server
from elster_mcp.constants import COMPUTED_KZ, INPUT_TAX_KZ, KENNZIFFERN
from elster_mcp.models import UstvaReport


def test_13b_kennziffern_2026():
    kz = server.elster_kennziffern_list()
    assert kz["84"]["type"] == "NET" and "§ 13b Abs. 2 Nr. 1, 2, 4 bis 12" in kz["84"]["description"]
    assert kz["85"]["type"] == "TAX" and "Kz 84" in kz["85"]["description"]
    assert "GrEStG" in kz["73"]["description"] and "Nr. 3" in kz["73"]["description"]
    assert "Kz 73" in kz["74"]["description"]
    assert not any("Drittland" in v["description"] for v in kz.values())


def test_corrected_labels():
    assert "Erwerbe" in KENNZIFFERN["89"]["description"]
    assert KENNZIFFERN["60"]["type"] == "NET" and "§ 13b Abs. 5" in KENNZIFFERN["60"]["description"]
    assert "60" not in INPUT_TAX_KZ
    assert "ohne Vorsteuerabzug" in KENNZIFFERN["48"]["description"]


def test_kz83_is_computed_and_rejected():
    assert "83" not in KENNZIFFERN and "83" in COMPUTED_KZ
    with pytest.raises(ValidationError, match="berechnet ELSTER selbst"):
        UstvaReport(year=2026, period="Q3", report={"83": 40.66})


def test_report_accepts_84_85():
    r = UstvaReport(year=2026, period="Q3", report={"84": 100, "85": 19})
    assert r.report == {"84": 100.0, "85": 19.0}
