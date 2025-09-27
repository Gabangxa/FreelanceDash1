import os
import logging
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, g, session
from flask_sqlalchemy import SQLAlchemy
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

class Base(DeclarativeBase):
    pass

# Initialize extensions
db = SQLAlchemy(model_class=Base)
login_manager = LoginManager()

# Create Flask app
app = Flask(__name__)

# Fix for proper IP handling behind proxies - sanitize forwarded headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)

# Configuration
if os.environ.get("FLASK_ENV") == "development":
    app.config["DEBUG"] = True
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev_key_only_for_development")
    logging.getLogger().setLevel(logging.DEBUG)
else:
    # Production settings
    app.config["DEBUG"] = False
    # Ensure we have a strong secret key in production
    app.secret_key = os.environ.get("FLASK_SECRET_KEY")
    if not app.secret_key:
        logger.warning("No secret key set, generating a temporary one. This is not secure for production!")
        app.secret_key = secrets.token_hex(32)

# Enhanced security settings for cookies and sessions
app.config["SESSION_COOKIE_HTTPONLY"] = True  # Prevent JavaScript access to session cookie
app.config["SESSION_COOKIE_SECURE"] = not app.debug  # Force HTTPS in production
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # Prevent CSRF attacks
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=1)  # Session expires after 1 day
app.config["SESSION_REFRESH_EACH_REQUEST"] = True  # Update session on each request

# Remember me cookie security
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SECURE"] = not app.debug
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)  # Remember for 30 days

# Database configuration
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
    "pool_size": 10,  # Optimized connection pool size
    "max_overflow": 20,  # Allow more connections during high load
}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False  # Disable to improve performance

# Initialize extensions with app
db.init_app(app)
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
    
    if app.debug:
        logger.debug('Request Headers: %s', request.headers)
        logger.debug('Request Body: %s', request.get_data())

# Template context processors
@app.context_processor
def inject_common_variables():
    """Inject common variables into all templates."""
    return {
        'current_year': datetime.now().year
    }

@app.after_request
def add_security_headers_and_log_timing(response):
    # Add comprehensive security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'  # Prevents MIME type sniffing
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'  # Prevents clickjacking
    response.headers['X-XSS-Protection'] = '1; mode=block'  # Browser XSS filtering
    
    # Add Content Security Policy
    csp_directives = [
        "default-src 'self'",  # Default policy for fetching content
        "script-src 'self' https://cdn.jsdelivr.net https://cdnjs.buymeacoffee.com https://cdnjs.cloudflare.com 'unsafe-inline'",
        "style-src 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com 'unsafe-inline'",
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
from api import api_bp  # Import the API blueprint
from webhooks.routes import bp as webhooks_bp  # Import webhook blueprint
# import polar  # Temporarily disabled Polar.sh integration

# Register web interface blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(projects_bp)
app.register_blueprint(invoices_bp)
app.register_blueprint(clients_bp)
app.register_blueprint(faq_bp)
app.register_blueprint(settings_bp)

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

# For testing/debugging purposes only
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)