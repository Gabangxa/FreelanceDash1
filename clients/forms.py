from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SubmitField
from wtforms.validators import DataRequired, Email, Length, Optional

class ClientForm(FlaskForm):
    """Form for client creation and editing."""
    name = StringField('Client Name', validators=[
        DataRequired(message="Client name is required"),
        Length(min=2, max=100, message="Client name must be between 2 and 100 characters")
    ])
    email = StringField('Email', validators=[
        Optional(),
        Email(message="Please enter a valid email address"),
        Length(max=120, message="Email must be less than 120 characters")
    ])
    company = StringField('Company Name', validators=[
        Optional(),
        Length(max=100, message="Company name must be less than 100 characters")
    ])
    address = TextAreaField('Address', validators=[
        Optional(),
        Length(max=500, message="Address must be less than 500 characters")
    ])
    submit = SubmitField('Save Client')