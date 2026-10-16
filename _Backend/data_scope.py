"""Shared provenance filters for reports and operational decisions."""
SOURCES = {'all': 'All records', 'real': 'Real records', 'synthetic': 'Synthetic records'}


def parse_source(value):
    source = value or 'real'
    if source not in SOURCES:
        raise ValueError('Choose all, real, or synthetic records.')
    return source


def source_sql(source, alias):
    # Callers supply code-owned table aliases; the user chooses a validated enum.
    parse_source(source)
    if source == 'all':
        return '1=1'
    return f"COALESCE({alias}.is_synthetic,0)={'1' if source == 'synthetic' else '0'}"
