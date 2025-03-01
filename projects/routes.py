from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from app import db, logger
from models import Project, Task, TimeEntry, Client, Invoice # Added Invoice import
from projects.forms import ProjectForm, TaskForm, TimeEntryForm
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from errors import handle_db_errors, UserFriendlyError

projects_bp = Blueprint('projects', __name__)

@projects_bp.route('/')
@projects_bp.route('/dashboard')
@login_required
@handle_db_errors
def dashboard():
    try:
        # Get start and end of current week
        today = datetime.utcnow()
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)

        # Query all projects for current user
        projects = Project.query.filter_by(user_id=current_user.id).all()

        # Get tasks with status information
        tasks = Task.query.join(Project).filter(
            Project.user_id == current_user.id,
            Task.status != 'completed'
        ).order_by(Task.due_date.asc()).all()

        # Calculate weekly hours
        weekly_time_entries = TimeEntry.query.join(Project).filter(
            Project.user_id == current_user.id,
            TimeEntry.start_time >= start_of_week,
            TimeEntry.start_time <= end_of_week
        ).all()

        weekly_hours = sum(entry.duration for entry in weekly_time_entries) / 60.0

        # Get daily hours for chart
        daily_hours = [0] * 7
        for entry in weekly_time_entries:
            day_index = entry.start_time.weekday()
            daily_hours[day_index] += entry.duration / 60.0

        # Count pending invoices - modified to handle missing invoices relationship
        pending_invoices = 0
        for project in projects:
            pending_project_invoices = Invoice.query.filter_by(
                project_id=project.id,
                status='draft'
            ).count()
            pending_invoices += pending_project_invoices

        return render_template('dashboard.html',
                             projects=projects,
                             tasks=tasks,
                             weekly_hours=weekly_hours,
                             daily_hours=daily_hours,
                             pending_invoices=pending_invoices,
                             today=today.date())
    except SQLAlchemyError as e:
        logger.error(f"Database error in dashboard: {str(e)}")
        flash('Error loading dashboard data. Please try again.', 'danger')
        return render_template('dashboard.html', 
                             projects=[], 
                             tasks=[], 
                             weekly_hours=0, 
                             daily_hours=[0]*7, 
                             pending_invoices=0,
                             today=datetime.utcnow().date())

@projects_bp.route('/projects')
@login_required
@handle_db_errors
def list_projects():
    try:
        # Optimized query with eager loading
        projects = Project.query.filter_by(user_id=current_user.id).all()
        return render_template('projects/list.html', projects=projects)
    except SQLAlchemyError as e:
        logger.error(f"Database error in list_projects: {str(e)}")
        flash('Error loading projects. Please try again.', 'danger')
        return render_template('projects/list.html', projects=[])

