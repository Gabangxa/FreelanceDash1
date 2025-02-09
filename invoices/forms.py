from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, DateTimeField, SelectField, FloatField, SubmitField
from wtforms.validators import DataRequired

class InvoiceForm(FlaskForm):
    client_id = SelectField('Client', coerce=int, validators=[DataRequired()])
    project_id = SelectField('Project', coerce=int, validators=[DataRequired()])
    amount = FloatField('Amount', validators=[DataRequired()])
    due_date = DateTimeField('Due Date', validators=[DataRequired()], format='%Y-%m-%d')
    notes = TextAreaField('Notes')
    submit = SubmitField('Create Invoice')

class InvoiceItemForm(FlaskForm):
    description = TextAreaField('Description', validators=[DataRequired()])
    quantity = FloatField('Quantity', validators=[DataRequired()])
    rate = FloatField('Rate', validators=[DataRequired()])
    submit = SubmitField('Add Item')