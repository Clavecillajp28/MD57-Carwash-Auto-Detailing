
import os
import sys
from datetime import timedelta

# Ensure Python can find modules inside _Backend/
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from config import template_dir, static_dir
from database import get_db_connection, init_db
from extensions import init_extensions
from live_updates import is_announced_write, publish
from password_policy import (
    PASSWORD_MIN_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_HINT,
    PASSWORD_PATTERN,
)

app = Flask(
    __name__,
    template_folder=template_dir,
    static_folder=static_dir,
)


@app.context_processor
def password_form_policy():
    return dict(
        password_min=PASSWORD_MIN_LENGTH,
        password_max=PASSWORD_MAX_LENGTH,
        password_hint=PASSWORD_HINT,
        password_pattern=PASSWORD_PATTERN,
    )


app.config["PUBLIC_BASE_URL"] = os.environ.get(
    "PUBLIC_BASE_URL", ""
).rstrip("/")

app.config["BACKGROUND_JOBS_ENABLED"] = (
    os.environ.get("MD57_BACKGROUND_JOBS", "false").lower()
    in ("true", "1")
)

# Handle HTTPS and host headers from Vercel's reverse proxy.
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_proto=1,
    x_host=1,
)

# Session security
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set. Add it to Vercel Environment Variables."
    )

# Google OAuth configuration
app.config["GOOGLE_CLIENT_ID"] = os.environ.get("GOOGLE_CLIENT_ID")
app.config["GOOGLE_CLIENT_SECRET"] = os.environ.get(
    "GOOGLE_CLIENT_SECRET"
)

if (
    not app.config["GOOGLE_CLIENT_ID"]
    or not app.config["GOOGLE_CLIENT_SECRET"]
):
    raise RuntimeError(
        "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set. "
        "Add them to Vercel Environment Variables."
    )

# Session timeout
SESSION_LIFETIME = timedelta(
    minutes=max(
        5,
        int(os.environ.get("SESSION_TIMEOUT_MINUTES", "60")),
    )
)
app.config["PERMANENT_SESSION_LIFETIME"] = SESSION_LIFETIME

# Session cookie security
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("SESSION_COOKIE_SECURE", "true").lower()
    in ("true", "1")
)

# Email configuration
app.config['MAIL_TRANSPORT'] = os.environ.get('MAIL_TRANSPORT', 'smtp').strip().lower()
app.config['BREVO_API_KEY'] = os.environ.get('BREVO_API_KEY')
app.config["MAIL_SERVER"] = os.environ.get(
    "MAIL_SERVER", "smtp.gmail.com"
)
app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", "587"))
app.config["MAIL_USE_TLS"] = (
    os.environ.get("MAIL_USE_TLS", "true").lower()
    in ("true", "1")
)
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")
app.config["MAIL_DEFAULT_SENDER"] = os.environ.get(
    "MAIL_DEFAULT_SENDER",
    app.config["MAIL_USERNAME"],
)

if app.config['MAIL_TRANSPORT'] == 'smtp' and (
    not app.config["MAIL_USERNAME"]
    or not app.config["MAIL_PASSWORD"]
):
    print(
        "[WARNING] MAIL_USERNAME/MAIL_PASSWORD not set. "
        "Email notifications will fail."
    )

init_extensions(app)


def _wants_json():
    return request.path.startswith("/api/") or request.is_json


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    if _wants_json():
        return jsonify({
            "status": "error",
            "message": f"CSRF token validation failed: {e.description}",
        }), 400

    return render_template(
        "login.html",
        error="Security validation failed or session expired. Please log in again.",
    ), 400


@app.errorhandler(404)
def handle_not_found(e):
    if _wants_json():
        return jsonify({
            "status": "error",
            "message": "Not found.",
        }), 404

    return render_template(
        "error.html",
        error_code=404,
        error_title="Page Not Found",
        error_message="The page you're looking for doesn't exist or may have moved.",
    ), 404


