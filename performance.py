"""
Performance monitoring and logging module for the SoloDolo application.
"""
import time
import logging
from functools import wraps
from flask import request, g
import threading
import traceback

# Initialize performance logger
perf_logger = logging.getLogger('performance')

class PerformanceMonitor:
    """Class for tracking and logging slow operations in the application."""
    
    def __init__(self, app=None, slow_request_threshold=1.0, slow_db_threshold=0.5):
        """
        Initialize performance monitor.
        
        Args:
            app: Flask application instance. If None, call init_app later.
            slow_request_threshold: Time in seconds to consider a request as slow
            slow_db_threshold: Time in seconds to consider a database operation as slow
        """
        self.slow_request_threshold = slow_request_threshold
        self.slow_db_threshold = slow_db_threshold
        
        if app is not None:
            self.init_app(app)
    
    def init_app(self, app):
        """
        Configure the performance monitor with a Flask application instance.
        
        Args:
            app: Flask application instance
        """
        # Register before_request and after_request handlers
        app.before_request(self._before_request)
        app.after_request(self._after_request)
        
        # Store reference to app
        self.app = app
        
        # Log initialization
        perf_logger.info(f"Performance monitoring initialized. " 
                        f"Thresholds: Request={self.slow_request_threshold}s, DB={self.slow_db_threshold}s")
    
    def _before_request(self):
        """Record the start time of each request."""
        g.start_time = time.time()
        g.db_time = 0  # Initialize database time accumulator
    
    def _after_request(self, response):
        """Log slow requests after the response is prepared."""
        # Skip for static files which shouldn't need monitoring
        if request.path.startswith('/static/'):
            return response
        
        # Calculate request duration
        if hasattr(g, 'start_time'):
            duration = time.time() - g.start_time
            
            # Log requests that exceed the threshold
            if duration > self.slow_request_threshold:
                request_data = {
                    'method': request.method,
                    'path': request.path,
                    'endpoint': request.endpoint,
                    'status_code': response.status_code,
                    'duration': f"{duration:.4f}s",
                    'db_time': f"{getattr(g, 'db_time', 0):.4f}s",
                    'db_percent': f"{(getattr(g, 'db_time', 0) / duration * 100) if duration > 0 else 0:.1f}%",
                    'ip': request.remote_addr,
                    'user_agent': request.user_agent.string
                }
                
                try:
                    # Try to get user info
                    from flask_login import current_user
                    if current_user.is_authenticated:
                        request_data['user_id'] = current_user.id
                except (ImportError, RuntimeError, AttributeError):
                    pass
                
                perf_logger.warning(f"Slow request detected: {duration:.4f}s - {request.method} {request.path}")
                perf_logger.warning(f"Details: {request_data}")
        
        return response

def track_db_query(f):
    """
    Decorator for tracking database query execution time.
    
    Use this to wrap SQLAlchemy query execution methods.
    """
    @wraps(f)
    def wrapped(*args, **kwargs):
        # Skip if not in request context
        if not hasattr(g, 'db_time'):
            return f(*args, **kwargs)
        
        start_time = time.time()
        try:
            return f(*args, **kwargs)
        finally:
            duration = time.time() - start_time
            # Add to total DB time for the current request
            g.db_time = getattr(g, 'db_time', 0) + duration
            
            # Log slow database operations
            from flask import current_app
            threshold = getattr(current_app, 'slow_db_threshold', 0.5)
            if duration > threshold:
                perf_logger.warning(f"Slow database operation detected: {duration:.4f}s - {f.__name__}")
                # Get abbreviated stack trace (skip decorator frames)
                stack = ''.join(traceback.format_stack(limit=7)[:-2])
                perf_logger.warning(f"Query location:\n{stack}")
    return wrapped