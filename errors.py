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
    
    # Clear existing handlers to avoid duplicate logs
    if logger.handlers:
        logger.handlers.clear()
    
    logger.setLevel(logging.INFO if not app.debug else logging.DEBUG)
    
    # Create formatters
    verbose_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(name)s] %(message)s [in %(pathname)s:%(lineno)d]'
    )
    simple_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s'
    )
    
    # Console handler - for all environments
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(simple_formatter)
    console_handler.setLevel(logging.DEBUG if app.debug else logging.INFO)
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
    
    # Security log file for authentication and critical operations
    security_file_handler = RotatingFileHandler(
        os.path.join(logs_dir, 'security.log'), 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=10  # Keep more security logs
    )
    security_file_handler.setLevel(logging.INFO)
    security_file_handler.setFormatter(verbose_formatter)
    
    # Create and configure security logger
    security_logger = logging.getLogger('security')
    security_logger.setLevel(logging.INFO)
    security_logger.addHandler(security_file_handler)
    
    # Set package-specific log levels
    logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    
    # Specific loggers for application components
    setup_component_loggers(logs_dir, verbose_formatter)
    
    # Log startup
    app.logger.info(f"Application logging configured. Mode: {'Debug' if app.debug else 'Production'}")
    
    return logger

def setup_component_loggers(logs_dir, formatter):
    """Set up loggers for specific application components."""
    # Application modules
    components = ['auth', 'projects', 'invoices', 'clients']
    
    for component in components:
        component_logger = logging.getLogger(component)
        component_logger.propagate = True  # Propagate to root logger
        
        # Component-specific file handler
        handler = RotatingFileHandler(
            os.path.join(logs_dir, f'{component}.log'),
            maxBytes=5*1024*1024,  # 5MB
            backupCount=3
        )
        handler.setFormatter(formatter)
        handler.setLevel(logging.INFO)
        component_logger.addHandler(handler)
    
    # Database logger - for SQL and ORM operations
    db_logger = logging.getLogger('database')
    db_logger.propagate = True
    
    db_handler = RotatingFileHandler(
        os.path.join(logs_dir, 'database.log'),
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    db_handler.setFormatter(formatter)
    db_handler.setLevel(logging.INFO)
    db_logger.addHandler(db_handler)
    
    # Performance logger - for tracking slow operations
    perf_logger = logging.getLogger('performance')
    perf_logger.propagate = True
    
    perf_handler = RotatingFileHandler(
        os.path.join(logs_dir, 'performance.log'),
        maxBytes=5*1024*1024,  # 5MB
        backupCount=3
    )
    perf_handler.setFormatter(formatter)
    perf_handler.setLevel(logging.INFO)
    perf_logger.addHandler(perf_handler)

# Register error handlers
def register_error_handlers(app):
    """Register custom error handlers with the Flask application."""
    
    @app.errorhandler(404)
    def not_found_error(error):
        # Log 404 errors with request details
        app.logger.info(f"404 Not Found: {request.method} {request.path} | Referrer: {request.referrer or 'None'} | IP: {request.remote_addr}")
        
        if request.path.startswith('/api/'):
            return jsonify({"error": "Resource not found"}), 404
        return render_template('errors/404.html', requested_url=request.path), 404

    @app.errorhandler(403)
    def forbidden_error(error):
        # Log access attempts that are forbidden. ``current_user`` lives on
        # flask_login, NOT on flask.current_app -- the previous code always
        # logged "Unknown" because the attribute lookup silently failed.
        user_id = 'Unknown'
        try:
            from flask_login import current_user
            if current_user.is_authenticated:
                user_id = current_user.id
        except Exception:
            pass
        app.logger.warning(f"403 Forbidden access: {request.method} {request.path} | User: {user_id} | IP: {request.remote_addr}")
        
        if request.path.startswith('/api/'):
            return jsonify({"error": "Access forbidden"}), 403
        return render_template('errors/403.html'), 403
    
    @app.errorhandler(500)
    def internal_error(error):
        # Add request context to error logs
        request_info = {
            'path': request.path,
            'method': request.method,
            'user_agent': request.user_agent.string,
            'ip': request.remote_addr
        }
        app.logger.error(
            f"500 Internal Server Error: {str(error)}\n"
            f"Request: {request_info}\n"
            f"{traceback.format_exc()}"
        )
        
        if request.path.startswith('/api/'):
            return jsonify({"error": "Internal server error"}), 500
        return render_template('errors/500.html'), 500
    
    @app.errorhandler(400)
    def bad_request_error(error):
        # Log bad requests with request data
        app.logger.warning(
            f"400 Bad Request: {request.method} {request.path} | "
            f"Form Data: {request.form or 'None'} | IP: {request.remote_addr}"
        )
        
        if request.path.startswith('/api/'):
            return jsonify({"error": "Bad request"}), 400
        return render_template('errors/400.html'), 400
    
    @app.errorhandler(Exception)
    def handle_exception(error):
        # Pass through HTTP exceptions
        if isinstance(error, HTTPException):
            return error
        
        # Enhanced unhandled exception logging
        user_id = 'Unknown'
        try:
            from flask_login import current_user
            if current_user.is_authenticated:
                user_id = current_user.id
        except Exception:
            pass
        
        app.logger.error(
            f"Unhandled exception: {error.__class__.__name__}: {str(error)}\n"
            f"Route: {request.method} {request.path} | User: {user_id} | IP: {request.remote_addr}\n"
            f"{traceback.format_exc()}"
        )
        
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
            # Get detailed error information
            error_details = {
                'function': f.__name__,
                'module': f.__module__,
                'error_type': 'IntegrityError',
                'error_message': str(e)
            }
            
            # Get user info if available
            user_id = 'Unknown'
            try:
                from flask_login import current_user
                if current_user.is_authenticated:
                    user_id = current_user.id
                    error_details['user_id'] = user_id
            except Exception:
                pass
            
            # Get request details if available
            try:
                from flask import request
                error_details['path'] = request.path
                error_details['method'] = request.method
                error_details['remote_addr'] = request.remote_addr
            except Exception:
                pass
            
            # Log with all details
            db_logger = logging.getLogger('database')
            db_logger.error(
                f"IntegrityError in {f.__name__}: {str(e)}\n"
                f"Details: {error_details}\n"
                f"{traceback.format_exc()}"
            )
            
            # Determine user-friendly error message
            if "duplicate key" in str(e):
                error_message = "This record already exists."
            elif "foreign key constraint" in str(e):
                error_message = "This operation would violate data integrity."
            else:
                error_message = "A database constraint was violated."
            
            # Rollback transaction
            from app import db
            db.session.rollback()
            
            # Return appropriate error response
            if request.path.startswith('/api/'):
                return jsonify({"error": error_message}), 400
            return render_template('errors/db_error.html', error=error_message), 400
            
        except SQLAlchemyError as e:
            # Get detailed error information
            error_details = {
                'function': f.__name__,
                'module': f.__module__,
                'error_type': 'SQLAlchemyError',
                'error_message': str(e)
            }
            
            # Get user info if available
            user_id = 'Unknown'
            try:
                from flask_login import current_user
                if current_user.is_authenticated:
                    user_id = current_user.id
                    error_details['user_id'] = user_id
            except Exception:
                pass
            
            # Get request details if available
            try:
                from flask import request
                error_details['path'] = request.path
                error_details['method'] = request.method
                error_details['remote_addr'] = request.remote_addr
            except Exception:
                pass
            
            # Log with all details
            db_logger = logging.getLogger('database')
            db_logger.error(
                f"SQLAlchemyError in {f.__name__}: {str(e)}\n"
                f"Details: {error_details}\n"
                f"{traceback.format_exc()}"
            )
            
            # Rollback transaction
            from app import db
            db.session.rollback()
            
            # Return appropriate error response
            if request.path.startswith('/api/'):
                return jsonify({"error": "A database error occurred."}), 500
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
