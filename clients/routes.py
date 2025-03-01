from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from app import db, logger
from models import Client, Project
from clients.forms import ClientForm
from errors import handle_db_errors, UserFriendlyError

clients_bp = Blueprint('clients', __name__, url_prefix='/clients')

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
    form = ClientForm()

    try:
        if form.validate_on_submit():
            try:
                client = Client(
                    name=form.name.data.strip(),
                    email=form.email.data.lower().strip() if form.email.data else None,
                    company=form.company.data.strip() if form.company.data else None,
                    address=form.address.data.strip() if form.address.data else None,
                    user_id=current_user.id
                )
                db.session.add(client)
                db.session.commit()
                logger.info(f"New client created by user {current_user.id}: {client.name}")
                flash('Client added successfully', 'success')
                return redirect(url_for('clients.list_clients'))
            except SQLAlchemyError as e:
                db.session.rollback()
                logger.error(f"Database error creating client: {str(e)}")
                flash('Error creating client. Please try again.', 'danger')

        return render_template('clients/create.html', form=form)
    except Exception as e:
        logger.error(f"Unexpected error in create_client: {str(e)}")
        flash('An unexpected error occurred. Please try again.', 'danger')
        return redirect(url_for('clients.list_clients'))

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
                client.name = form.name.data.strip()
                client.email = form.email.data.lower().strip() if form.email.data else None
                client.company = form.company.data.strip() if form.company.data else None
                client.address = form.address.data.strip() if form.address.data else None

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