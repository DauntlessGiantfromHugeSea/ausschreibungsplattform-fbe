"""Export-Helfer fuer CSV und Excel."""
from __future__ import annotations

import csv
import io
from typing import Iterable, List

import pandas as pd

from .models import Tender


COLUMNS = [
    "id",
    "title",
    "portal",
    "contracting_authority",
    "location",
    "region",
    "publication_date",
    "deadline",
    "url",
    "matched_terms",
    "relevance_score",
    "relevance_level",
    "status",
    "cpv_codes",
    "notes",
]


def _rows(tenders: Iterable[Tender]) -> List[dict]:
    return [
        {col: t.to_dict().get(col) for col in COLUMNS}
        for t in tenders
    ]


def to_csv(tenders: Iterable[Tender]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()
    for row in _rows(tenders):
        writer.writerow(row)
    return buf.getvalue()


def to_xlsx(tenders: Iterable[Tender]) -> bytes:
    df = pd.DataFrame(_rows(tenders), columns=COLUMNS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Ausschreibungen", index=False)
    return buf.getvalue()
