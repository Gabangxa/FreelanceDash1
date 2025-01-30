import os
import logging
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from sqlalchemy.orm import DeclarativeBase

# Configure logging
logging.basicConfig(level=logging.DEBUG)

class Base(DeclarativeBase):
    pass

# Initialize extensions
db = SQLAlchemy(model_class=Base)
login_manager = LoginManager()

# Create Flask app
app = Flask(__name__)

# Configuration
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "dev_key_only_for_development"
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}

# Initialize extensions with app
db.init_app(app)
login_manager.init_app(app)
login_manager.login_view = 'auth.login'

# Template context processor for time tracking data
@app.context_processor
def inject_time_tracking_data():
    if not hasattr(g, 'user') or not g.user:
        return {}
    
    from datetime import datetime, timedelta
    from models import TimeEntry
    
    # Get start and end of current week
    today = datetime.now()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    
    # Initialize data structure
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    hours_by_day = [0] * 7
    
    # Query time entries for current week
    entries = TimeEntry.query.filter(
        TimeEntry.start_time >= start_of_week,
        TimeEntry.start_time <= end_of_week
    ).all()
    
    # Calculate hours for each day
    for entry in entries:
        if entry.duration:  # duration is in minutes
            weekday = entry.start_time.weekday()
            hours_by_day[weekday] += entry.duration / 60  # Convert minutes to hours
    
    return {
        'time_tracking_data': {
            'labels': days,
            'hours': [round(h, 1) for h in hours_by_day]
        }
    }

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