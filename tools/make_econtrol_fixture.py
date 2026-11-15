"""Write the E-Control test fixture: an xlsx laid out like E-Control's history file.

    python tools/make_econtrol_fixture.py   # writes tests/fixtures/Referenzmarktwert_Entwicklung.xlsx

The file is generated here instead of copying E-Control's file into the repository. It holds the
monthly values E-Control published (Cent/kWh; source: E-Control, www.e-control.at/referenzmarktwert)
in the same layout as the original: a title row, the column headers, a unit row, one row per month
(dates as Excel serial numbers) and a source line below. The headers are shared strings, like in the
original, so the parser's code paths stay covered. The output is byte-for-byte reproducible.
"""

import io
import zipfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parents[1] / "tests/fixtures/Referenzmarktwert_Entwicklung.xlsx"

# month, hydro, wind, photovoltaics, base price (Cent/kWh), publication date
VALUES = """
2022-01 18.07 18.51 20.6 18.9 2022-02-07
2022-02 16.57 16.3 16.55 16.78 2022-03-09
2022-03 27.69 29.37 27.32 28.29 2022-04-04
2022-04 18.38 17.31 16.79 18.62 2022-05-04
2022-05 17.96 16.8 15.91 18.45 2022-06-02
2022-06 21.95 21.37 21.05 22.83 2022-07-06
2022-07 34.79 32.35 33.92 35.91 2022-08-10
2022-08 49.69 46.19 47.79 49.38 2022-09-08
2022-09 38.1 35.74 39.48 38.64 2022-10-07
2022-10 17.88 17.92 16.46 17.51 2022-11-07
2022-11 21.47 20.55 20.9 21.25 2022-12-07
2022-12 23.86 26.44 28.79 26.12 2023-01-10
2023-01 14.53 14.49 15.56 14.46 2023-02-14
2023-02 14.38 13.65 13.99 14.46 2023-03-09
2023-03 11.24 10.59 10.48 11.34 2023-04-06
2023-04 10.41 10.58 8.56 10.48 2023-05-05
2023-05 8.26 8.71 5.43 8.23 2023-06-07
2023-06 9.37 9.67 7.54 9.48 2023-07-04
2023-07 8.62 7.84 6.53 8.48 2023-08-03
2023-08 8.93 7.77 7.5 9.27 2023-09-05
2023-09 10.23 9.47 7.7 10.14 2023-10-05
2023-10 9.97 9.47 8.14 9.92 2023-11-09
2023-11 9.35 9.13 9.2 9.36 2023-12-05
2023-12 7.12 6.6 7.48 7.32 2024-01-09
2024-01 8.11 8.06 8.03 8.13 2024-02-05
2024-02 6.61 6.38 6.18 6.58 2024-03-07
2024-03 6.47 5.82 4.62 6.36 2024-04-08
2024-04 6.08 5.61 3.12 5.87 2024-05-08
2024-05 6.88 6.03 3.34 6.414100806 2024-06-06
2024-06 6.92 6.05 4.24 6.77 2024-07-04
2024-07 6.51 5.94 4.27 6.34 2024-08-08
2024-08 8.82 8.23 5.75 8.51 2024-09-06
2024-09 7.94 7.74 5.86 8.19 2024-10-04
2024-10 8.45 7.67 6.19 8.56 2024-11-06
2024-11 13.3 12.5 12.08 13.08 2024-12-05
2024-12 13.3 11.56 13.32 12.97 2025-01-07
2025-01 13.63 12.5 13.15 13.38 2025-02-05
2025-02 14.36 13.61 12.52 14.08 2025-03-06
2025-03 10.54 9.83 6.41 10.39 2025-04-08
2025-04 8.46 8.1 2.63 8.1 2025-05-08
2025-05 7.51 7.08 1.49 7.06 2025-06-05
2025-06 6.94 6.87 1.61 6.6458 2025-07-03
2025-07 8.85 8.84 5.91 8.79 2025-08-04
2025-08 7.54 6.99 3.21 7.43 2025-09-08
2025-09 9.4 8.92 4.84 9.23 2025-10-06
2025-10 11.23 10.42 8.85 10.89 2025-11-06
2025-11 11.69 11.57 10.24 11.6 2025-12-04
2025-12 11.75 10.6 11.26 11.4 2026-01-07
2026-01 14.65 12.13 13.95 14.14 2026-02-09
2026-02 10.48 10.84 8.16 10.92 2026-03-04
2026-03 11.43 11.33 5.94 11.24 2026-04-07
2026-04 9.28 9.3 1.7 8.66 2026-05-05
2026-05 10.89 10.7 3.76 10 2026-06-03
2026-06 10.87 10.79 5.55 10.67 2026-07-02
2026-07 12.18 10.91 6.85 11.72 2026-08-06
2026-08 15.19 15.32 9.42 14.81 2026-09-04
2026-09 18.07 17.46 10.53 17.37 2026-10-02
2026-10 22.78 22.06 17.71 22.02 2026-11-04
"""

