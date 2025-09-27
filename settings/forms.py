from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import StringField, TextAreaField, SelectField, SubmitField, HiddenField, PasswordField, BooleanField
from wtforms.validators import Optional, Email, Length, URL, DataRequired, EqualTo, ValidationError
from flask_login import current_user
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

class DeleteAccountForm(FlaskForm):
    """Form for account deletion with confirmation steps."""
    confirmation = StringField('Confirm Email', validators=[
        DataRequired(message="Please enter your email address"),
    ])
    password = PasswordField('Password', validators=[
        DataRequired(message="Password is required")
    ])
    understand = BooleanField('I understand', validators=[
        DataRequired(message="You must acknowledge that this action is permanent")
    ])
    submit = SubmitField('Delete Account')
    
    def validate_confirmation(self, field):
        """Validate that the confirmation email matches the user's email."""
        if field.data != current_user.email:
            raise ValidationError("The email address you entered doesn't match your account email")
            
    def validate_password(self, field):
        """Validate that the password is correct."""
        if not current_user.check_password(field.data):
            raise ValidationError("Incorrect password. Please try again.")

class NotificationSettingsForm(FlaskForm):
    """Form for managing notification preferences."""
    
    # Email notification preferences
    email_enabled = BooleanField('Enable Email Notifications')
    email_webhook_events = BooleanField('Webhook Events (External integrations)')
    email_project_updates = BooleanField('Project Updates (Tasks, time entries)')
    email_invoice_updates = BooleanField('Invoice Updates (Created, paid, overdue)')
    email_payment_notifications = BooleanField('Payment Notifications (Successful payments)')
    email_system_notifications = BooleanField('System Notifications (Important updates)')
    
    # In-app notification preferences
    inapp_enabled = BooleanField('Enable In-App Notifications')
    inapp_webhook_events = BooleanField('Webhook Events')
    inapp_project_updates = BooleanField('Project Updates')
    inapp_invoice_updates = BooleanField('Invoice Updates')
    inapp_payment_notifications = BooleanField('Payment Notifications')
    inapp_system_notifications = BooleanField('System Notifications')
    
    # Delivery preferences
    digest_frequency = SelectField('Email Digest Frequency', choices=[
        ('immediate', 'Immediate - Send emails right away'),
        ('hourly', 'Hourly - Send digest every hour'),
        ('daily', 'Daily - Send daily digest'),
        ('weekly', 'Weekly - Send weekly digest')
    ])
    
    # Quiet hours
    quiet_hours_enabled = BooleanField('Enable Quiet Hours (No notifications during specified times)')
    quiet_hours_start = StringField('Start Time (HH:MM)', validators=[
        Optional(),
        Length(max=5, message="Time must be in HH:MM format")
    ])
    quiet_hours_end = StringField('End Time (HH:MM)', validators=[
        Optional(),
        Length(max=5, message="Time must be in HH:MM format")
    ])
    
    timezone = SelectField('Timezone', choices=[
        ('UTC', 'UTC'),
        ('America/New_York', 'Eastern Time (ET)'),
        ('America/Chicago', 'Central Time (CT)'),
        ('America/Denver', 'Mountain Time (MT)'),
        ('America/Los_Angeles', 'Pacific Time (PT)'),
        ('Europe/London', 'London'),
        ('Europe/Paris', 'Paris'),
        ('Europe/Berlin', 'Berlin'),
        ('Asia/Tokyo', 'Tokyo'),
        ('Asia/Shanghai', 'Shanghai'),
        ('Australia/Sydney', 'Sydney')
    ])
    
    submit = SubmitField('Save Notification Settings')