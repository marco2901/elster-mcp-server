"""UStVA-XML-Snapshot (nur Archiv, keine Übermittlung) und §13b-Erkennung."""

from __future__ import annotations

import re
from datetime import date
from xml.sax.saxutils import escape

from .config import ElsterConfig
from .models import UstvaReport
from .taxnumber import to_elster13

_OUTER_VERSION = "11"


def generate_ustva_xml(cfg: ElsterConfig, ustva: UstvaReport) -> str:
    """Erzeugt ein ELSTER-UStVA-XML zur Archivierung.

    Die tatsächliche Übermittlung läuft ausschließlich über das Online-Formular.
    Für eine XML-Übermittlung wäre die ERiC-Bibliothek nötig.

    Raises:
        TaxNumberError: wenn Steuernummer und Bundesland-Code nicht zusammenpassen.
    """
    schema_version = "2025" if ustva.year >= 2025 else "2023"
    # Im Datensatz steht die Steuernummer im bundeseinheitlichen 13-stelligen Format (wirft bei Fehlern).
    tax_number = to_elster13(cfg.taxpayer.tax_number, cfg.taxpayer.state_code) if cfg.taxpayer.tax_number else ""
    today = date.today().strftime("%Y%m%d")

    lines = [
        '<?xml version="1.0" encoding="ISO-8859-15" standalone="no"?>',
        f'<Elster xmlns="http://www.elster.de/elsterxml/schema/v{_OUTER_VERSION}" version="{_OUTER_VERSION}">',
        f'  <TransferHeader version="{_OUTER_VERSION}">',
        "    <Verfahren>ElsterAnmeldung</Verfahren>",
        "    <DatenArt>UStVA</DatenArt>",
        "    <Vorgang>send-Auth</Vorgang>",
        "    <HerstellerID>74999</HerstellerID>",
        "  </TransferHeader>",
        "  <DatenTeil>",
        "    <Nutzdatenblock>",
        f'      <NutzdatenHeader version="{_OUTER_VERSION}">',
        "        <NutzdatenArt>UStVA</NutzdatenArt>",
        '        <Empfaenger id="F">DE</Empfaenger>',
        "      </NutzdatenHeader>",
        "      <Nutzdaten>",
        f'        <Anmeldungssteuern xmlns="http://finkonsens.de/elster/elsteranmeldung/ustva/v{schema_version}" version="{schema_version}">',
        f"          <Erstellungsdatum>{today}</Erstellungsdatum>",
        "          <Steuerfall>",
        "            <Umsatzsteuervoranmeldung>",
        f"              <Jahr>{ustva.year}</Jahr>",
        f"              <Zeitraum>{escape(ustva.elster_period)}</Zeitraum>",
    ]
    if tax_number:
        lines.append(f"              <Steuernummer>{tax_number}</Steuernummer>")
    for code in sorted(ustva.report, key=int):
        value = ustva.report[code]
        if value == 0:
            continue
        lines.append(f"              <Kz{code}>{value:.2f}</Kz{code}>")
    lines += [
        "            </Umsatzsteuervoranmeldung>",
        "          </Steuerfall>",
        "        </Anmeldungssteuern>",
        "      </Nutzdaten>",
        "    </Nutzdatenblock>",
        "  </DatenTeil>",
        "</Elster>",
    ]
    return "\n".join(lines)


_EXPLICIT_RC = re.compile(
    r"reverse[\s-]?charge|steuerschuldnerschaft\s+des\s+leistungsempf(ä|ae)ngers|tax\s+to\s+be\s+paid\s+on\s+reverse\s+charge",
    re.IGNORECASE,
)
_NON_EU_HINT = re.compile(r"usa|america|inc\.|llc|pbc", re.IGNORECASE)


def detect_reverse_charge(cfg: ElsterConfig, contact_name: str | None, description: str | None) -> dict | None:
    name = (contact_name or "").strip()
    desc = description or ""
    for s in cfg.ustva.reverse_charge_suppliers:
        try:
            rx = re.compile(s.pattern, re.IGNORECASE)
        except re.error:
            continue
        if rx.search(name) or rx.search(desc):
            return {"region": s.region, "supplier": s.name}
    if _EXPLICIT_RC.search(desc):
        return {"region": "NON_EU" if _NON_EU_HINT.search(name) else "EU", "supplier": name or "Unknown"}
    return None