TITLE = "REFERENZMARKTWERT gem §13 EAG\n(Monatswerte ab 2022)"
HEADERS = [
    "Monat",
    "Wasserkraftanlagen",
    "Windkraftanlagen",
    "Photovoltaikanlagen",
    "Referenzmarktpreis\n[Day-Ahead Base]",
    "Datum Veröffentlichung",
]
UNIT = "Cent/kWh"
SOURCE = "Quelle: E-Control"
COLUMNS = "ABCDEF"


def serial(day: date) -> int:
    """Excel's date number: days since 1899-12-30."""
    return (day - date(1899, 12, 30)).days


def build(values: str = VALUES) -> bytes:
    """The xlsx file for `values` (lines as in VALUES; tests pass shortened tables)."""
    strings: list[str] = []

    def shared(text: str) -> str:
        if text not in strings:
            strings.append(text)
        return str(strings.index(text))

    rows: list[list[tuple[str, str]]] = [  # per row: (type, value) per column, "" = empty
        [("s", shared(TITLE))],
        [],
        [("s", shared(h)) for h in HEADERS],
        [("", "")] + [("s", shared(UNIT))] * 4,
    ]
    for line in values.split("\n"):
        if not line.strip():
            continue
        month, hydro, wind, pv, base, published = line.split()
        first = date.fromisoformat(month + "-01")
        numbers = [str(serial(first)), hydro, wind, pv, base, str(serial(date.fromisoformat(published)))]
        rows.append([("n", n) for n in numbers])
    rows += [[], [("s", shared(SOURCE))]]

    sheet_rows = []
    for index, row in enumerate(rows, start=1):
        cells = []
        for column, (kind, value) in zip(COLUMNS, row, strict=False):
            if not kind:
                continue
            kind_attr = ' t="s"' if kind == "s" else ""
            cells.append(f'<c r="{column}{index}"{kind_attr}><v>{value}</v></c>')
        sheet_rows.append(f'<row r="{index}">{"".join(cells)}</row>')

    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    office = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{office}.sheet.main+xml"/>'
            f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{office}.worksheet+xml"/>'
            f'<Override PartName="/xl/sharedStrings.xml" ContentType="{office}.sharedStrings+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{package_rel}">'
            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{main}" xmlns:r="{rel}">'
            '<sheets><sheet name="Referenzmarktwert" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{package_rel}">'
            f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{rel}/sharedStrings" Target="sharedStrings.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{main}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
        ),
        "xl/sharedStrings.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<sst xmlns="{main}" count="{len(strings)}" uniqueCount="{len(strings)}">'
            + "".join(f'<si><t xml:space="preserve">{escape(s)}</t></si>' for s in strings)
            + "</sst>"
        ),
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))  # fixed: reproducible bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text.encode())
    return buffer.getvalue()


if __name__ == "__main__":
    OUT.write_bytes(build())
    print("written to", OUT)
