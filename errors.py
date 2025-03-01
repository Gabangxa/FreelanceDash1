"""
Centralized error handling and logging module for the Freelancer Suite application.
"""
import os
import logging
from logging.handlers import RotatingFileHandler
import traceback
from functools import wraps
from flask import render_template, jsonify, request, current_app
from werkzeug.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

# Setup logging
def setup_logging(app):
    """Configure application logging with console and file handlers."""
    
    # Ensure logs directory exists
    logs_dir = os.path.join(app.root_path, 'logs')
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)
    
    # Configure the root logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO if not app.debug else logging.DEBUG)
    
    # Create formatters
    verbose_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] - %(name)s - %(message)s [in %(pathname)s:%(lineno)d]'
    )
    simple_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] - %(message)s'
    )
    
    # Console handler - for development
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(simple_formatter)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)
    
    # File handler - info level and above
    info_file_handler = RotatingFileHandler(
        os.path.join(logs_dir, 'info.log'), 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    info_file_handler.setLevel(logging.INFO)
    info_file_handler.setFormatter(verbose_formatter)
    logger.addHandler(info_file_handler)
    
    # File handler - error level and above
    error_file_handler = RotatingFileHandler(
        os.path.join(logs_dir, 'error.log'), 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    error_file_handler.setLevel(logging.ERROR)
    error_file_handler.setFormatter(verbose_formatter)
    logger.addHandler(error_file_handler)
    
    # Set SQLAlchemy logging level
    logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
    
    # Log startup
    app.logger.info(f"Application logging configured. Mode: {'Debug' if app.debug else 'Production'}")
    
    return logger

# Register error handlers
def register_error_handlers(app):
    """Register custom error handlers with the Flask application."""
    
    @app.errorhandler(404)
    def not_found_error(error):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Resource not found"}), 404
        return render_template('errors/404.html'), 404

    @app.errorhandler(403)
    def forbidden_error(error):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Access forbidden"}), 403
        return render_template('errors/403.html'), 403
    
    @app.errorhandler(500)
    def internal_error(error):
        app.logger.error(f"Internal server error: {str(error)}\n{traceback.format_exc()}")
        if request.path.startswith('/api/'):
            return jsonify({"error": "Internal server error"}), 500
        return render_template('errors/500.html'), 500
    
    @app.errorhandler(400)
    def bad_request_error(error):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Bad request"}), 400
        return render_template('errors/400.html'), 400
    
    @app.errorhandler(Exception)
    def handle_exception(error):
        # Pass through HTTP exceptions
        if isinstance(error, HTTPException):
            return error
        
        # Log the error
        app.logger.error(f"Unhandled exception: {str(error)}\n{traceback.format_exc()}")
        
        # Return a JSON response for API requests
        if request.path.startswith('/api/'):
            return jsonify({"error": "Internal server error"}), 500
        
        # Return an error page for web requests
        return render_template('errors/500.html'), 500

# Decorator for database error handling
def handle_db_errors(f):
    """Decorator for handling database errors in route functions."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except IntegrityError as e:
            current_app.logger.error(f"IntegrityError in {f.__name__}: {str(e)}")
            if "duplicate key" in str(e):
                error_message = "This record already exists."
            elif "foreign key constraint" in str(e):
                error_message = "This operation would violate data integrity."
            else:
                error_message = "A database constraint was violated."
            
            from app import db
            db.session.rollback()
            return render_template('errors/db_error.html', error=error_message), 400
        except SQLAlchemyError as e:
            current_app.logger.error(f"SQLAlchemyError in {f.__name__}: {str(e)}")
            from app import db
            db.session.rollback()
            return render_template('errors/db_error.html', error="A database error occurred."), 500
    return decorated_function

# User-friendly error messages
class UserFriendlyError(Exception):
    """Custom exception for user-friendly error messages."""
    def __init__(self, message, category="danger", status_code=400):
        self.message = message
        self.category = category
        self.status_code = status_code
        super().__init__(self.message)

def register_user_friendly_error_handler(app):
    """Register custom handler for UserFriendlyError."""
    @app.errorhandler(UserFriendlyError)
    def handle_user_friendly_error(error):
        from flask import flash, redirect, url_for
        
        flash(error.message, error.category)
        
        # Default redirect to dashboard or home
        if hasattr(request, 'referrer') and request.referrer:
            return redirect(request.referrer)
        return redirect(url_for('projects.dashboard'))
