from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, DateTimeField, SelectField, SubmitField, BooleanField, IntegerField, FieldList, FormField, FloatField, Form
from wtforms.validators import DataRequired, Optional, NumberRange, ValidationError
from datetime import datetime

class ProjectForm(FlaskForm):
    name = StringField('Project Name', validators=[DataRequired()])
    description = TextAreaField('Description')
    start_date = DateTimeField('Start Date', validators=[DataRequired()], format='%Y-%m-%d')
    end_date = DateTimeField('End Date', format='%Y-%m-%d')
    client_id = SelectField('Client', coerce=int, validators=[DataRequired()])
    submit = SubmitField('Save Project')
    
    def __init__(self, *args, **kwargs):
        super(ProjectForm, self).__init__(*args, **kwargs)
        # Change button text if this is an edit form
        if kwargs.get('obj'):
            self.submit.label.text = 'Update Project'

class TaskForm(FlaskForm):
    title = StringField('Task Title', validators=[DataRequired()])
    description = TextAreaField('Description')
    due_date = DateTimeField('Due Date', format='%Y-%m-%d')
    project_id = SelectField('Project', coerce=int, validators=[DataRequired()])
    status = SelectField('Status', choices=[
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('on_hold', 'On Hold')
    ])
    submit = SubmitField('Save Task')

class TimeEntryForm(FlaskForm):
    project_id = SelectField('Project', coerce=int, validators=[DataRequired()])
    task_id = SelectField('Task', coerce=int)
    start_time = DateTimeField('Start Time', validators=[DataRequired()], format='%Y-%m-%d %H:%M')
    end_time = DateTimeField('End Time', format='%Y-%m-%d %H:%M')
    description = TextAreaField('Description')
    billable = BooleanField('Billable', default=True)
    submit = SubmitField('Record Time')

class BatchTimeEntryForm(FlaskForm):
    project_id = SelectField('Project', coerce=int, validators=[DataRequired()])
    task_id = SelectField('Task', coerce=int)
    date = DateTimeField('Date', validators=[DataRequired()], format='%Y-%m-%d', default=datetime.now)
    description = TextAreaField('Description')
    billable = BooleanField('Billable', default=True)
    
    # For batch editing
    action = SelectField('Action', choices=[
        ('delete', 'Delete Selected Entries'),
        ('change_project', 'Move to Project'),
        ('change_task', 'Assign to Task'),
        ('mark_billable', 'Mark as Billable'),
        ('mark_non_billable', 'Mark as Non-Billable')
    ])
    
    target_project_id = SelectField('Target Project', coerce=int)
    target_task_id = SelectField('Target Task', coerce=int)
    
    submit = SubmitField('Apply to Selected')

class SingleEntryForm(Form):
    """Form for a single time entry row in the batch submission form"""
    entry_date = DateTimeField('Date', format='%Y-%m-%d', validators=[DataRequired()], default=datetime.now)
    project_id = SelectField('Project', coerce=int, validators=[DataRequired()])
    task_id = SelectField('Task', coerce=int, validators=[Optional()])
    hours = FloatField('Hours', validators=[
        DataRequired(), 
        NumberRange(min=0.1, max=24, message="Hours must be between 0.1 and 24")
    ])
    description = TextAreaField('Description')
    billable = BooleanField('Billable', default=True)
    
    def __init__(self, *args, **kwargs):
        super(SingleEntryForm, self).__init__(*args, **kwargs)
        # Default task selection will be added in the route

class BatchHoursEntryForm(FlaskForm):
    """Form for batch time entry submission with hours instead of start/end times"""
    entries = FieldList(FormField(SingleEntryForm), min_entries=1)
    submit = SubmitField('Save All Entries')
    
    def validate_entries(self, field):
        if len(field.data) < 1:
            raise ValidationError("Please add at least one time entry")
        
        # Additional validation could be added here if needed

class TimeEntryFilterForm(FlaskForm):
    date_from = DateTimeField('From Date', format='%Y-%m-%d', validators=[Optional()])
    date_to = DateTimeField('To Date', format='%Y-%m-%d', validators=[Optional()])
    project_id = SelectField('Project', coerce=int, validators=[Optional()], default=0)
    task_id = SelectField('Task', coerce=int, validators=[Optional()], default=0)
    billable = SelectField('Billable Status', choices=[
        (0, 'All Entries'),
        (1, 'Billable Only'),
        (2, 'Non-Billable Only')
    ], coerce=int, default=0)
    duration_min = IntegerField('Min Duration (minutes)', validators=[Optional(), NumberRange(min=0)])
    duration_max = IntegerField('Max Duration (minutes)', validators=[Optional(), NumberRange(min=0)])
    
    submit = SubmitField('Apply Filters')