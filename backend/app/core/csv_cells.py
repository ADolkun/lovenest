"""Spreadsheet-safe text for CSV exports.

A spreadsheet evaluates any cell that starts with one of these as a formula,
so a description like "=HYPERLINK(...)" runs as live content when the export
is opened in Excel or Sheets (CSV injection, CWE-1236). Every free-text cell
of an export goes through `csv_safe`.
"""

_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: object) -> str:
    """`value` as text that a spreadsheet reads literally, never as a formula."""
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in _FORMULA_LEADERS else text
