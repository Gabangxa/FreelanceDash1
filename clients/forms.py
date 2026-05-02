from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateTimeField,
    Form,
    FormField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, Length, Optional


class InitialProjectForm(Form):
    """Nested sub-form for the optional initial project on client creation.

    This is a plain ``wtforms.Form`` (not ``FlaskForm``) so it can be
    mounted on the parent ``ClientForm`` via ``FormField`` without
    pulling in a duplicate CSRF token -- the parent form's CSRF token
    covers the whole submission.
    """

    name = StringField('Project Name', validators=[
        Optional(),
        Length(max=255, message="Project name must be less than 255 characters"),
    ])
    description = TextAreaField('Description', validators=[Optional()])
    start_date = DateTimeField('Start Date', validators=[Optional()], format='%Y-%m-%d')
    end_date = DateTimeField('End Date', validators=[Optional()], format='%Y-%m-%d')
    include = BooleanField('Create this project', default=True)

    def validate(self, extra_validators=None):
        # Run normal field validators first so type errors (e.g. bad date
        # strings) surface regardless of the include flag.
        base_ok = super().validate(extra_validators=extra_validators)

        # When the user has unchecked "Create this project" the rest of
        # the sub-form is irrelevant -- treat it as valid no matter what
        # was typed in the optional fields.
        if not self.include.data:
            return True

        ok = base_ok
        if not self.name.data or not self.name.data.strip():
            self.name.errors = list(self.name.errors) + [
                'Project name is required when creating a project.'
            ]
            ok = False
        if not self.start_date.data:
            self.start_date.errors = list(self.start_date.errors) + [
                'Start date is required when creating a project.'
            ]
            ok = False
        return ok


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
    project = FormField(InitialProjectForm)
    submit = SubmitField('Save Client')
