"""Umwandlung der Steuernummer ins 13-stellige ELSTER-Format (Beispielnummern der Länder)."""

import pytest

from elster_mcp import server
from elster_mcp.config import reset_config_cache
from elster_mcp.taxnumber import TaxNumberError, to_elster13


@pytest.mark.parametrize(
    ("state", "local", "elster13"),
    [
        ("28", "93815/08152", "2893081508152"),     # Baden-Württemberg
        ("9", "181/815/08155", "9181081508155"),    # Bayern
        ("11", "21/815/08150", "1121081508150"),    # Berlin
        ("30", "048/815/08155", "3048081508155"),   # Brandenburg
        ("24", "75 815 08152", "2475081508152"),    # Bremen
        ("22", "02/815/08156", "2202081508156"),    # Hamburg
        ("26", "013 815 08153", "2613081508153"),   # Hessen
        ("40", "079/815/08151", "4079081508151"),   # Mecklenburg-Vorpommern
        ("23", "24/815/08151", "2324081508151"),    # Niedersachsen
        ("5", "133/8150/8159", "5133081508159"),    # Nordrhein-Westfalen
        ("27", "22/815/0815/4", "2722081508154"),   # Rheinland-Pfalz
        ("10", "010/815/08182", "1010081508182"),   # Saarland
        ("32", "201/123/12340", "3201012312340"),   # Sachsen
        ("31", "101/815/08154", "3101081508154"),   # Sachsen-Anhalt
        ("21", "29/815/08158", "2129081508158"),    # Schleswig-Holstein
        ("41", "151/815/08156", "4151081508156"),   # Thüringen
    ],
)
def test_local_to_elster13(state, local, elster13):
    assert to_elster13(local, state) == elster13
    # 13-stellige Eingabe bleibt unverändert
    assert to_elster13(elster13, state) == elster13


def test_hessen_case_from_bug_report():
    assert to_elster13("03080631273", "26") == "2630080631273"
    assert to_elster13("030/806/31273", "HE") == "2630080631273"


@pytest.mark.parametrize(
    ("number", "state"),
    [
        ("03080631273", ""),            # kein Bundesland
        ("03080631273", "99"),          # unbekanntes Bundesland
        ("13080631273", "26"),          # Hessen muss mit 0 beginnen
        ("0308063127", "26"),           # zu kurz
        ("30/806/31273", "26"),         # führende 0 fehlt – nicht raten
        ("2830080631273", "26"),        # 13 Stellen, aber anderes Land
        ("301/123/12340", "32"),        # Sachsen muss mit 2 beginnen
        ("", "26"),
    ],
)
def test_invalid(number, state):
    with pytest.raises(TaxNumberError):
        to_elster13(number, state)


def test_security_check_reports_format(monkeypatch):
    monkeypatch.setenv("ELSTER_TAX_NUMBER", "03080631273")
    monkeypatch.setenv("ELSTER_STATE_CODE", "26")
    reset_config_cache()
    tn = server.elster_security_check()["taxNumber"]
    assert tn["ok"] is True
    assert tn["elsterFormat"] == "*********1273"

    monkeypatch.setenv("ELSTER_STATE_CODE", "28")
    reset_config_cache()
    tn = server.elster_security_check()["taxNumber"]
    assert tn["ok"] is False
    assert "Baden-Württemberg" in tn["error"]
