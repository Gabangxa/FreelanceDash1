from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import StringField, TextAreaField, SelectField, SubmitField, HiddenField
from wtforms.validators import Optional, Email, Length, URL
import os

class CompanySettingsForm(FlaskForm):
    company_name = StringField('Company Name', validators=[
        Optional(),
        Length(max=100, message="Company name must be less than 100 characters")
    ])
    company_address = TextAreaField('Company Address', validators=[
        Optional(),
        Length(max=500, message="Address must be less than 500 characters")
    ])
    company_phone = StringField('Company Phone', validators=[
        Optional(),
        Length(max=20, message="Phone number must be less than 20 characters")
    ])
    company_email = StringField('Company Email', validators=[
        Optional(),
        Email(message="Please enter a valid email address"),
        Length(max=120, message="Email must be less than 120 characters")
    ])
    company_website = StringField('Company Website', validators=[
        Optional(),
        URL(message="Please enter a valid URL"),
        Length(max=120, message="Website URL must be less than 120 characters")
    ])
    submit = SubmitField('Save Company Information')

class InvoiceTemplateForm(FlaskForm):
    invoice_logo = FileField('Company Logo', validators=[
        Optional(),
        FileAllowed(['jpg', 'jpeg', 'png', 'gif'], 'Images only!')
    ])
    remove_logo = HiddenField('Remove Logo')
    invoice_template = SelectField('Invoice Template', choices=[
        ('default', 'Default - Professional'),
        ('modern', 'Modern - Clean & Minimal'),
        ('classic', 'Classic - Traditional Business'),
        ('creative', 'Creative - Bold & Colorful')
    ])
    invoice_color_primary = StringField('Primary Color', validators=[
        Optional(),
        Length(max=10, message="Color code must be in proper format")
    ])
    invoice_color_secondary = StringField('Secondary Color', validators=[
        Optional(),
        Length(max=10, message="Color code must be in proper format")
    ])
    invoice_footer_text = TextAreaField('Invoice Footer Text', validators=[
        Optional(),
        Length(max=500, message="Footer text must be less than 500 characters")
    ])
    submit = SubmitField('Save Invoice Settings')