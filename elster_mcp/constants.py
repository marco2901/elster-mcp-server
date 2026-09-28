"""Fachliche Konstanten: UStVA-Kennziffern, EÜR-Feldzuordnung, Portal-URLs."""

from __future__ import annotations

from typing import Literal, TypedDict


class Kennziffer(TypedDict):
    type: Literal["NET", "TAX"]
    description: str


#: UStVA-Kennziffern, Bezeichnungen nach dem ELSTER-Formular UStVA 2026 (Zeilennummern in Klammern).
#: NET = Bemessungsgrundlage (volle Euro), TAX = Steuerbetrag (Euro, Cent).
KENNZIFFERN: dict[str, Kennziffer] = {
    # Lieferungen und sonstige Leistungen
    "81": {"type": "NET", "description": "Steuerpflichtige Umsätze zum Steuersatz von 19 % (Z. 13, BMG)"},
    "86": {"type": "NET", "description": "Steuerpflichtige Umsätze zum Steuersatz von 7 % (Z. 14, BMG)"},
    "41": {"type": "NET", "description": "Innergemeinschaftliche Lieferungen (§ 4 Nr. 1 Buchst. b UStG) an Abnehmer mit USt-IdNr. (Z. 19)"},
    "48": {"type": "NET", "description": "Steuerfreie Umsätze ohne Vorsteuerabzug, z. B. § 4 Nr. 8 bis 29 oder § 19 Abs. 1 UStG (Z. 23)"},
    # Innergemeinschaftliche Erwerbe
    "89": {"type": "NET", "description": "Innergemeinschaftliche Erwerbe zum Steuersatz von 19 % (Z. 25, BMG)"},
    # Leistungsempfänger als Steuerschuldner (§ 13b UStG)
    "46": {"type": "NET", "description": "Sonstige Leistungen nach § 3a Abs. 2 UStG eines im übrigen Gemeinschaftsgebiet "
                                          "ansässigen Unternehmers (§ 13b Abs. 1 UStG) (Z. 30, BMG)"},
    "47": {"type": "TAX", "description": "Steuer zu Kz 46 (Z. 30)"},
    "73": {"type": "NET", "description": "Umsätze, die unter das GrEStG fallen (§ 13b Abs. 2 Nr. 3 UStG) (Z. 31, BMG)"},
    "74": {"type": "TAX", "description": "Steuer zu Kz 73 (Z. 31)"},
    "84": {"type": "NET", "description": "Andere Leistungen (§ 13b Abs. 2 Nr. 1, 2, 4 bis 12 UStG), u. a. Werklieferungen und "
                                          "sonstige Leistungen eines im Ausland ansässigen Unternehmers (Z. 32, BMG)"},
    "85": {"type": "TAX", "description": "Steuer zu Kz 84 (Z. 32)"},
    # Ergänzende Angaben zu Umsätzen
    "60": {"type": "NET", "description": "Steuerpflichtige Umsätze des leistenden Unternehmers, für die der Leistungsempfänger "
                                          "die Steuer nach § 13b Abs. 5 UStG schuldet (Z. 34)"},
    "45": {"type": "NET", "description": "Übrige nicht steuerbare Umsätze (Leistungsort nicht im Inland) (Z. 36)"},
    # Abziehbare Vorsteuerbeträge
    "66": {"type": "TAX", "description": "Vorsteuerbeträge aus Rechnungen von anderen Unternehmern (§ 15 Abs. 1 S. 1 Nr. 1 UStG) (Z. 38)"},
    "61": {"type": "TAX", "description": "Vorsteuerbeträge aus dem innergemeinschaftlichen Erwerb (§ 15 Abs. 1 S. 1 Nr. 3 UStG) (Z. 39)"},
    "67": {"type": "TAX", "description": "Vorsteuerbeträge aus Leistungen im Sinne des § 13b UStG (§ 15 Abs. 1 S. 1 Nr. 4 UStG) (Z. 41)"},
}

#: Von ELSTER berechnete Kennziffern: nie als Eingabe, dürfen aber in der Versand-Übersicht stehen.
COMPUTED_KZ: dict[str, str] = {
    "83": "Verbleibende Umsatzsteuer-Vorauszahlung bzw. verbleibender Überschuss (Z. 50, Rechnungsergebnis)",
}

#: Vorsteuer-Kennziffern dürfen nie negativ sein (typischer Vorzeichenfehler).
INPUT_TAX_KZ = frozenset({"61", "66", "67"})

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
