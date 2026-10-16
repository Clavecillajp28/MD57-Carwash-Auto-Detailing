"""Shop settings the owner changes on screen (the app_settings table).

Each setting has a default, an allowed range, and an optional .env
override for the default - the saved value always wins over both."""
import os

from database import get_db_connection
from utils import now_str

SETTINGS = {
    # How many staff work on one car at a time - Analytics' recommended
    # staffing multiplies the cars being washed at once by this.
    'crew_per_car': {'default': int(os.environ.get('ANALYTICS_CREW_PER_CAR', 2)), 'min': 1, 'max': 4},
}


def get_setting(key):
    spec = SETTINGS[key]
    conn = get_db_connection()
    row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key = ?", (key,)).fetchone()
    conn.close()
    try:
        value = int(row['setting_value']) if row else spec['default']
    except (TypeError, ValueError):
        value = spec['default']
    return min(spec['max'], max(spec['min'], value))


def all_settings():
    return {key: {'value': get_setting(key), 'min': spec['min'], 'max': spec['max']} for key, spec in SETTINGS.items()}


def save_setting(key, value, username):
    """Saves an allowed value; returns an error message or None."""
    spec = SETTINGS.get(key)
    if spec is None:
        return 'Unknown setting.'
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 'Enter a whole number.'
    if not spec['min'] <= value <= spec['max']:
        return f"Choose a value from {spec['min']} to {spec['max']}."
    conn = get_db_connection()
    conn.execute(
        """INSERT INTO app_settings (setting_key, setting_value, updated_at, updated_by) VALUES (?, ?, ?, ?)
           ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value,
               updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
        (key, str(value), now_str(), username),
    )
    conn.commit()
    conn.close()
    return None
