from flask_mail import Mail
from flask_wtf.csrf import CSRFProtect
from authlib.integrations.flask_client import OAuth

from ai_engine import StaffMCDAEngine, WaitTimePredictorAI

csrf = CSRFProtect()
mail = Mail()
oauth = OAuth()

# Pure-Python singletons, no app binding needed.
ai_predictor = WaitTimePredictorAI()
mcda_engine = StaffMCDAEngine()


def init_extensions(app):
    """Binds the unbound extension singletons above to the Flask app.
    Call once, during app creation, after app.config is fully populated."""
    csrf.init_app(app)
    mail.init_app(app)
    oauth.init_app(app)
    oauth.register(
        name='google',
        client_id=app.config['GOOGLE_CLIENT_ID'],
        client_secret=app.config['GOOGLE_CLIENT_SECRET'],
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'}
    )
