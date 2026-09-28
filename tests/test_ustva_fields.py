"""UStVA: Kennziffer-Felder anhand der ELSTER-Feld-IDs zuordnen und Werte formatieren."""

import pytest

from elster_mcp.portal.ustva import format_kz_value, kz_of_field

P = "Startseite(0)_LeistungsempfaengerAlsSteuerschuldner(0)_fields(eruAnmeldungssteuernSteuerfallUmsatzsteuervoranmeldung"


@pytest.mark.parametrize(
    ("field_id", "name", "kz"),
    [
        (P + "Kz46)", "", "46"),
        (P + "Kz47)", "", "47"),
        ("", "fields[eruAnmeldungssteuernSteuerfallUmsatzsteuervoranmeldungKz73]", "73"),
        (P + "Kz046)", "", "46"),         # führende Null
        (P + "Kz46Steuer)", "", None),     # anderes Feld, das nur mit Kz46 beginnt
        (P + "Kz4)", "", "4"),
        ("searchFieldInput_header", "suchstring", None),
    ],
)
def test_kz_of_field(field_id, name, kz):
    assert kz_of_field(field_id, name) == kz


def test_kz_prefix_does_not_match_longer_number():
    assert kz_of_field(P + "Kz46)") != "4"


@pytest.mark.parametrize(
    ("value", "placeholder", "expected"),
    [
        (214, "Euro", "214"),
        (40.66, "Euro, Cent", "40,66"),
        (40.6, "Euro, Cent", "40,60"),
        (1845.3, "Euro, Cent", "1845,30"),
        (12000, "Euro", "12000"),
    ],
)
def test_format_kz_value(value, placeholder, expected):
    assert format_kz_value(value, placeholder) == expected
