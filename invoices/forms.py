from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, DateTimeField, SelectField, FloatField, SubmitField, FieldList, FormField
from wtforms.validators import DataRequired

class InvoiceItemForm(FlaskForm):
    description = TextAreaField('Description', validators=[DataRequired()])
    quantity = FloatField('Quantity', validators=[DataRequired()])
    rate = FloatField('Rate', validators=[DataRequired()])
    amount = FloatField('Amount')

    class Meta:
        csrf = False  # Disable CSRF for nested form

class InvoiceForm(FlaskForm):
    client_id = SelectField('Client', coerce=int, validators=[DataRequired()])
    project_id = SelectField('Project', coerce=int, validators=[DataRequired()])
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
    due_date = DateTimeField('Due Date', validators=[DataRequired()], format='%Y-%m-%d')
    notes = TextAreaField('Notes')
    items = FieldList(FormField(InvoiceItemForm), min_entries=1)
    submit = SubmitField('Save Invoice')

    def __init__(self, *args, **kwargs):
        super(InvoiceForm, self).__init__(*args, **kwargs)
        # Initialize project_id choices with empty list to avoid None error
        self.project_id.choices = []