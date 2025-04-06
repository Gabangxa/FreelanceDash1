"""
Polar.sh integration module for Freelancer Suite.
Provides subscription management and payment processing functionality.
"""

def init_app(app):
    """Initialize Polar.sh integration with the Flask app."""
    # Register routes
    from . import routes
    app.register_blueprint(routes.bp)
    
    # Initialize models
    from . import models
    
    app.logger.info("Polar.sh integration initialized")