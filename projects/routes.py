import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from sqlalchemy import func, extract, desc, cast, String
from app import db, logger
from models import Project, Task, TimeEntry, Client, Invoice # Added Invoice import
from projects.forms import ProjectForm, TaskForm, TimeEntryForm, BatchTimeEntryForm, TimeEntryFilterForm, BatchHoursEntryForm, SingleEntryForm
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from errors import handle_db_errors, UserFriendlyError
import calendar

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

        # Count pending invoices - Using a more efficient query and including both pending and draft invoices
        # This ensures the dashboard "Pending Invoices" tile matches what users see on the invoices page
        pending_invoices = Invoice.query.join(
            Project, Invoice.project_id == Project.id
        ).filter(
            Project.user_id == current_user.id,
            Invoice.status.in_(['pending', 'draft'])  # Count both pending and draft as they need attention
        ).count()

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

@projects_bp.route('/projects/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def edit_project(id):
    try:
        # Get the project with security check
        project = Project.query.filter_by(id=id, user_id=current_user.id).first_or_404()
        
        # Initialize form with project data
        form = ProjectForm(obj=project)
        
        # Populate client choices
        form.client_id.choices = [(c.id, c.name) for c in Client.query.filter_by(user_id=current_user.id)]
        
        if form.validate_on_submit():
            try:
                # Update project attributes
                project.name = form.name.data
                project.description = form.description.data
                project.start_date = form.start_date.data
                project.end_date = form.end_date.data
                project.client_id = form.client_id.data
                
                db.session.commit()
                flash('Project updated successfully', 'success')
                return redirect(url_for('projects.view_project', id=project.id))
            except SQLAlchemyError as e:
                db.session.rollback()
                logger.error(f"Error updating project {id}: {str(e)}")
                flash('Error updating project. Please try again.', 'danger')
        
        return render_template('projects/edit.html', form=form, project=project)
    except Exception as e:
        logger.error(f"Unexpected error in edit_project {id}: {str(e)}")
        flash('An unexpected error occurred. Please try again.', 'danger')
        return redirect(url_for('projects.list_projects'))
        
@projects_bp.route('/projects/<int:id>/delete', methods=['POST'])
@login_required
@handle_db_errors
def delete_project(id):
    try:
        # Get the project with security check
        project = Project.query.filter_by(id=id, user_id=current_user.id).first_or_404()
        
        project_name = project.name
        
        # Delete the project - cascading will handle related records
        db.session.delete(project)
        db.session.commit()
        
        flash(f'Project "{project_name}" has been deleted successfully', 'success')
        return redirect(url_for('projects.list_projects'))
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Error deleting project {id}: {str(e)}")
        flash('Error deleting project. Please try again.', 'danger')
        return redirect(url_for('projects.view_project', id=id))
    except Exception as e:
        logger.error(f"Unexpected error in delete_project {id}: {str(e)}")
        flash('An unexpected error occurred. Please try again.', 'danger')
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
                # Store original project ID to check if it changed
                original_project_id = task.project_id
                
                # Verify new project belongs to user if changed
                if original_project_id != form.project_id.data:
                    project = Project.query.filter_by(id=form.project_id.data, user_id=current_user.id).first()
                    if not project:
                        flash('Invalid project selection', 'danger')
                        return redirect(url_for('projects.edit_task', id=id))

                # Update task details
                task.title = form.title.data
                task.description = form.description.data
                task.due_date = form.due_date.data
                task.status = form.status.data
                task.project_id = form.project_id.data
                
                # If project has changed, update all time entries associated with this task
                if original_project_id != form.project_id.data:
                    time_entries = TimeEntry.query.filter_by(task_id=task.id).all()
                    entries_count = 0
                    
                    for entry in time_entries:
                        entry.project_id = form.project_id.data
                        db.session.add(entry)
                        entries_count += 1
                    
                    if entries_count > 0:
                        logger.info(f"Updated {entries_count} time entries from project {original_project_id} to project {form.project_id.data}")
                        flash(f'Updated {entries_count} time entries to the new project.', 'info')
                
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
    # Get component logger
    proj_logger = logging.getLogger('projects')
    proj_logger.info(f"User {current_user.id} creating new time entry")
    
    try:
        # Verify project belongs to user
        project_id = request.form.get('project_id', type=int)
        project = Project.query.filter_by(
            id=project_id,
            user_id=current_user.id
        ).first_or_404()

        # Handle and validate form input for start_time
        start_time_str = request.form.get('start_time')
        if start_time_str:
            try:
                # Try to parse with time if provided
                if ' ' in start_time_str:
                    start_time = datetime.strptime(start_time_str, '%Y-%m-%d %H:%M')
                else:
                    start_time = datetime.strptime(start_time_str, '%Y-%m-%d')
            except ValueError as e:
                proj_logger.warning(f"Invalid start_time format: {start_time_str} - {str(e)}")
                flash('Invalid start time format. Please use YYYY-MM-DD or YYYY-MM-DD HH:MM', 'danger')
                return redirect(url_for('projects.view_project', id=project.id))
        else:
            start_time = datetime.utcnow()
            proj_logger.info(f"No start_time provided, using current time: {start_time}")

        # Check if we have end_time and calculate duration, or use provided duration
        end_time = None
        duration = None
        
        end_time_str = request.form.get('end_time')
        duration_input = request.form.get('duration', type=int)
        
        # If end_time is provided, parse and calculate duration
        if end_time_str:
            try:
                # Try to parse with time if provided
                if ' ' in end_time_str:
                    end_time = datetime.strptime(end_time_str, '%Y-%m-%d %H:%M')
                else:
                    end_time = datetime.strptime(end_time_str, '%Y-%m-%d')
                    
                # Calculate duration in minutes
                time_diff = end_time - start_time
                if time_diff.total_seconds() <= 0:
                    proj_logger.warning(f"End time ({end_time}) is before start time ({start_time})")
                    flash('End time must be after start time', 'danger')
                    return redirect(url_for('projects.view_project', id=project.id))
                
                # Calculate duration in minutes
                duration = int(time_diff.total_seconds() / 60)
                proj_logger.info(f"Calculated duration: {duration} minutes from {start_time} to {end_time}")
                
            except ValueError as e:
                proj_logger.warning(f"Invalid end_time format: {end_time_str} - {str(e)}")
                flash('Invalid end time format. Please use YYYY-MM-DD or YYYY-MM-DD HH:MM', 'danger')
                return redirect(url_for('projects.view_project', id=project.id))
        # If no end_time but duration is provided, use it
        elif duration_input:
            if duration_input <= 0:
                proj_logger.warning(f"Invalid duration: {duration_input}")
                flash('Duration must be a positive number', 'danger')
                return redirect(url_for('projects.view_project', id=project.id))
            duration = duration_input
        # If neither end_time nor duration is provided
        else:
            proj_logger.warning("Neither end_time nor duration provided")
            flash('Please provide either an end time or a duration', 'danger')
            return redirect(url_for('projects.view_project', id=project.id))

        # Create and save the time entry
        try:
            task_id = request.form.get('task_id', type=int)
            description = request.form.get('description', '')
            
            # Get billable status (checkboxes are only present in the form data when checked)
            billable = 'billable' in request.form
            
            entry = TimeEntry(
                project_id=project.id,
                task_id=task_id,
                start_time=start_time,
                end_time=end_time,
                duration=duration,
                description=description,
                billable=billable
            )

            db.session.add(entry)
            db.session.commit()
            proj_logger.info(f"Time entry created successfully: id={entry.id}, project={project.id}, duration={duration}")
            flash('Time entry recorded successfully', 'success')
        except SQLAlchemyError as e:
            db.session.rollback()
            proj_logger.error(f"Error creating time entry: {str(e)}")
            flash('Error recording time entry. Please try again.', 'danger')

        return redirect(url_for('projects.view_project', id=project.id))
    except Exception as e:
        logger.error(f"Unexpected error in create_time_entry: {str(e)}")
        flash('An unexpected error occurred. Please try again.', 'danger')
        return redirect(url_for('projects.list_projects'))

@projects_bp.route('/time-entries/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def edit_time_entry(id):
    # Get component logger
    proj_logger = logging.getLogger('projects')
    
    try:
        # Fetch the time entry and verify ownership
        time_entry = TimeEntry.query.join(Project).filter(
            TimeEntry.id == id,
            Project.user_id == current_user.id
        ).first_or_404()
        
        form = TimeEntryForm()
        
        # Populate project choices
        user_projects = Project.query.filter_by(user_id=current_user.id).all()
        form.project_id.choices = [(p.id, p.name) for p in user_projects]
        
        # Populate task choices based on the selected project
        # For GET requests, use the time entry's project
        # For POST requests, use the project selected in the form
        if request.method == 'GET':
            project_id_for_tasks = time_entry.project_id
        else:
            project_id_for_tasks = request.form.get('project_id', time_entry.project_id, type=int)
            
        project_tasks = Task.query.filter_by(project_id=project_id_for_tasks).all()
        # Convert task choices to the expected format
        task_choices = [(0, 'No Task')]
        for task in project_tasks:
            task_choices.append((task.id, task.title))
        form.task_id.choices = task_choices
        
        if form.validate_on_submit():
            try:
                # Verify the project belongs to the user
                project = Project.query.filter_by(
                    id=form.project_id.data,
                    user_id=current_user.id
                ).first_or_404()
                
                # Calculate duration if both start and end times are provided
                duration = None
                if form.start_time.data and form.end_time.data:
                    # Ensure end time is after start time
                    if form.end_time.data <= form.start_time.data:
                        flash('End time must be after start time', 'danger')
                        return render_template('projects/edit_time_entry.html', form=form, time_entry=time_entry)
                    
                    time_diff = form.end_time.data - form.start_time.data
                    duration = int(time_diff.total_seconds() / 60)
                else:
                    # Keep the existing duration if only one of the times is updated
                    duration = time_entry.duration
                
                # Update time entry
                time_entry.project_id = form.project_id.data
                time_entry.task_id = form.task_id.data if form.task_id.data > 0 else None
                time_entry.start_time = form.start_time.data
                time_entry.end_time = form.end_time.data
                time_entry.duration = duration
                time_entry.description = form.description.data
                time_entry.billable = form.billable.data
                
                db.session.commit()
                proj_logger.info(f"Time entry updated successfully: id={time_entry.id}")
                flash('Time entry updated successfully', 'success')
                return redirect(url_for('projects.view_project', id=time_entry.project_id))
                
            except SQLAlchemyError as e:
                db.session.rollback()
                proj_logger.error(f"Error updating time entry {id}: {str(e)}")
                flash('Error updating time entry. Please try again.', 'danger')
        
        # Populate form with existing data
        elif request.method == 'GET':
            # Set the project and task fields
            form.project_id.data = time_entry.project_id
            form.task_id.data = time_entry.task_id if time_entry.task_id else 0
            
            # We need to manually format the datetime fields to match the form's expected format
            if time_entry.start_time:
                # No need to convert, directly use the datetime object
                form.start_time.data = time_entry.start_time
            
            if time_entry.end_time:
                # No need to convert, directly use the datetime object 
                form.end_time.data = time_entry.end_time
            
            form.description.data = time_entry.description
            form.billable.data = time_entry.billable
        
        return render_template('projects/edit_time_entry.html', form=form, time_entry=time_entry)
        
    except Exception as e:
        logger.error(f"Error editing time entry {id}: {str(e)}")
        flash('An error occurred while accessing the time entry. Please try again.', 'danger')
        return redirect(url_for('projects.list_projects'))

@projects_bp.route('/time-entries/<int:id>/delete', methods=['POST'])
@login_required
@handle_db_errors
def delete_time_entry(id):
    # Get component logger
    proj_logger = logging.getLogger('projects')
    
    try:
        # Fetch the time entry and verify ownership
        time_entry = TimeEntry.query.join(Project).filter(
            TimeEntry.id == id,
            Project.user_id == current_user.id
        ).first_or_404()
        
        # Store project ID for redirect after deletion
        project_id = time_entry.project_id
        
        # Delete the time entry
        db.session.delete(time_entry)
        db.session.commit()
        
        proj_logger.info(f"Time entry {id} deleted successfully by user {current_user.id}")
        flash('Time entry deleted successfully', 'success')
        
        return redirect(url_for('projects.view_project', id=project_id))
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting time entry {id}: {str(e)}")
        flash('An error occurred while deleting the time entry. Please try again.', 'danger')
        return redirect(url_for('projects.list_projects'))

@projects_bp.route('/time-entries/statistics', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def time_entry_statistics():
    """Display detailed statistics about time entries"""
    try:
        # Initialize filter form
        filter_form = TimeEntryFilterForm()
        
        # Populate project choices for filter
        user_projects = Project.query.filter_by(user_id=current_user.id).all()
        filter_form.project_id.choices = [(0, 'All Projects')] + [(p.id, p.name) for p in user_projects]
        
        # Default to all tasks
        filter_form.task_id.choices = [(0, 'All Tasks')]
        
        # Apply filters if form is submitted
        if filter_form.validate_on_submit():
            # Build query with filters
            query = TimeEntry.query.join(Project).filter(Project.user_id == current_user.id)
            
            # Date range filter
            if filter_form.date_from.data:
                query = query.filter(TimeEntry.start_time >= filter_form.date_from.data)
            if filter_form.date_to.data:
                query = query.filter(TimeEntry.start_time <= filter_form.date_to.data)
                
            # Project filter
            if filter_form.project_id.data != 0:
                query = query.filter(TimeEntry.project_id == filter_form.project_id.data)
                
                # Load tasks for selected project
                tasks = Task.query.filter_by(project_id=filter_form.project_id.data).all()
                task_choices = [(0, 'All Tasks')] + [(t.id, t.title) for t in tasks]
                filter_form.task_id.choices = task_choices
                
                # Task filter
                if filter_form.task_id.data != 0:
                    query = query.filter(TimeEntry.task_id == filter_form.task_id.data)
            
            # Billable status filter
            if filter_form.billable.data == 1:  # Billable only
                query = query.filter(TimeEntry.billable == True)
            elif filter_form.billable.data == 2:  # Non-billable only
                query = query.filter(TimeEntry.billable == False)
                
            # Duration filters
            if filter_form.duration_min.data is not None:
                query = query.filter(TimeEntry.duration >= filter_form.duration_min.data)
            if filter_form.duration_max.data is not None:
                query = query.filter(TimeEntry.duration <= filter_form.duration_max.data)
            
            # Get filtered time entries
            time_entries = query.order_by(TimeEntry.start_time.desc()).all()
        else:
            # Get all time entries for initial page load, limited to past 30 days
            thirty_days_ago = datetime.utcnow() - timedelta(days=30)
            time_entries = TimeEntry.query.join(Project).filter(
                Project.user_id == current_user.id,
                TimeEntry.start_time >= thirty_days_ago
            ).order_by(TimeEntry.start_time.desc()).all()
            
        # Calculate statistics
        total_duration = sum(entry.duration for entry in time_entries)
        billable_duration = sum(entry.duration for entry in time_entries if entry.billable)
        non_billable_duration = total_duration - billable_duration
        billable_percentage = (billable_duration / total_duration * 100) if total_duration > 0 else 0
        
        # Billable status distribution
        billable_data = {
            'Billable': billable_duration,
            'Non-Billable': non_billable_duration
        }
        
        # Project time distribution
        project_data = {}
        project_billable_data = {}
        
        for entry in time_entries:
            project_name = entry.project.name
            
            # Total time by project
            if project_name not in project_data:
                project_data[project_name] = 0
                project_billable_data[project_name] = {'billable': 0, 'non_billable': 0}
                
            project_data[project_name] += entry.duration
            
            # Track billable vs non-billable time by project
            if entry.billable:
                project_billable_data[project_name]['billable'] += entry.duration
            else:
                project_billable_data[project_name]['non_billable'] += entry.duration
            
        # Daily distribution (for chart)
        daily_data = {}
        for entry in time_entries:
            date_key = entry.start_time.strftime('%Y-%m-%d')
            if date_key not in daily_data:
                daily_data[date_key] = 0
            daily_data[date_key] += entry.duration / 60.0  # Convert to hours
        
        # Sort by date
        daily_labels = sorted(daily_data.keys())
        daily_values = [daily_data[date] for date in daily_labels]
        
        # Day of week distribution
        weekday_data = [0] * 7
        for entry in time_entries:
            weekday = entry.start_time.weekday()
            weekday_data[weekday] += entry.duration / 60.0  # Convert to hours
            
        weekday_labels = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        
        # Task level analysis if we have a specific project selected
        task_data = {}
        if filter_form.project_id.data != 0:
            for entry in time_entries:
                if entry.task:
                    task_name = entry.task.title
                    if task_name not in task_data:
                        task_data[task_name] = 0
                    task_data[task_name] += entry.duration
        
        # Format duration for display
        def format_duration(minutes):
            hours = minutes // 60
            mins = minutes % 60
            return f"{hours}h {mins}m"
        
        total_formatted = format_duration(total_duration)
        billable_formatted = format_duration(billable_duration)
        
        # Format non-billable duration
        non_billable_formatted = format_duration(non_billable_duration)
        
        return render_template(
            'projects/time_statistics.html',
            filter_form=filter_form,
            time_entries=time_entries,
            total_duration=total_duration,
            total_formatted=total_formatted,
            billable_duration=billable_duration,
            billable_formatted=billable_formatted,
            non_billable_duration=non_billable_duration,
            non_billable_formatted=non_billable_formatted,
            billable_percentage=billable_percentage,
            billable_data=billable_data,
            project_data=project_data,
            project_billable_data=project_billable_data,
            daily_labels=daily_labels,
            daily_values=daily_values,
            weekday_labels=weekday_labels,
            weekday_data=weekday_data,
            task_data=task_data
        )
    except Exception as e:
        logger.error(f"Error loading time entry statistics: {str(e)}")
        flash('An error occurred while loading time entry statistics. Please try again.', 'danger')
        return redirect(url_for('projects.dashboard'))

@projects_bp.route('/time-entries/batch', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def batch_time_entries():
    """Batch time entry submission with hours input"""
    try:
        # Create a new batch time entry form
        form = BatchHoursEntryForm()
        
        # Get all projects for the current user
        projects = Project.query.filter_by(user_id=current_user.id).all()
        project_choices = [(p.id, p.name) for p in projects]
        
        # For each entry form, set the project and task choices
        for entry_form in form.entries:
            # Set project choices - this must be done before validation
            entry_form.form.project_id.choices = project_choices
            
            # Set task choices (initially just "No Task")
            entry_form.form.task_id.choices = [(0, 'No Task')]
            
            # If a project is selected, load its tasks
            if entry_form.form.project_id.data:
                try:
                    project_id = int(entry_form.form.project_id.data)
                    tasks = Task.query.filter_by(project_id=project_id).all()
                    task_choices = [(0, 'No Task')] + [(t.id, t.title) for t in tasks]
                    entry_form.form.task_id.choices = task_choices
                except (ValueError, TypeError):
                    # Handle case where project_id is not a valid integer
                    pass
        
        # If this is a POST request and the form is submitted
        if request.method == 'POST':
            # Validate the form
            if form.validate_on_submit():
                # Lists to track entry processing status
                valid_entries = []
                error_entries = []
                
                # First pass: validate all entries without saving
                for index, entry_data in enumerate(form.entries.data):
                    entry_number = index + 1
                    entry_error = False
                    
                    # Validate that the project belongs to the user
                    project_id = entry_data['project_id']
                    project = Project.query.filter_by(
                        id=project_id,
                        user_id=current_user.id
                    ).first()
                    
                    if not project:
                        error_entries.append(f"Entry {entry_number}: Invalid project selection")
                        entry_error = True
                        continue
                    
                    # Validate date and hours
                    try:
                        entry_date_str = entry_data['entry_date']
                        if isinstance(entry_date_str, str):
                            # Parse the date string from the form
                            entry_date = datetime.strptime(entry_date_str, '%Y-%m-%d')
                        else:
                            # It's already a datetime object
                            entry_date = entry_date_str
                            
                        hours = float(entry_data['hours'])
                        
                        # Validate hours (should be between 0.1 and 24)
                        if hours <= 0 or hours > 24:
                            error_entries.append(f"Entry {entry_number}: Hours must be between 0.1 and 24")
                            entry_error = True
                            continue
                            
                        # Convert hours to minutes for the duration
                        duration_minutes = int(hours * 60)
                        
                        # Set start time to the selected date at 9 AM
                        start_time = datetime.combine(entry_date.date(), datetime.min.time().replace(hour=9))
                        
                        # Calculate end time by adding hours
                        end_time = start_time + timedelta(minutes=duration_minutes)
                    except (ValueError, TypeError) as e:
                        logger.error(f"Error processing date/time: {str(e)}")
                        error_entries.append(f"Entry {entry_number}: Invalid date or hours format")
                        entry_error = True
                        continue
                    
                    # Check if the task is specified and belongs to the selected project
                    task_id = None
                    if entry_data['task_id'] and int(entry_data['task_id']) > 0:
                        task = Task.query.filter_by(
                            id=int(entry_data['task_id']),
                            project_id=project.id
                        ).first()
                        if task:
                            task_id = task.id
                    
                    # If all validations pass, add to valid entries
                    if not entry_error:
                        valid_entries.append({
                            'project_id': project.id,
                            'task_id': task_id,
                            'start_time': start_time,
                            'end_time': end_time,
                            'duration': duration_minutes,
                            'description': entry_data['description'],
                            'billable': entry_data.get('billable', True),
                            'entry_number': entry_number
                        })
                
                # Only proceed with database transaction if we have valid entries
                if valid_entries:
                    try:
                        # Create all time entries in a single transaction
                        entries_created = 0
                        for entry_data in valid_entries:
                            time_entry = TimeEntry(
                                project_id=entry_data['project_id'],
                                task_id=entry_data['task_id'],
                                start_time=entry_data['start_time'],
                                end_time=entry_data['end_time'],
                                duration=entry_data['duration'],
                                description=entry_data['description'],
                                billable=entry_data['billable']
                            )
                            db.session.add(time_entry)
                            entries_created += 1
                        
                        # Commit all entries at once
                        db.session.commit()
                        
                        # Show success message
                        flash(f'Successfully created {entries_created} time entries', 'success')
                        
                        # Show errors if any
                        if error_entries:
                            for error in error_entries:
                                flash(error, 'warning')
                                
                        return redirect(url_for('projects.dashboard'))
                    
                    except SQLAlchemyError as e:
                        # Roll back the transaction if any error occurs
                        db.session.rollback()
                        logger.error(f"Database error saving batch time entries: {str(e)}")
                        flash('Error saving time entries. Please try again.', 'danger')
                else:
                    # No valid entries to save
                    flash('No valid time entries were submitted', 'warning')
                    
                    # Show specific errors
                    for error in error_entries:
                        flash(error, 'danger')
            else:
                # Form validation errors
                for field, errors in form.errors.items():
                    for error in errors:
                        flash(f'Error in {field}: {error}', 'danger')
        
        # Initialize the form with one empty entry if no entries exist
        if len(form.entries) == 0:
            # Add an empty entry - we'll populate the choices after
            form.entries.append_entry({})
            
            # Set project choices for the new entry
            form.entries[0].form.project_id.choices = project_choices
            form.entries[0].form.task_id.choices = [(0, 'No Task')]
            
            # Default project selection if there are projects
            if projects:
                default_project = projects[0]
                form.entries[0].form.project_id.data = default_project.id
                
                # Load tasks for the default project
                tasks = Task.query.filter_by(project_id=default_project.id).all()
                task_choices = [(0, 'No Task')] + [(t.id, t.title) for t in tasks]
                form.entries[0].form.task_id.choices = task_choices
                
                # If there are tasks, pre-select the first one
                if tasks:
                    form.entries[0].form.task_id.data = tasks[0].id
        
        # Set default values for all entries
        for entry_form in form.entries:
            # Set default values for fields if not already set
            if not entry_form.form.hours.data:
                entry_form.form.hours.data = 1.0
                
            if entry_form.form.billable.data is None:
                entry_form.form.billable.data = True
        
        # Render the template with the form
        return render_template(
            'projects/batch_time_entries.html',
            form=form,
            projects=projects
        )
                
    except Exception as e:
        import traceback
        logger.error(f"Error in batch time entries: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        flash('An error occurred while processing time entries. Please try again.', 'danger')
        return redirect(url_for('projects.dashboard'))

@projects_bp.route('/projects/<int:project_id>/tasks')
@login_required
def get_project_tasks(project_id):
    """Get tasks for a specific project (for AJAX requests)"""
    try:
        # Verify the project belongs to the current user
        project = Project.query.filter_by(
            id=project_id,
            user_id=current_user.id
        ).first_or_404()
        
        # Get all tasks for the project
        tasks = Task.query.filter_by(project_id=project_id).all()
        
        # Format tasks as JSON
        tasks_json = [{'id': task.id, 'title': task.title} for task in tasks]
        
        return jsonify(tasks_json)
        
    except Exception as e:
        logger.error(f"Error fetching tasks for project {project_id}: {str(e)}")
        return jsonify([]), 400