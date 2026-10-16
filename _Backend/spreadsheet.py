"""Neutralize spreadsheet formulas without changing numeric values."""
def safe_cell(value):
    if isinstance(value, str) and value.lstrip(' \t\r\n\x00\x0b\x0c').startswith(('=', '+', '-', '@')):
        return "'" + value
    return value