@projects_bp.route('/projects/new', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def create_project():
    form = ProjectForm()

    try:
        form.client_id.choices = [(c.id, c.name) for c in Client.query.filter_by(user_id=current_user.id)]

        if form.validate_on_submit():
            try:
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
                flash('Project created successfully', 'success')
                return redirect(url_for('projects.list_projects'))
            except SQLAlchemyError as e:
                db.session.rollback()
                logger.error(f"Error creating project: {str(e)}")
                flash('Error creating project. Please try again.', 'danger')

        return render_template('projects/detail.html', form=form, title='New Project')
    except Exception as e:
        logger.error(f"Unexpected error in create_project: {str(e)}")
        flash('An unexpected error occurred. Please try again.', 'danger')
        return redirect(url_for('projects.list_projects'))

@projects_bp.route('/projects/<int:id>')
@login_required
@handle_db_errors
def view_project(id):
    try:
        # Enhanced query with security check and eager loading
        project = Project.query.filter_by(id=id, user_id=current_user.id).first_or_404()
        return render_template('projects/detail.html', project=project, today=datetime.utcnow().strftime('%Y-%m-%d'))
    except SQLAlchemyError as e:
        logger.error(f"Error viewing project {id}: {str(e)}")
        flash('Error loading project details. Please try again.', 'danger')
        return redirect(url_for('projects.list_projects'))

@projects_bp.route('/tasks/new', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def create_task():
    form = TaskForm()

    try:
        form.project_id.choices = [(p.id, p.name) for p in Project.query.filter_by(user_id=current_user.id)]

        if form.validate_on_submit():
            try:
                # Verify project belongs to user
                project = Project.query.filter_by(id=form.project_id.data, user_id=current_user.id).first()
                if not project:
                    flash('Invalid project selection', 'danger')
                    return redirect(url_for('projects.create_task'))

                task = Task(
                    title=form.title.data,
                    description=form.description.data,
                    due_date=form.due_date.data,
                    status=form.status.data,
                    project_id=form.project_id.data
                )
                db.session.add(task)
                db.session.commit()
                flash('Task created successfully', 'success')
                return redirect(url_for('projects.view_project', id=form.project_id.data))
            except SQLAlchemyError as e:
                db.session.rollback()
                logger.error(f"Error creating task: {str(e)}")
                flash('Error creating task. Please try again.', 'danger')

        return render_template('projects/task_form.html', form=form, title='New Task')
    except Exception as e:
        logger.error(f"Unexpected error in create_task: {str(e)}")
        flash('An unexpected error occurred. Please try again.', 'danger')
        return redirect(url_for('projects.list_projects'))

@projects_bp.route('/tasks/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def edit_task(id):
    try:
        # Secured query with join to ensure task belongs to user's project
        task = Task.query.join(Project).filter(
            Task.id == id,
            Project.user_id == current_user.id
        ).first_or_404()

        form = TaskForm(obj=task)
        form.project_id.choices = [(p.id, p.name) for p in Project.query.filter_by(user_id=current_user.id)]

        if form.validate_on_submit():
            try:
                # Verify new project belongs to user if changed
                if task.project_id != form.project_id.data:
                    project = Project.query.filter_by(id=form.project_id.data, user_id=current_user.id).first()
                    if not project:
                        flash('Invalid project selection', 'danger')
                        return redirect(url_for('projects.edit_task', id=id))

                task.title = form.title.data
                task.description = form.description.data
                task.due_date = form.due_date.data
                task.status = form.status.data
                task.project_id = form.project_id.data
                db.session.commit()
                flash('Task updated successfully', 'success')
                return redirect(url_for('projects.view_project', id=task.project_id))
            except SQLAlchemyError as e:
                db.session.rollback()
                logger.error(f"Error updating task {id}: {str(e)}")
                flash('Error updating task. Please try again.', 'danger')

        return render_template('projects/task_form.html', form=form, task=task, title='Edit Task')
    except SQLAlchemyError as e:
        logger.error(f"Error loading task {id}: {str(e)}")
        flash('Error loading task. Please try again.', 'danger')
        return redirect(url_for('projects.list_projects'))

@projects_bp.route('/time-entries/new', methods=['POST'])
@login_required
@handle_db_errors
def create_time_entry():
    try:
        # Verify project belongs to user
        project = Project.query.filter_by(
            id=request.form.get('project_id', type=int),
            user_id=current_user.id
        ).first_or_404()

        # Handle and validate form input
        start_time = request.form.get('start_time')
        if start_time:
            try:
                start_time = datetime.strptime(start_time, '%Y-%m-%d')
            except ValueError:
                flash('Invalid date format', 'danger')
                return redirect(url_for('projects.view_project', id=project.id))
        else:
            start_time = datetime.utcnow()

        duration = request.form.get('duration', type=int)
        if not duration or duration <= 0:
            flash('Duration must be a positive number', 'danger')
            return redirect(url_for('projects.view_project', id=project.id))

        try:
            entry = TimeEntry(
                project_id=project.id,
                task_id=request.form.get('task_id', type=int),
                start_time=start_time,
                duration=duration,
                description=request.form.get('description')
            )

            db.session.add(entry)
            db.session.commit()
            flash('Time entry recorded successfully', 'success')
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Error creating time entry: {str(e)}")
            flash('Error recording time entry. Please try again.', 'danger')

        return redirect(url_for('projects.view_project', id=project.id))
    except Exception as e:
        logger.error(f"Unexpected error in create_time_entry: {str(e)}")
        flash('An unexpected error occurred. Please try again.', 'danger')
        return redirect(url_for('projects.list_projects'))