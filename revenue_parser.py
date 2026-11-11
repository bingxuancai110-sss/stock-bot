"""Named MOPS columns only: month-on-month is never cumulative year-on-year."""
import csv
from html.parser import HTMLParser
from io import StringIO
import math
import re

VERSION = "mops-named-columns-v2"


def number(value):
    text = str(value or "").strip().replace(",", "").replace("%", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        value = float(text)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"th", "td"} and self.row is not None:
            self.cell = []

    def handle_data(self, text):
        if self.cell is not None:
            self.cell.append(text)

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row, self.cell = None, None


def parse_rows(rows, target_code=None):
    result, columns = {}, None
    aliases = {
        "code": ("公司代號",), "month": ("當月營收", "營業收入-當月營收"),
        "yoy": ("去年同月增減(%)", "去年同月增減百分比", "去年同月增減", "營業收入-去年同月增減(%)"),
        "mom": ("上月比較增減(%)", "前月比較增減(%)", "上月比較增減百分比", "前月比較增減", "營業收入-上月比較增減(%)"),
        "cum": ("前期比較增減(%)", "累計營收增減(%)", "累計營收增減", "累計營業收入-前期比較增減(%)"),
        "current": ("當月累計營收", "今年累計營收", "累計營收", "累計營業收入-當月累計營收"),
        "prior": ("去年累計營收", "前期累計營收", "累計營業收入-去年累計營收"),
    }
    for row in rows:
        header = [re.sub(r"\s+", "", str(x or "").replace("\ufeff", "")).replace("（", "(").replace("）", ")") for x in row]
        if "公司代號" in header:
            if any(sum(h in names for h in header) > 1 for names in aliases.values()):
                raise ValueError("官方營收欄名重複，不能判斷正確欄位")
            columns = {key: next((i for i, h in enumerate(header) if h in names), None)
                       for key, names in aliases.items()}
            if columns["month"] is None:
                columns = None
            continue
        if not columns:
            continue
        def cell(key):
            index = columns.get(key)
            return row[index] if index is not None and index < len(row) else None
        code = str(cell("code") or "").strip().upper()
        if not re.fullmatch(r"\d{4,6}[A-Z]?", code) or (target_code and code != str(target_code).strip().upper()):
            continue
        monthly = number(cell("month"))
        if monthly is None:
            continue
        cumulative, current, prior = number(cell("cum")), number(cell("current")), number(cell("prior"))
        if current is not None and prior is not None and prior > 0:
            calculated = (current / prior - 1) * 100
            if cumulative is None:
                cumulative = round(calculated, 2)
            elif abs(cumulative - calculated) > 0.05:
                raise ValueError("官方累計增減與同列金額不一致：" + code)
        item = {"month_revenue": monthly, "yoy_pct": number(cell("yoy")),
                "mom_pct": number(cell("mom")), "cum_yoy_pct": cumulative,
                "parser_version": VERSION}
        if code in result and result[code] != item:
            raise ValueError("官方營收出現矛盾重複列：" + code)
        result[code] = item
    return result


def parse_html(text, target_code=None):
    parser = TableParser()
    parser.feed(text or "")
    parser.close()
    return parse_rows(parser.rows, target_code)


def parse_csv(raw, target_code=None):
    for encoding in ("utf-8-sig", "cp950", "big5"):
        try:
            text = raw.decode(encoding)
            return parse_rows(csv.reader(StringIO(text)), target_code)
        except UnicodeDecodeError:
            continue
    return {}
