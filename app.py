import os
import logging
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, g, session
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager, current_user
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix
import secrets
from errors import setup_logging, register_error_handlers, register_user_friendly_error_handler
from performance import PerformanceMonitor
import mail
import asset_bundler
from pathlib import Path
from dotenv import load_dotenv
import jinja2

# Load environment variables from .env file if it exists
env_path = Path('.') / '.env'
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

# Initialize basic logging for startup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def _is_production():
    """Return True iff the app is running in a production environment.

    The app considers itself in production when EITHER ``PRODUCTION=true`` is
    set, OR ``FLASK_ENV`` is anything other than ``development``/``test``.
    Tests should set ``FLASK_ENV=test``; local development should set
    ``FLASK_ENV=development``.
    """
    if os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes"):
        return True
    flask_env = os.environ.get("FLASK_ENV", "").lower()
    if flask_env in ("development", "test"):
        return False
    # Default: treat unknown/empty FLASK_ENV as production. This is the safe
    # default for a deployed app.
    return True


IS_PRODUCTION = _is_production()


class Base(DeclarativeBase):
    pass

# Initialize extensions
db = SQLAlchemy(model_class=Base)
migrate = Migrate()
login_manager = LoginManager()

# Create Flask app
app = Flask(__name__)

# Fix for proper IP handling behind proxies - sanitize forwarded headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)

# Configuration
if not IS_PRODUCTION:
    app.config["DEBUG"] = os.environ.get("FLASK_ENV", "").lower() == "development"
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev_key_only_for_development")
    if app.config["DEBUG"]:
        logging.getLogger().setLevel(logging.DEBUG)
else:
    # Production settings
    app.config["DEBUG"] = False
    # Ensure we have a strong secret key in production. Refuse to start
    # rather than fall back to a generated-per-process key, because that
    # would silently invalidate every session and CSRF token on each
    # gunicorn worker reload (and would differ between workers).
    app.secret_key = os.environ.get("FLASK_SECRET_KEY")
    if not app.secret_key:
        raise RuntimeError(
            "FLASK_SECRET_KEY is required in production. Refusing to start "
            "with a generated key (would invalidate sessions/CSRF on every "
            "restart and differ across gunicorn workers). "
            "Set FLASK_SECRET_KEY to a strong random value."
        )

# Enhanced security settings for cookies and sessions. Cookie flags are tied
# to IS_PRODUCTION (not app.debug) so that a one-off debug session in prod
# can't quietly drop the Secure flag.
app.config["SESSION_COOKIE_HTTPONLY"] = True  # Prevent JavaScript access to session cookie
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION  # HTTPS-only in production
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # Prevent CSRF attacks
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=1)  # Session expires after 1 day
app.config["SESSION_REFRESH_EACH_REQUEST"] = True  # Update session on each request

# Remember me cookie security
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SECURE"] = IS_PRODUCTION
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)  # Remember for 30 days

# Database configuration
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")

# Pool tuning is only valid for client/server engines like Postgres/MySQL.
# SQLite (used by the test suite) uses a StaticPool that rejects pool_size /
# max_overflow, so apply those settings only when the URL clearly is not
# SQLite. pool_recycle / pool_pre_ping are harmless across both.
_db_uri = app.config.get("SQLALCHEMY_DATABASE_URI") or ""
_is_sqlite = _db_uri.startswith("sqlite")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}
if not _is_sqlite:
    app.config["SQLALCHEMY_ENGINE_OPTIONS"].update({
        "pool_size": 10,
        "max_overflow": 20,
    })
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False  # Disable to improve performance

# Initialize extensions with app
db.init_app(app)
migrate.init_app(app, db)
login_manager.init_app(app)
login_manager.login_view = 'auth.login'
login_manager.login_message_category = 'info'  # Bootstrap message styling

# Setup advanced logging
logger = setup_logging(app)

# Initialize mail service
mail.init_app(app)

# Initialize asset bundling for CSS minification
asset_bundler.init_app(app)

# Custom Jinja2 template filters
def slice_filter(iterable, start, end=None):
    """Slice an iterable like Python's list slicing."""
    if end is None:
        return list(iterable)[start:]
    return list(iterable)[start:end]

app.jinja_env.filters['slice'] = slice_filter

# Initialize performance monitoring
# Set higher thresholds for production to reduce noise
slow_request_threshold = 2.0 if not app.debug else 1.0
slow_db_threshold = 1.0 if not app.debug else 0.5

