from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, DateTimeField, SelectField, SubmitField
from wtforms.validators import DataRequired

class ProjectForm(FlaskForm):
    name = StringField('Project Name', validators=[DataRequired()])
    description = TextAreaField('Description')
    start_date = DateTimeField('Start Date', validators=[DataRequired()], format='%Y-%m-%d')
    end_date = DateTimeField('End Date', format='%Y-%m-%d')
    client_id = SelectField('Client', coerce=int, validators=[DataRequired()])
    submit = SubmitField('Create Project')

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
    submit = SubmitField('Record Time')