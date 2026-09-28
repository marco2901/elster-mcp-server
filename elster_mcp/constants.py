"""Fachliche Konstanten: UStVA-Kennziffern, EÜR-Feldzuordnung, Portal-URLs."""

from __future__ import annotations

from typing import Literal, TypedDict


class Kennziffer(TypedDict):
    type: Literal["NET", "TAX"]
    description: str


#: UStVA-Kennziffern (Stand 2025). NET = Bemessungsgrundlage, TAX = Steuerbetrag.
KENNZIFFERN: dict[str, Kennziffer] = {
    "81": {"type": "NET", "description": "Steuerpflichtige Umsätze 19%"},
    "86": {"type": "NET", "description": "Steuerpflichtige Umsätze 7%"},
    "83": {"type": "NET", "description": "Steuerfreie Umsätze ohne Vorsteuerabzug"},
    "41": {"type": "NET", "description": "Innergemeinschaftliche Lieferungen"},
    "45": {"type": "NET", "description": "Übrige nicht steuerbare Umsätze"},
    "89": {"type": "NET", "description": "Steuerpflichtige EG-Lieferungen"},
    "60": {"type": "TAX", "description": "Übrige Vorsteuer"},
    "61": {"type": "TAX", "description": "Vorsteuer aus innergemeinschaftlichem Erwerb"},
    "66": {"type": "TAX", "description": "Vorsteuer aus Rechnungen (§15 UStG)"},
    "67": {"type": "TAX", "description": "Vorsteuer aus Reverse-Charge §13b UStG"},
    # Reverse-Charge §13b UStG
    "46": {"type": "NET", "description": "Sonstige Leistung EU-Unternehmer §13b Abs.1 (BMG 19%)"},
    "47": {"type": "TAX", "description": "USt auf KZ 46 (selbstberechnet)"},
    "73": {"type": "NET", "description": "Leistungen §13b Abs.2 Nr.1-5 (Drittland; BMG 19%)"},
    "74": {"type": "TAX", "description": "USt auf KZ 73 (selbstberechnet)"},
}

#: Vorsteuer-Kennziffern dürfen nie negativ sein (typischer Vorzeichenfehler).
INPUT_TAX_KZ = frozenset({"60", "61", "66", "67"})

class EurField(TypedDict):
    field: str
    labels: list[str]
    kz_patterns: list[str]


EUR_FIELD_MAP: list[EurField] = [
    {"field": "betriebseinnahmen", "labels": ["Betriebseinnahmen", "steuerpflichtige Betriebseinnahmen", "Umsatzerlöse"], "kz_patterns": ["Kz111", "Kz100"]},
    {"field": "kfzPrivatNutzung", "labels": ["Entnahmen", "private Kfz-Nutzung", "Privatanteile"], "kz_patterns": ["Kz185", "Kz180"]},
    {"field": "fahrzeugkosten", "labels": ["Fahrzeugkosten", "Kfz-Kosten", "Kraftfahrzeugkosten"], "kz_patterns": ["Kz175"]},
    {"field": "kfzSteuer", "labels": ["Kfz-Steuer", "Kraftfahrzeugsteuer"], "kz_patterns": ["Kz166"]},
    {"field": "telekommunikation", "labels": ["Telekommunikation", "Telefon", "Internet"], "kz_patterns": ["Kz155"]},
    {"field": "versicherungen", "labels": ["Versicherungen", "Beiträge"], "kz_patterns": ["Kz165"]},
    {"field": "bewirtung", "labels": ["Bewirtung", "Geschäftsessen"], "kz_patterns": ["Kz157"]},
    {"field": "reisekosten", "labels": ["Reisekosten"], "kz_patterns": ["Kz177"]},
    {"field": "bankgebuehren", "labels": ["Bankgebühren", "Kontoführung"], "kz_patterns": ["Kz169"]},
    {"field": "fremdleistungen", "labels": ["Fremdleistungen", "Subunternehmer"], "kz_patterns": ["Kz135"]},
    {"field": "software", "labels": ["Software", "Lizenzen"], "kz_patterns": ["Kz140"]},
    {"field": "buchfuehrung", "labels": ["Buchführungskosten", "Steuerberatung", "Rechts- und Steuerberatung"], "kz_patterns": ["Kz181"]},
    {"field": "beratung", "labels": ["Rechts- und Beratungskosten", "Beratungskosten"], "kz_patterns": ["Kz181"]},
    {"field": "werbung", "labels": ["Werbung", "Werbekosten"], "kz_patterns": ["Kz178"]},
    {"field": "gwg", "labels": ["Geringwertige Wirtschaftsgüter", "GWG"], "kz_patterns": ["Kz131"]},
    {"field": "steuern", "labels": ["Steuern, Versicherungen", "sonstige Steuern"], "kz_patterns": ["Kz167"]},
    {"field": "uebrigeBA", "labels": ["übrige unbeschränkt abziehbare Betriebsausgaben", "übrige Betriebsausgaben", "sonstige Betriebsausgaben"], "kz_patterns": ["Kz183"]},
    {"field": "afa", "labels": ["Absetzung für Abnutzung auf bewegliche Wirtschaftsgüter", "Absetzung für Abnutzung", "AfA auf bewegliche", "AfA"], "kz_patterns": ["Kz131", "Kz150", "Kz136"]},
    {"field": "homeOffice", "labels": ["Aufwendungen für ein häusliches Arbeitszimmer", "häusliches Arbeitszimmer", "Arbeitszimmer", "Home-Office", "Raumkosten"], "kz_patterns": ["Kz176", "Kz170"]},
    {"field": "iabAbzug", "labels": ["Investitionsabzugsbetrag", "Investitionsabzugsbeträge", "§ 7g Absatz 1", "§ 7g Abs. 1"], "kz_patterns": ["Kz187", "Kz216"]},
]

EUR_FIELDS = frozenset(f["field"] for f in EUR_FIELD_MAP)

PORTAL_URLS = {
    "start": "https://www.elster.de/eportal/start",
    "ustva_form": "https://www.elster.de/eportal/formulare-leistungen/alleformulare/ustvaeru",
    "eur_form": "https://www.elster.de/eportal/formulare-leistungen/alleformulare/euer",
    "est_form": "https://www.elster.de/eportal/formulare-leistungen/alleformulare/est",
    "meine_formulare": "https://www.elster.de/eportal/meineformulare",
    "posteingang": "https://www.elster.de/eportal/meinelster/meinposteingang",
}
