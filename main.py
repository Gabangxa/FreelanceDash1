import logging
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

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/faq')
def faq():
    return render_template('faq.html')

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)