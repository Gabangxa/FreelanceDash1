"""
API module for SoloDolo.

This module provides RESTful API endpoints for programmatic access to the application.
"""
from flask import Blueprint, jsonify, request, current_app, g
from flask_login import current_user, login_required
from functools import wraps
import time
import logging

logger = logging.getLogger('api')

# Create Blueprint
api_bp = Blueprint('api', __name__, url_prefix='/api/v1')


def require_api_access(f):
    """Gate an API endpoint behind the ``api_access`` paid feature flag.

    Returns 401 if the caller is not authenticated and 403 if authenticated
    but on a tier without ``api_access`` (the default for free users). Apply
    *below* ``@login_required`` so the auth check fires first and unauthenticated
    requests get a clean 401 instead of a misleading 403.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({
                'status': 'error',
                'message': 'Authentication required',
            }), 401
        if not current_user.has_feature('api_access'):
            return jsonify({
                'status': 'error',
                'message': (
                    'API access requires a paid plan. Upgrade your subscription '
                    'to enable programmatic access to your data.'
                ),
            }), 403
        return f(*args, **kwargs)
    return wrapper

# Import routes after blueprint creation to avoid circular imports
from api.routes import *  # noqa

@api_bp.before_request
def log_request_info():
    """Log API request information and start timing for performance tracking."""
    g.api_request_start_time = time.time()
    
    # Enhanced request logging for API endpoints
    if current_app.debug:
        logger.debug(f"API Request: {request.method} {request.path}")
        logger.debug(f"Headers: {dict(request.headers)}")
        if request.is_json:
            logger.debug(f"Request Body: {request.get_json()}")

@api_bp.after_request
def add_api_headers(response):
    """Add headers for API responses, including CORS and caching controls."""
    # Set API-specific headers
    response.headers['Content-Type'] = 'application/json'
    
    # Add CORS headers - restrict to same origin in production
    if current_app.debug:
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    else:
        response.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '')
    
    # Add cache control - APIs generally shouldn't be cached
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    
    # Log API response times in development for performance optimization
    if hasattr(g, 'api_request_start_time'):
        duration = time.time() - g.api_request_start_time
        if duration > 0.5:  # Log slow API requests
            user_id = current_user.id if current_user.is_authenticated else None
            logger.info(f"API Response Time: {duration:.4f}s for {request.method} {request.path} - User ID: {user_id}")
    
    return response

@api_bp.errorhandler(404)
def api_not_found(e):
    """Handle 404 errors in the API with a proper JSON response."""
    return jsonify({
        'status': 'error',
        'message': 'API endpoint not found',
        'error': 'not_found',
        'code': 404
    }), 404

@api_bp.errorhandler(405)
def api_method_not_allowed(e):
    """Handle 405 errors in the API with a proper JSON response."""
    return jsonify({
        'status': 'error',
        'message': 'Method not allowed for this endpoint',
        'error': 'method_not_allowed',
        'code': 405
    }), 405

@api_bp.errorhandler(500)
def api_server_error(e):
    """Handle 500 errors in the API with a proper JSON response."""
    logger.exception("API Server Error")
    return jsonify({
        'status': 'error',
        'message': 'An unexpected error occurred',
        'error': 'server_error',
        'code': 500
    }), 500