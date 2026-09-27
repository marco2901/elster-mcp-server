"""Posteingang: Dateinamen und Zip-Auswertung ohne echten Browser."""

import io
import zipfile

import pytest

from elster_mcp.portal import sync
from elster_mcp.portal.sync import ascii_slug, extract_inbox_zip, inbox_basename, short_subject

EBW = "Energieberatung für Wohngebäude (EBW): Neue Dokumente in Ihrem Postfach verfügbar"
EBW_ZIP_STEM = "Energieberatung_fuer_Wohngebaeude__EBW___Neue_Dokumente_in_Ihrem_Postfach_verfuegbar"


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _msg(**kw):
    return {"elsterId": "581368313", "subject": EBW, "date": "2026-09-27", **kw}


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        (EBW, "EBW-Neue-Dokumente"),
        ("Informationen zu Ihrem Zertifikat", "Informationen-Zertifikat"),
        ("Bundesförderung für effiziente Gebäude (BEG EM): Neue Dokumente", "BEG-EM-Neue-Dokumente"),
        ("", "Nachricht"),
    ],
)
def test_short_subject(subject, expected):
    assert short_subject(subject) == expected


def test_ascii_slug_transliterates_and_collapses():
    assert ascii_slug("Größe __ für  Übergänge") == "Groesse-fuer-Uebergaenge"


def test_inbox_basename_schema():
    assert inbox_basename(_msg()) == "2026-09-27_ELSTER_581368313_EBW-Neue-Dokumente"
    assert inbox_basename(_msg(date=None)).startswith("undatiert_ELSTER_581368313_")


def test_extract_zip_saves_message_pdf_and_attachments(tmp_path):
    data = _zip({
        f"{EBW_ZIP_STEM}.pdf": b"%PDF-msg",
        f"{EBW_ZIP_STEM}.html": b"<html></html>",
        "Aufhebung_85402461.pdf": b"%PDF-bescheid",
    })
    message_pdf, attachments = extract_inbox_zip(data, tmp_path, _msg())
    assert message_pdf == str(tmp_path / "2026-09-27_ELSTER_581368313_EBW-Neue-Dokumente.pdf")
    assert (tmp_path / "2026-09-27_ELSTER_581368313_EBW-Neue-Dokumente.pdf").read_bytes() == b"%PDF-msg"
    assert attachments == [{
        "name": "Aufhebung_85402461.pdf",
        "path": str(tmp_path / "2026-09-27_ELSTER_581368313_EBW-Neue-Dokumente_Aufhebung_85402461.pdf"),
        "size": len(b"%PDF-bescheid"),
    }]
    assert not list(tmp_path.glob("*.html"))
    assert oct(next(tmp_path.glob("*Aufhebung*")).stat().st_mode & 0o777) == "0o600"


def test_extract_zip_ignores_paths_and_unknown_types(tmp_path):
    data = _zip({"../../evil/Bescheid.pdf": b"%PDF", "tool.exe": b"MZ"})
    _, attachments = extract_inbox_zip(data, tmp_path, _msg())
    assert [a["path"] for a in attachments] == [
        str(tmp_path / "2026-09-27_ELSTER_581368313_EBW-Neue-Dokumente_Bescheid.pdf")
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-09-27_ELSTER_581368313_EBW-Neue-Dokumente_Bescheid.pdf"]


def test_extract_zip_rejects_oversized(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "ZIP_MAX_TOTAL_BYTES", 10)
    with pytest.raises(ValueError):
        extract_inbox_zip(_zip({"a.pdf": b"x" * 100}), tmp_path, _msg())
