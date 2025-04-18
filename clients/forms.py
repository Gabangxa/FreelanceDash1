from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SubmitField, FieldList, FormField, DateTimeField, Form, BooleanField
from wtforms.validators import DataRequired, Email, Length, Optional
from datetime import datetime

class ProjectFieldForm(Form):
    """Form for project fields in client creation"""
    name = StringField('Project Name', validators=[
        DataRequired(message="Project name is required"),
        Length(min=2, max=100, message="Project name must be between 2 and 100 characters")
    ])
    description = TextAreaField('Description', validators=[Optional()])
    start_date = DateTimeField('Start Date', validators=[DataRequired()], format='%Y-%m-%d', default=datetime.now)
    end_date = DateTimeField('End Date', format='%Y-%m-%d', validators=[Optional()])
    include_project = BooleanField('Include this project', default=True)

class ClientForm(FlaskForm):
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
    # Initial project that will be displayed by default
    projects = FieldList(FormField(ProjectFieldForm), min_entries=1)
    submit = SubmitField('Save Client')