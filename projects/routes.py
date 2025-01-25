from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app import db
from models import Project, Task, TimeEntry, Client
from projects.forms import ProjectForm, TaskForm, TimeEntryForm

projects_bp = Blueprint('projects', __name__)

@projects_bp.route('/')
@projects_bp.route('/dashboard')
@login_required
def dashboard():
    projects = Project.query.filter_by(user_id=current_user.id).all()
    tasks = Task.query.join(Project).filter(Project.user_id == current_user.id).all()
    return render_template('dashboard.html', projects=projects, tasks=tasks)

@projects_bp.route('/projects')
@login_required
def list_projects():
    projects = Project.query.filter_by(user_id=current_user.id).all()
    return render_template('projects/list.html', projects=projects)

@projects_bp.route('/projects/new', methods=['GET', 'POST'])
@login_required
def create_project():
    form = ProjectForm()
    form.client_id.choices = [(c.id, c.name) for c in Client.query.filter_by(user_id=current_user.id)]
    
    if form.validate_on_submit():
        project = Project(
            name=form.name.data,
            description=form.description.data,
            start_date=form.start_date.data,
            end_date=form.end_date.data,
            client_id=form.client_id.data,
            user_id=current_user.id
        )
        db.session.add(project)
        db.session.commit()
        flash('Project created successfully')
        return redirect(url_for('projects.list_projects'))
    
    return render_template('projects/detail.html', form=form, title='New Project')

@projects_bp.route('/projects/<int:id>')
@login_required
def view_project(id):
    project = Project.query.get_or_404(id)
    if project.user_id != current_user.id:
        flash('Access denied')
        return redirect(url_for('projects.list_projects'))
    return render_template('projects/detail.html', project=project)

@projects_bp.route('/tasks/new', methods=['GET', 'POST'])
@login_required
def create_task():
    form = TaskForm()
    form.project_id.choices = [(p.id, p.name) for p in Project.query.filter_by(user_id=current_user.id)]
    
    if form.validate_on_submit():
        task = Task(
            title=form.title.data,
            description=form.description.data,
            due_date=form.due_date.data,
            project_id=form.project_id.data
        )
        db.session.add(task)
        db.session.commit()
        flash('Task created successfully')
        return redirect(url_for('projects.view_project', id=form.project_id.data))
    
    return render_template('projects/task_form.html', form=form)

@projects_bp.route('/time-entries/new', methods=['GET', 'POST'])
@login_required
def create_time_entry():
    form = TimeEntryForm()
    form.project_id.choices = [(p.id, p.name) for p in Project.query.filter_by(user_id=current_user.id)]
    
    if form.validate_on_submit():
        entry = TimeEntry(
            start_time=form.start_time.data,
            end_time=form.end_time.data,
            description=form.description.data,
            project_id=form.project_id.data,
            task_id=form.task_id.data if form.task_id.data else None
        )
        db.session.add(entry)
        db.session.commit()
        flash('Time entry recorded successfully')
        return redirect(url_for('projects.dashboard'))
    
    return render_template('projects/time_entry_form.html', form=form)