# Create and initialize the performance monitor
performance_monitor = PerformanceMonitor(
    app=app,
    slow_request_threshold=slow_request_threshold,
    slow_db_threshold=slow_db_threshold
)

# Store thresholds on app for access by other components
app.slow_db_threshold = slow_db_threshold

# Request handlers for logging
@app.before_request
def log_request_info():
    # Add timestamp for application-level tracking
    g.request_start_time = time.time()

    # Generate a per-request CSP nonce used by inline <script> tags so we can
    # drop 'unsafe-inline' from script-src. Stashed on flask.g and exposed to
    # templates via the context processor below.
    g.csp_nonce = secrets.token_urlsafe(16)

    if app.debug:
        logger.debug('Request Headers: %s', request.headers)
        logger.debug('Request Body: %s', request.get_data())

# Template context processors
@app.context_processor
def inject_common_variables():
    """Inject common variables into all templates."""
    return {
        'current_year': datetime.now().year,
        # Per-request CSP nonce. Templates emit `nonce="{{ csp_nonce }}"` on
        # inline <script> tags so they execute under the strict script-src.
        'csp_nonce': getattr(g, 'csp_nonce', '')
    }


# Register duration helpers as Jinja filters so templates can write
# `{{ entry.duration|format_duration }}` instead of duplicating
# `{% set hours = entry.duration // 60 %}` math (which is easy to typo).
from utils.duration import format_duration as _format_duration  # noqa: E402
from utils.duration import minutes_to_hours as _minutes_to_hours  # noqa: E402

app.add_template_filter(_format_duration, name='format_duration')
app.add_template_filter(_minutes_to_hours, name='minutes_to_hours')

# Whitelist for embedding user-controlled color values into nonced <style>
# blocks. Without this, an attacker who could write a malformed value into
# UserSettings could inject arbitrary CSS into the rendered page. Accepts
# only strict #RGB / #RRGGBB hex strings; anything else falls back to the
# provided default so the preview still renders.
import re as _re  # noqa: E402

_HEX_COLOR_RE = _re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')


def _safe_color(value, default='#3498db'):
    if value and _HEX_COLOR_RE.match(value.strip()):
        return value.strip()
    return default


app.add_template_filter(_safe_color, name='safe_color')

@app.after_request
def add_security_headers_and_log_timing(response):
    # Add comprehensive security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'  # Prevents MIME type sniffing
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'  # Prevents clickjacking
    response.headers['X-XSS-Protection'] = '1; mode=block'  # Browser XSS filtering
    
    # Add Content Security Policy.
    #
    # Rollout note: both script-src and style-src enforce a per-request nonce
    # (generated in the before_request hook above) and no longer allow
    # 'unsafe-inline'. All inline <script> and <style> tags in templates must
    # carry nonce="{{ csp_nonce }}", and inline style="" attributes have been
    # migrated to CSS classes. Any new template work must follow the same
    # convention.
    nonce = getattr(g, 'csp_nonce', '')
    csp_directives = [
        "default-src 'self'",  # Default policy for fetching content
        f"script-src 'self' https://cdn.jsdelivr.net https://cdnjs.buymeacoffee.com https://cdnjs.cloudflare.com 'nonce-{nonce}'",
        f"style-src 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com 'nonce-{nonce}'",
        "img-src 'self' data: https://cdnjs.buymeacoffee.com",
        "font-src 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com",
        "connect-src 'self'",
        "frame-src 'self' https://cdnjs.buymeacoffee.com"
    ]
    response.headers['Content-Security-Policy'] = "; ".join(csp_directives)
    
    # Add Referrer Policy
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    
    # Add Feature Policy (now called Permissions Policy) to limit features
    feature_policies = [
        "geolocation 'none'",
        "microphone 'none'",
        "camera 'none'",
        "payment 'none'",
        "usb 'none'"
    ]
    response.headers['Permissions-Policy'] = ", ".join(feature_policies)
    
    # Session security - ensure sessions are marked as permanent to apply lifetime
    if current_user.is_authenticated and request.endpoint != 'static':
        session.permanent = True
        # Refresh CSRF token if it exists (this helps prevent CSRF token fixation)
        if '_csrf_token' in session:
            session.modified = True
    
    # Add server timing header for non-static resources in debug mode
    if hasattr(g, 'request_start_time') and app.debug and not request.path.startswith('/static/'):
        duration = time.time() - g.request_start_time
        response.headers['Server-Timing'] = f'total;dur={duration * 1000:.2f}'
        
        # Log response times in development for easier debugging
        if app.debug and duration > 0.5:  # Only log slow requests in debug mode
            logger.debug(f"Response time: {duration:.4f}s for {request.method} {request.path}")
            
    return response

