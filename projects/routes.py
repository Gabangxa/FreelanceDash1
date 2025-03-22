import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from sqlalchemy import func, extract, desc, cast, String
from app import db, logger
from models import Project, Task, TimeEntry, Client, Invoice # Added Invoice import
from projects.forms import ProjectForm, TaskForm, TimeEntryForm, BatchTimeEntryForm
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
            
            entry = TimeEntry(
                project_id=project.id,
                task_id=task_id,
                start_time=start_time,
                end_time=end_time,
                duration=duration,
                description=description
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
        if request.method == 'GET':
            project_tasks = Task.query.filter_by(project_id=time_entry.project_id).all()
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
            form.project_id.data = time_entry.project_id
            form.task_id.data = time_entry.task_id if time_entry.task_id else 0
            form.start_time.data = time_entry.start_time
            form.end_time.data = time_entry.end_time
            form.description.data = time_entry.description
        
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

@projects_bp.route('/time-entries/statistics')
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
        billable_percentage = (billable_duration / total_duration * 100) if total_duration > 0 else 0
        
        # Project time distribution
        project_data = {}
        for entry in time_entries:
            project_name = entry.project.name
            if project_name not in project_data:
                project_data[project_name] = 0
            project_data[project_name] += entry.duration
            
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
        
        return render_template(
            'projects/time_statistics.html',
            filter_form=filter_form,
            time_entries=time_entries,
            total_duration=total_duration,
            total_formatted=total_formatted,
            billable_duration=billable_duration,
            billable_formatted=billable_formatted,
            billable_percentage=billable_percentage,
            project_data=project_data,
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
    """Batch operations for time entries"""
    try:
        form = BatchTimeEntryForm()
        
        # Populate project choices
        user_projects = Project.query.filter_by(user_id=current_user.id).all()
        form.project_id.choices = [(p.id, p.name) for p in user_projects]
        form.target_project_id.choices = [(p.id, p.name) for p in user_projects]
        
        # Default task choices
        form.task_id.choices = [(0, 'No Task')]
        form.target_task_id.choices = [(0, 'No Task')]
        
        # Get all time entries for the user
        time_entries = TimeEntry.query.join(Project).filter(
            Project.user_id == current_user.id
        ).order_by(TimeEntry.start_time.desc()).all()
        
        if form.validate_on_submit():
            # Get selected entry IDs from form
            selected_ids = request.form.getlist('selected_entries', type=int)
            
            if not selected_ids:
                flash('No time entries selected. Please select at least one entry.', 'warning')
                return redirect(url_for('projects.batch_time_entries'))
            
            # Perform the requested batch action
            action = form.action.data
            
            # Verify that the selected entries belong to the user
            selected_entries = TimeEntry.query.join(Project).filter(
                TimeEntry.id.in_(selected_ids),
                Project.user_id == current_user.id
            ).all()
            
            if len(selected_entries) != len(selected_ids):
                flash('Some selected entries could not be found or do not belong to you.', 'danger')
                return redirect(url_for('projects.batch_time_entries'))
            
            if action == 'delete':
                # Delete the selected entries
                for entry in selected_entries:
                    db.session.delete(entry)
                db.session.commit()
                flash(f'Successfully deleted {len(selected_entries)} time entries.', 'success')
                
            elif action == 'change_project':
                # Move entries to a different project
                target_project_id = form.target_project_id.data
                
                # Verify target project belongs to user
                target_project = Project.query.filter_by(
                    id=target_project_id, 
                    user_id=current_user.id
                ).first()
                
                if not target_project:
                    flash('Invalid target project selection.', 'danger')
                    return redirect(url_for('projects.batch_time_entries'))
                
                # Update project ID for all selected entries
                for entry in selected_entries:
                    entry.project_id = target_project_id
                    # Clear task ID since it may not be valid for the new project
                    entry.task_id = None
                
                db.session.commit()
                flash(f'Successfully moved {len(selected_entries)} time entries to project "{target_project.name}".', 'success')
                
            elif action == 'change_task':
                # Assign entries to a task
                target_task_id = form.target_task_id.data
                
                if target_task_id == 0:
                    # Remove task association
                    for entry in selected_entries:
                        entry.task_id = None
                    db.session.commit()
                    flash(f'Successfully removed task association from {len(selected_entries)} time entries.', 'success')
                else:
                    # Verify task belongs to user
                    target_task = Task.query.join(Project).filter(
                        Task.id == target_task_id,
                        Project.user_id == current_user.id
                    ).first()
                    
                    if not target_task:
                        flash('Invalid target task selection.', 'danger')
                        return redirect(url_for('projects.batch_time_entries'))
                    
                    # Update task ID for all selected entries
                    for entry in selected_entries:
                        # Update project ID to match task's project
                        entry.project_id = target_task.project_id
                        entry.task_id = target_task_id
                    
                    db.session.commit()
                    flash(f'Successfully assigned {len(selected_entries)} time entries to task "{target_task.title}".', 'success')
            
            elif action == 'mark_billable' or action == 'mark_non_billable':
                # Set billable flag
                billable_value = (action == 'mark_billable')
                
                for entry in selected_entries:
                    entry.billable = billable_value
                
                db.session.commit()
                status = 'billable' if billable_value else 'non-billable'
                flash(f'Successfully marked {len(selected_entries)} time entries as {status}.', 'success')
            
            return redirect(url_for('projects.batch_time_entries'))
                
        return render_template(
            'projects/batch_time_entries.html',
            form=form,
            time_entries=time_entries
        )
                
    except Exception as e:
        logger.error(f"Error in batch time entries: {str(e)}")
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