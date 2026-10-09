"""Explicit GS1 barcode validation; a barcode never establishes consumption."""

import re


def normalize_barcode(value: str) -> str:
    code = value.strip()
    if not re.fullmatch(r"(?:[0-9]{8}|[0-9]{12,14})", code) or not code.strip("0"):
        raise ValueError("Enter a readable 8, 12, 13 or 14 digit product barcode.")
    weighted = sum(
        int(digit) * (3 if index % 2 == 0 else 1) for index, digit in enumerate(reversed(code[:-1]))
    )
    if int(code[-1]) != (-weighted) % 10:
        raise ValueError("The barcode check digit does not match. Check the printed digits.")
    # OFF's canonical identifier removes surplus leading zeros then pads short
    # codes to EAN-8, or ordinary consumer codes to EAN-13. GTIN-14 stays distinct.
    significant = code.lstrip("0")
    if len(significant) <= 8:
        return significant.zfill(8)
    if len(significant) <= 13:
        return significant.zfill(13)
    return significant
