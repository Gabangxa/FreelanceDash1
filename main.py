import logging
import os
from app import app
from errors import setup_logging

# Configure logging for WSGI server
if not app.debug:
    logger = setup_logging(app)
    logger.info("Production server started")
else:
    logging.basicConfig(level=logging.DEBUG)
    logger = logging.getLogger(__name__)
    logger.info("Development server started")

if __name__ == "__main__":
    # Only enable the Werkzeug debugger when explicitly in development.
    # Leaving debug=True on unconditionally would expose the interactive
    # debugger (an RCE vector) if this entrypoint is ever run in production.
    # In production the app is served by gunicorn (see railway.toml), which
    # imports ``main:app`` and never executes this block.
    debug = os.environ.get("FLASK_ENV", "").lower() == "development"
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=debug)