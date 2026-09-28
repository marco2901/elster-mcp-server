"""Steuernummer: Format laut Bescheid (Landesschema) → bundeseinheitliches 13-stelliges ELSTER-Format.

Aufbau des 13-stelligen Formats: 4-stellige Bundesfinanzamtsnummer + "0" + 8 Stellen
(Bezirk, Unterscheidungsnummer, Prüfziffer). Die Bundesfinanzamtsnummer ergibt sich aus
einer Landeskennung und der Finanzamtsnummer des Landesschemas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class TaxNumberError(ValueError):
    pass


@dataclass(frozen=True)
class _Rule:
    name: str
    codes: tuple[str, ...]   # akzeptierte ELSTER_STATE_CODE-Werte
    local_len: int           # Stellen im Landesschema
    lead: str                # führende Ziffer(n) im Landesschema, die entfallen ("" = keine)
    prefix: str              # Landeskennung im 13-stelligen Format
    head_start: str = ""     # erwartete erste Ziffer der Finanzamtsnummer (nach dem Entfernen von lead)


_RULES = (
    _Rule("Baden-Württemberg", ("28", "BW"), 10, "", "28"),
    _Rule("Bayern", ("9", "09", "BY"), 11, "", "9"),
    _Rule("Berlin", ("11", "BE"), 10, "", "11"),
    _Rule("Brandenburg", ("30", "BB"), 11, "0", "30"),
    _Rule("Bremen", ("24", "HB"), 10, "", "24"),
    _Rule("Hamburg", ("22", "HH"), 10, "", "22"),
    _Rule("Hessen", ("26", "HE"), 11, "0", "26"),
    _Rule("Mecklenburg-Vorpommern", ("40", "MV"), 11, "0", "40"),
    _Rule("Niedersachsen", ("23", "NI"), 10, "", "23"),
    _Rule("Nordrhein-Westfalen", ("5", "05", "NW"), 11, "", "5"),
    _Rule("Rheinland-Pfalz", ("27", "RP"), 10, "", "27"),
    _Rule("Saarland", ("10", "SL"), 11, "0", "10"),
    _Rule("Sachsen", ("32", "SN"), 11, "", "3", head_start="2"),
    _Rule("Sachsen-Anhalt", ("31", "ST"), 11, "", "3", head_start="1"),
    _Rule("Schleswig-Holstein", ("21", "SH"), 10, "", "21"),
    _Rule("Thüringen", ("41", "TH"), 11, "", "4", head_start="1"),
)
_BY_CODE = {code: rule for rule in _RULES for code in rule.codes}


def _rule(state_code: str) -> _Rule:
    rule = _BY_CODE.get(state_code.strip().upper())
    if not rule:
        raise TaxNumberError(f"Unbekannter Bundesland-Code {state_code!r} (ELSTER_STATE_CODE).")
    return rule


def to_elster13(tax_number: str, state_code: str) -> str:
    """Wandelt eine Steuernummer ins 13-stellige ELSTER-Format um; 13-stellige Eingaben werden geprüft.

    Beispiel Hessen (26): ``030 806 31273`` → ``2630080631273``.
    """
    rule = _rule(state_code)
    digits = re.sub(r"\D", "", tax_number)
    if not digits:
        raise TaxNumberError("Keine Steuernummer konfiguriert (ELSTER_TAX_NUMBER).")

    if len(digits) == 13:
        if not digits.startswith(rule.prefix + rule.head_start) or digits[4] != "0":
            raise TaxNumberError(f"13-stellige Steuernummer passt nicht zu {rule.name}.")
        return digits

    # Nur exakt das Format laut Bescheid – eine zu kurz abgeschriebene Nummer wird nicht „repariert“.
    if len(digits) != rule.local_len:
        raise TaxNumberError(
            f"Steuernummer für {rule.name} hat {len(digits)} Stellen, erwartet {rule.local_len} (laut Bescheid) oder 13."
        )
    if rule.lead and not digits.startswith(rule.lead):
        raise TaxNumberError(f"Steuernummer für {rule.name} muss mit {rule.lead} beginnen (Format laut Bescheid).")
    local = digits[len(rule.lead):]
    head, tail = local[:-8], local[-8:]
    if rule.head_start and not head.startswith(rule.head_start):
        raise TaxNumberError(f"Steuernummer für {rule.name} muss mit {rule.head_start} beginnen.")
    result = rule.prefix + head + "0" + tail
    if len(result) != 13:  # Schutz gegen Tabellenfehler
        raise TaxNumberError("Interner Fehler bei der Steuernummer-Umwandlung.")
    return result


def tax_id_check_digit(first10: str) -> int:
    """Prüfziffer der Steuer-Identifikationsnummer (§ 139b AO, ISO 7064 MOD 11,10)."""
    product = 10
    for ch in first10:
        total = (int(ch) + product) % 10 or 10
        product = (total * 2) % 11
    check = 11 - product
    return 0 if check == 10 else check


def validate_tax_id(value: str) -> str:
    """Steuer-Identifikationsnummer: 11 Ziffern, keine führende 0, gültige Prüfziffer."""
    digits = re.sub(r"\D", "", value)
    if len(digits) != 11 or digits[0] == "0":
        raise TaxNumberError("Steuer-Identifikationsnummer muss 11 Ziffern haben und darf nicht mit 0 beginnen.")
    if tax_id_check_digit(digits[:10]) != int(digits[10]):
        raise TaxNumberError("Steuer-Identifikationsnummer: Prüfziffer passt nicht (Tippfehler?).")
    return digits
