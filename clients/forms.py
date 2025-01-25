from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SubmitField
from wtforms.validators import DataRequired, Email

class ClientForm(FlaskForm):
    name = StringField('Client Name', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    company = StringField('Company Name')
    address = TextAreaField('Address')
    submit = SubmitField('Save Client')
