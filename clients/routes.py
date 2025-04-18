from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from datetime import datetime
from app import db, logger
from models import Client, Project
from clients.forms import ClientForm
from errors import handle_db_errors, UserFriendlyError

clients_bp = Blueprint('clients', __name__, url_prefix='/clients')

@clients_bp.route('/<int:id>')
@login_required
@handle_db_errors
def view_client(id):
    try:
        # Secure query ensuring client belongs to current user with eager loading of projects
        client = Client.query.filter_by(id=id, user_id=current_user.id).first_or_404()
        
        # Get all projects for this client
        projects = Project.query.filter_by(client_id=id).all()
        
        return render_template('clients/detail.html', client=client, projects=projects)
    except SQLAlchemyError as e:
        logger.error(f"Error viewing client {id}: {str(e)}")
        flash('Error loading client details. Please try again.', 'danger')
        return redirect(url_for('clients.list_clients'))

@clients_bp.route('/')
@login_required
@handle_db_errors
def list_clients():
    try:
        # Optimized query with eager loading
        clients = Client.query.filter_by(user_id=current_user.id).all()
        return render_template('clients/list.html', clients=clients)
    except SQLAlchemyError as e:
        logger.error(f"Database error in list_clients: {str(e)}")
        flash('Error loading clients. Please try again.', 'danger')
        return render_template('clients/list.html', clients=[])

@clients_bp.route('/new', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def create_client():
    """Create a new client with optional projects."""
    form = ClientForm()
    
    # On GET: just display the form
    if request.method == 'GET':
        return render_template('clients/create.html', form=form)
    
    # On POST: process the form
    if form.validate_on_submit():
        try:
            # Check client limit based on subscription tier
            client_count = Client.query.filter_by(user_id=current_user.id).count()
            clients_limit = current_user.has_subscription_feature('clients_limit')
            
            # Ensure clients_limit is an integer
            try:
                clients_limit = int(clients_limit) if clients_limit is not None else 3
            except (ValueError, TypeError):
                clients_limit = 3  # Default if there's an issue
            
            # If client limit is reached, show a subscription upgrade message
            if client_count >= clients_limit:
                flash(f'You have reached the maximum number of clients ({clients_limit}) for your current plan. '
                      f'Please upgrade your subscription to add more clients.', 'warning')
                return redirect(url_for('polar.index'))
            
            # Create new client
            client = Client(
                name=form.name.data.strip(),
                email=form.email.data.lower().strip() if form.email.data else None,
                company=form.company.data.strip() if form.company.data else None,
                address=form.address.data.strip() if form.address.data else None,
                user_id=current_user.id
            )
            
            db.session.add(client)
            db.session.flush()  # Get the client ID without committing
            
            # Process projects
            projects_created = 0
            for project_entry in form.projects.entries:
                # Skip if this project should not be included or has no name
                if not project_entry.form.include_project.data or not project_entry.form.name.data:
                    continue
                
                # Create the project
                project = Project(
                    name=project_entry.form.name.data.strip(),
                    description=project_entry.form.description.data.strip() if project_entry.form.description.data else None,
                    start_date=project_entry.form.start_date.data,
                    end_date=project_entry.form.end_date.data,
                    client_id=client.id,
                    user_id=current_user.id,
                    status='active'
                )
                
                db.session.add(project)
                projects_created += 1
            
            # Commit all changes
            db.session.commit()
            
            # Log the creation
            logger.info(f"New client created by user {current_user.id}: {client.name} with {projects_created} projects")
            
            # Show success message
            if projects_created > 0:
                flash(f'Client added successfully with {projects_created} project(s)', 'success')
            else:
                flash('Client added successfully', 'success')
            
            # Redirect to the new client detail page
            return redirect(url_for('clients.list_clients'))
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error creating client: {str(e)}")
            flash('An error occurred while creating the client. Please try again.', 'danger')
    
    # If form validation failed or we're just displaying the form
    return render_template('clients/create.html', form=form)

@clients_bp.route('/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@handle_db_errors
def edit_client(id):
    try:
        # Secure query ensuring client belongs to current user
        client = Client.query.filter_by(id=id, user_id=current_user.id).first_or_404()

        form = ClientForm(obj=client)
        if form.validate_on_submit():
            try:
                # Handle form data safely with proper type checking
                client.name = form.name.data.strip() if hasattr(form.name, 'data') and form.name.data else ""
                client.email = form.email.data.lower().strip() if hasattr(form.email, 'data') and form.email.data else None
                client.company = form.company.data.strip() if hasattr(form.company, 'data') and form.company.data else None
                client.address = form.address.data.strip() if hasattr(form.address, 'data') and form.address.data else None

                db.session.commit()
                logger.info(f"Client {id} updated by user {current_user.id}")
                flash('Client updated successfully', 'success')
                return redirect(url_for('clients.list_clients'))
            except SQLAlchemyError as e:
                db.session.rollback()
                logger.error(f"Database error updating client {id}: {str(e)}")
                flash('Error updating client. Please try again.', 'danger')

        return render_template('clients/create.html', form=form, client=client)
    except SQLAlchemyError as e:
        logger.error(f"Error loading client {id}: {str(e)}")
        flash('Error loading client details. Please try again.', 'danger')
        return redirect(url_for('clients.list_clients'))

@clients_bp.route('/<int:id>/delete', methods=['POST'])
@login_required
@handle_db_errors
def delete_client(id):
    try:
        # Secure query ensuring client belongs to current user
        client = Client.query.filter_by(id=id, user_id=current_user.id).first_or_404()

        # Check if client has associated projects
        projects_count = Project.query.filter_by(client_id=id).count()
        if projects_count > 0:
            flash(f'Cannot delete client with {projects_count} associated projects. Remove projects first.', 'warning')
            return redirect(url_for('clients.list_clients'))

        client_name = client.name  # Store for logging

        try:
            db.session.delete(client)
            db.session.commit()
            logger.info(f"Client {id} ({client_name}) deleted by user {current_user.id}")
            flash('Client deleted successfully', 'success')
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Database error deleting client {id}: {str(e)}")
            flash('Error deleting client. Please try again.', 'danger')

        return redirect(url_for('clients.list_clients'))
    except Exception as e:
        logger.error(f"Unexpected error in delete_client {id}: {str(e)}")
        flash('An unexpected error occurred. Please try again.', 'danger')
        return redirect(url_for('clients.list_clients'))