# Register error handlers
register_error_handlers(app)
register_user_friendly_error_handler(app)

# Add root route for landing page
@app.route('/')
def index():
    return render_template('index.html')

# Terms of Service and Privacy Policy routes
@app.route('/terms')
def terms():
    # Pass the current date for the "Last Updated" section
    current_date = datetime.now().strftime('%B %d, %Y')
    return render_template('terms.html', current_date=current_date)

@app.route('/privacy')
def privacy():
    # Pass the current date for the "Last Updated" section
    current_date = datetime.now().strftime('%B %d, %Y')
    return render_template('privacy.html', current_date=current_date)

# Register blueprints
from auth.routes import auth_bp
from projects.routes import projects_bp
from invoices.routes import invoices_bp
from clients.routes import clients_bp
from faq.routes import faq_bp
from settings.routes import settings_bp
from notifications.routes import notifications_bp  # Import notifications blueprint
from api import api_bp  # Import the API blueprint
from webhooks.routes import bp as webhooks_bp  # Import webhook blueprint
from admin import bp as admin_bp  # Import admin blueprint
# import polar  # Temporarily disabled Polar.sh integration

# Register web interface blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(projects_bp)
app.register_blueprint(invoices_bp)
app.register_blueprint(clients_bp)
app.register_blueprint(faq_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(notifications_bp)
app.register_blueprint(admin_bp)

# Register API blueprint
app.register_blueprint(api_bp)

# Register webhook blueprint
app.register_blueprint(webhooks_bp)

# Initialize Polar.sh integration - Temporarily disabled
# polar.init_app(app)
logger.info("Polar.sh integration is temporarily disabled")

# Create database tables
with app.app_context():
    # Import models to ensure they're registered with SQLAlchemy
    import models
    
    # Check which tables are already created
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()
    
    # Create all tables that don't exist
    db.create_all()

    # Initialise the webhook security storage backend (Redis if
    # ``REDIS_URL`` is set, otherwise the DB fallback) and prime the
    # dynamic IP allowlist cache. This logs one line for the chosen
    # backend and one line for the IP-list reachability check so
    # operators can confirm both at boot. Outbound HTTP refresh is
    # skipped during tests / when explicitly disabled, so the test suite
    # doesn't make network calls on import.
    #
    # If the operator explicitly set ``REDIS_URL`` we MUST fail fast on a
    # connection error -- silently degrading to DB (or, worse, to broken
    # behaviour) would mask a misconfiguration and let the app keep
    # serving webhooks under the wrong assumptions about counter
    # consistency. The DB fallback path is only acceptable when REDIS_URL
    # is absent, in which case we still log any unexpected error but
    # don't abort boot.
    from webhooks.storage import get_storage as _get_webhook_storage
    if os.environ.get("REDIS_URL"):
        # Let the RuntimeError raised by get_storage() propagate and
        # abort startup. gunicorn will refuse to boot the worker, which
        # is exactly what we want for a misconfigured Redis URL.
        _get_webhook_storage()
    else:
        try:
            _get_webhook_storage()
        except Exception as _exc:  # noqa: BLE001 - top-level safety net for storage init  # pragma: no cover - DB init shouldn't fail here
            logger.exception("Webhook DB-fallback storage backend init failed")

    if (
        os.environ.get("FLASK_ENV", "").lower() != "test"
        and os.environ.get("WEBHOOK_IP_REFRESH_ON_BOOT", "1").lower()
        not in ("0", "false", "no")
    ):
        try:
            from webhooks.ip_ranges import refresh_now as _refresh_ip
            _gh = _refresh_ip("github")
            _stripe = _refresh_ip("stripe")
            logger.info(
                "Webhook dynamic IP allowlist initial refresh: "
                "github=%s, stripe=%s",
                "ok" if _gh else "fallback",
                "ok" if _stripe else "fallback",
            )
        except Exception as _exc:  # noqa: BLE001 - top-level safety net for IP refresh
            logger.exception(
                "Webhook IP allowlist initial refresh raised "
                "(static fallback ranges remain in effect)"
            )

# For testing/debugging purposes only
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)