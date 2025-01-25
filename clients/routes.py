from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required, current_user
from app import db
from models import Client
from clients.forms import ClientForm

clients_bp = Blueprint('clients', __name__, url_prefix='/clients')

@clients_bp.route('/')
@login_required
def list_clients():
    clients = Client.query.filter_by(user_id=current_user.id).all()
    return render_template('clients/list.html', clients=clients)

@clients_bp.route('/new', methods=['GET', 'POST'])
@login_required
def create_client():
    form = ClientForm()
    if form.validate_on_submit():
        client = Client(
            name=form.name.data,
            email=form.email.data,
            company=form.company.data,
            address=form.address.data,
            user_id=current_user.id
        )
        db.session.add(client)
        db.session.commit()
        flash('Client added successfully', 'success')
        return redirect(url_for('clients.list_clients'))
    return render_template('clients/create.html', form=form)
