"""Shared provenance filters for reports and operational decisions."""
SOURCES = {'all': 'All records', 'real': 'Live operations', 'synthetic': 'Presentation history'}


def demo_staff_ids(cursor):
    """Read provenance IDs, never infer fictional identities from a name."""
    import json
    if not cursor.execute("SELECT 1 FROM sqlite_master WHERE name='demo_history_batches'").fetchone():
        return set()
    ids = set()
    for row in cursor.execute('SELECT summary_json FROM demo_history_batches'):
        summary = json.loads(row[0])
        ids.update(value for value in summary.get('staff_ids', []) if type(value) is int)
    return ids


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
