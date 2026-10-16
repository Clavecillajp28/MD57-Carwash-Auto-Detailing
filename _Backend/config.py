import os


def load_dotenv_custom(env_path):
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key not in os.environ:
                        os.environ[key] = value


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv_custom(os.path.join(BASE_DIR, '.env'))

template_dir = os.path.join(BASE_DIR, '_Frontend', 'templates')
static_dir = os.path.join(BASE_DIR, '_Frontend', 'static')
LOGO_PATH = os.path.join(static_dir, 'images', 'md57logo.jpg')

# Overridable so the test suite (tests/conftest.py) runs against throwaway
# files instead of the real dev database and audit log.
DB_FILE = os.environ.get('MD57_DB_FILE') or os.path.join(BASE_DIR, 'md57_carwash.db')
ACTIVITY_LOG_FILE = os.environ.get('MD57_ACTIVITY_LOG') or os.path.join(BASE_DIR, 'system_activity.log')
