from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    StringField,
    TextAreaField,
    DateTimeField,
    SelectField,
    DecimalField,
    SubmitField,
    FieldList,
    FormField,
)
from wtforms.validators import DataRequired, NumberRange, Length, Optional, ValidationError
from datetime import datetime


class InvoiceItemForm(FlaskForm):
    description = TextAreaField('Description', validators=[
        DataRequired(message="Description is required"),
        Length(min=1, max=500, message="Description must be between 1 and 500 characters")
    ])
    # DecimalField (not FloatField) so binary rounding never silently
    # eats a cent. ``places=4`` on quantity to allow fractional hours.
    quantity = DecimalField('Quantity', places=4, validators=[
        DataRequired(message="Quantity is required"),
        NumberRange(min=Decimal('0.01'), message="Quantity must be greater than 0"),
    ])
    rate = DecimalField('Rate', places=2, validators=[
        DataRequired(message="Rate is required"),
        NumberRange(min=Decimal('0.01'), message="Rate must be greater than 0"),
    ])
    # ``amount`` is computed server-side from quantity * rate, so the
    # form-side field is read-only and unvalidated -- the route ignores
    # any value posted here.
    amount = DecimalField('Amount', places=2, validators=[Optional()])

    class Meta:
        csrf = False  # Disable CSRF for nested form

class InvoiceForm(FlaskForm):
    client_id = SelectField('Client', coerce=int, validators=[
        DataRequired(message="Client selection is required")
    ])
    project_id = SelectField('Project', coerce=int, validators=[
        DataRequired(message="Project selection is required")
    ])
    currency = SelectField('Currency', choices=[
        ('USD', 'USD - US Dollar'),
        ('EUR', 'EUR - Euro'),
        ('GBP', 'GBP - British Pound'),
        ('JPY', 'JPY - Japanese Yen'),
        ('CAD', 'CAD - Canadian Dollar'),
        ('AUD', 'AUD - Australian Dollar'),
        ('ZAR', 'ZAR - South African Rand'),
        ('NGN', 'NGN - Nigerian Naira'),
        ('KES', 'KES - Kenyan Shilling'),
        ('GHS', 'GHS - Ghanaian Cedi'),
        ('BRL', 'BRL - Brazilian Real'),
        ('MXN', 'MXN - Mexican Peso'),
        ('SGD', 'SGD - Singapore Dollar'),
        ('AED', 'AED - United Arab Emirates Dirham')
    ], default='USD')
    status = SelectField('Status', choices=[
        ('draft', 'Draft'),
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('cancelled', 'Cancelled')
    ])
    due_date = DateTimeField('Due Date', validators=[
        DataRequired(message="Due date is required")
    ], format='%Y-%m-%d')
    notes = TextAreaField('Notes', validators=[
        Optional(),
        Length(max=1000, message="Notes must be less than 1000 characters")
    ])
    items = FieldList(FormField(InvoiceItemForm), min_entries=1)
    submit = SubmitField('Save Invoice')

    def __init__(self, *args, **kwargs):
        super(InvoiceForm, self).__init__(*args, **kwargs)
        # Initialize project_id choices with empty list to avoid None error
        self.project_id.choices = []

    def validate_due_date(self, field):
        if field.data and field.data < datetime.now():
            raise ValidationError('Due date cannot be in the past')

    def validate_items(self, field):
        if len(field.data) < 1:
            raise ValidationError('At least one invoice item is required')

        valid_items = 0
        for item in field.data:
            if item['description'] and item['quantity'] and item['rate']:
                # Decimal comparison is exact -- no float() coercion needed.
                if item['quantity'] > 0 and item['rate'] > 0:
                    valid_items += 1

        if valid_items < 1:
            raise ValidationError('At least one valid invoice item with description, quantity and rate is required')
