import os
import logging
from flask import Flask, render_template, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix
import secrets

# Configure logging with more details for production
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

# Fix for proper IP handling behind proxies
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

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
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["REMEMBER_COOKIE_HTTPONLY"] = True
    app.config["REMEMBER_COOKIE_SECURE"] = True

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

# Request handlers for logging
@app.before_request
def log_request_info():
    if app.debug:
        logger.debug('Request Headers: %s', request.headers)
        logger.debug('Request Body: %s', request.get_data())

@app.after_request
def add_security_headers(response):
    # Add security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

# Error handlers
@app.errorhandler(404)
def page_not_found(e):
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    logger.error('Server Error: %s', str(e))
    return render_template('errors/500.html'), 500

# Add root route for landing page
@app.route('/')
def index():
    return render_template('index.html')

# Register blueprints
from auth.routes import auth_bp
from projects.routes import projects_bp
from invoices.routes import invoices_bp
from clients.routes import clients_bp

app.register_blueprint(auth_bp)
app.register_blueprint(projects_bp)
app.register_blueprint(invoices_bp)
app.register_blueprint(clients_bp)

# Create database tables
with app.app_context():
    import models
    db.create_all()

# For testing/debugging purposes only
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)