@app.errorhandler(500)
def handle_server_error(e):
    if _wants_json():
        return jsonify({
            "status": "error",
            "message": "Internal server error.",
        }), 500

    return render_template(
        "error.html",
        error_code=500,
        error_title="Something Went Wrong",
        error_message=(
            "An unexpected error occurred on our end. Please try again, "
            "or contact support if it keeps happening."
        ),
    ), 500


@app.errorhandler(HTTPException)
def handle_http_error(e):
    if _wants_json():
        return jsonify({
            "status": "error",
            "message": e.description,
        }), e.code

    return e


@app.before_request
def make_session_permanent():
    session.permanent = True

    if "user" not in session:
        return

    conn = get_db_connection()
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (session.get("user_id"),),
        ).fetchone()
    finally:
        conn.close()

    if (
        not user
        or user["disabled_at"]
        or user["session_version"]
        != session.get("session_version", 0)
    ):
        session.clear()

        if request.path.startswith("/api/"):
            return jsonify(
                status="error",
                message="Your access has changed. Please sign in again.",
            ), 401

        if request.endpoint not in (
            "auth.login_page",
            "auth.accept_invitation",
            "static",
        ):
            return redirect(
                url_for(
                    "auth.login_page",
                    error="Your access has changed. Please sign in again.",
                )
            )

        return

    session.update(
        user=user["username"],
        role=user["role"],
        fullname=user["fullname"] or user["username"],
        is_owner=bool(user["is_owner"]),
        session_version=user["session_version"],
    )
    session.pop("front_desk", None)


@app.after_request
def announce_changes(response):
    if (
        response.status_code < 400
        and is_announced_write(request.method, request.path)
    ):
        publish("data")

    return response


@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = (
        "no-cache, no-store, must-revalidate, max-age=0"
    )
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# Initialize the database.
# Note: persistent production storage may require an external database on Vercel.
init_db()

# Opt-in for temporary presentations, including hosts without shell access.
# The batch is transactional and idempotent on the current database.
app.config['DEMO_HISTORY'] = None
if os.environ.get('MD57_DEMO_HISTORY', 'false').lower() in ('true', '1'):
    from demo_history import seed_demo_history
    app.config['DEMO_HISTORY'] = seed_demo_history()
    print('[DEMO] 60 simulated workdays loaded; not evidence of real-world accuracy.')


@app.context_processor
def demo_history_notice():
    return {'demo_history': app.config.get('DEMO_HISTORY')}

# Register application blueprints.
from blueprints.auth import auth_bp
from blueprints.pages import pages_bp
from blueprints.admin_api import admin_api_bp
from blueprints.queue_api import queue_api_bp
from blueprints.feedback_api import feedback_api_bp
from blueprints.staff_api import staff_api_bp
from blueprints.notifications_api import notifications_api_bp
from blueprints.analytics_api import analytics_api_bp
from blueprints.profile_api import profile_api_bp
from blueprints.mfa_api import mfa_bp
from blueprints.quality_api import quality_bp
from blueprints.operations_api import operations_bp

app.register_blueprint(auth_bp)
app.register_blueprint(pages_bp)
app.register_blueprint(admin_api_bp)
app.register_blueprint(queue_api_bp)
app.register_blueprint(feedback_api_bp)
app.register_blueprint(staff_api_bp)
app.register_blueprint(notifications_api_bp)
app.register_blueprint(analytics_api_bp)
app.register_blueprint(profile_api_bp)
app.register_blueprint(mfa_bp)
app.register_blueprint(quality_bp)
app.register_blueprint(operations_bp)

# Background jobs should remain disabled on Vercel serverless.
from background_jobs import start_background_jobs

if app.config["BACKGROUND_JOBS_ENABLED"]:
    start_background_jobs(app)


# Local development only. Vercel imports the app object above.
if __name__ == "__main__":
    from runtime import main
    main()
