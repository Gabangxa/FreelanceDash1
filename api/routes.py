"""
API routes for SoloDolo.
"""
from flask import jsonify, request, g, current_app
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload
import logging
from datetime import datetime

from api import api_bp, require_api_access
from app import db
from models import Project, Client, Task, TimeEntry, Invoice, InvoiceItem, User

logger = logging.getLogger('api')

# Helper function to standardize API responses
def api_response(data=None, message=None, status="success", code=200, meta=None):
    """
    Standardize API responses format.
    
    Args:
        data: The data to return
        message: Optional message
        status: 'success' or 'error'
        code: HTTP status code
        meta: Optional metadata dictionary (pagination, etc.)
    
    Returns:
        JSON response with standardized format
    """
    response = {
        'status': status,
    }
    
    if data is not None:
        response['data'] = data
    
    if message:
        response['message'] = message
        
    if meta:
        response['meta'] = meta
        
    return jsonify(response), code

# API Status endpoint
@api_bp.route('/status', methods=['GET'])
def api_status():
    """Check API status and version information."""
    return api_response(data={
        'status': 'operational',
        'version': '1.0.0',
        'app_name': 'SoloDolo',
        'timestamp': datetime.utcnow().isoformat()
    })

# Projects endpoints
@api_bp.route('/projects', methods=['GET'])
@login_required
@require_api_access
def get_projects():
    """Get list of user's projects with optional filtering."""
    # Get query parameters for filtering
    status = request.args.get('status')
    client_id = request.args.get('client_id', type=int)
    
    # Start with base query
    query = Project.query.filter_by(user_id=current_user.id)
    
    # Apply filters if provided
    if status:
        query = query.filter_by(status=status)
    if client_id:
        query = query.filter_by(client_id=client_id)
    
    # Execute query
    projects = query.order_by(Project.created_at.desc()).all()
    
    # Prepare response data
    project_data = [{
        'id': project.id,
        'name': project.name,
        'description': project.description,
        'start_date': project.start_date.isoformat() if project.start_date else None,
        'end_date': project.end_date.isoformat() if project.end_date else None,
        'status': project.status,
        'client_id': project.client_id,
        'client_name': project.client.name
    } for project in projects]
    
    return api_response(data=project_data)

@api_bp.route('/projects/<int:project_id>', methods=['GET'])
@login_required
@require_api_access
def get_project(project_id):
    """Get detailed information about a specific project."""
    project = Project.query.filter_by(id=project_id, user_id=current_user.id).first()
    
    if not project:
        return api_response(message="Project not found", status="error", code=404)
    
    # Count project metrics
    task_count = Task.query.filter_by(project_id=project.id).count()
    time_entries = TimeEntry.query.filter_by(project_id=project.id).all()
    
    # Calculate total hours
    total_minutes = sum(entry.duration or 0 for entry in time_entries if entry.duration)
    total_hours = round(total_minutes / 60, 2)
    
    # Calculate billable hours
    billable_minutes = sum(entry.duration or 0 for entry in time_entries if entry.duration and entry.billable)
    billable_hours = round(billable_minutes / 60, 2)
    
    # Prepare detailed project data
    project_data = {
        'id': project.id,
        'name': project.name,
        'description': project.description,
        'start_date': project.start_date.isoformat() if project.start_date else None,
        'end_date': project.end_date.isoformat() if project.end_date else None,
        'status': project.status,
        'client_id': project.client_id,
        'client_name': project.client.name,
        'created_at': project.created_at.isoformat(),
        'metrics': {
            'task_count': task_count,
            'total_hours': total_hours,
            'billable_hours': billable_hours,
            'time_entry_count': len(time_entries)
        }
    }
    
    return api_response(data=project_data)

# Clients endpoints
@api_bp.route('/clients', methods=['GET'])
@login_required
@require_api_access
def get_clients():
    """Get list of user's clients."""
    clients = Client.query.filter_by(user_id=current_user.id).order_by(Client.name).all()
    
    client_data = [{
        'id': client.id,
        'name': client.name,
        'email': client.email,
        'company': client.company,
        'project_count': len(client.projects)
    } for client in clients]
    
    return api_response(data=client_data)

# Time entries endpoints
@api_bp.route('/time-entries', methods=['GET'])
@login_required
@require_api_access
def get_time_entries():
    """Get time entries with filtering and pagination."""
    # Get query parameters
    project_id = request.args.get('project_id', type=int)
    billable = request.args.get('billable', type=lambda v: v.lower() == 'true')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    # Parse dates if provided
    try:
        if start_date:
            start_date = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        if end_date:
            end_date = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
    except ValueError:
        return api_response(message="Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SS)", 
                          status="error", code=400)
    
    # Start with base query
    query = TimeEntry.query.filter(
        TimeEntry.project.has(user_id=current_user.id)
    )
    
    # Apply filters
    if project_id:
        query = query.filter_by(project_id=project_id)
    if billable is not None:
        query = query.filter_by(billable=billable)
    if start_date:
        query = query.filter(TimeEntry.start_time >= start_date)
    if end_date:
        query = query.filter(TimeEntry.start_time <= end_date)
    
    # Pagination
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)  # Limit max items
    
    paginated = query.order_by(TimeEntry.start_time.desc()).paginate(page=page, per_page=per_page)
    
    # Prepare time entry data
    entries_data = [{
        'id': entry.id,
        'start_time': entry.start_time.isoformat(),
        'end_time': entry.end_time.isoformat() if entry.end_time else None,
        'duration': entry.duration,
        'duration_formatted': f"{entry.duration // 60}h {entry.duration % 60}m" if entry.duration else None,
        'description': entry.description,
        'project_id': entry.project_id,
        'project_name': entry.project.name,
        'task_id': entry.task_id,
        'task_name': entry.task.title if entry.task else None,
        'billable': entry.billable
    } for entry in paginated.items]
    
    # Metadata for pagination
    meta = {
        'page': paginated.page,
        'per_page': paginated.per_page,
        'total': paginated.total,
        'pages': paginated.pages,
        'has_next': paginated.has_next,
        'has_prev': paginated.has_prev
    }
    
    return api_response(data=entries_data, meta=meta)

# Invoices endpoints
@api_bp.route('/invoices', methods=['GET'])
@login_required
@require_api_access
def get_invoices():
    """Get list of user's invoices with optional filtering."""
    # Get query parameters
    status = request.args.get('status')
    client_id = request.args.get('client_id', type=int)
    project_id = request.args.get('project_id', type=int)
    
    # Build query
    query = Invoice.query.join(Project).filter(Project.user_id == current_user.id)
    
    # Apply filters
    if status:
        query = query.filter(Invoice.status == status)
    if client_id:
        query = query.filter(Invoice.client_id == client_id)
    if project_id:
        query = query.filter(Invoice.project_id == project_id)
    
    # Execute query
    invoices = query.order_by(Invoice.created_at.desc()).all()
    
    # Format response
    invoice_data = [{
        'id': invoice.id,
        'invoice_number': invoice.invoice_number,
        'amount': invoice.amount,
        'currency': invoice.currency,
        'status': invoice.status,
        'due_date': invoice.due_date.isoformat() if invoice.due_date else None,
        'client_name': invoice.client.name,
        'project_name': invoice.project.name,
        'created_at': invoice.created_at.isoformat()
    } for invoice in invoices]
    
    return api_response(data=invoice_data)

# User account and profile endpoints
@api_bp.route('/profile', methods=['GET'])
@login_required
@require_api_access
def get_user_profile():
    """Get current user's profile information."""
    user = current_user
    
    # Get statistics
    projects_count = Project.query.filter_by(user_id=user.id).count()
    clients_count = Client.query.filter_by(user_id=user.id).count()
    invoices_count = Invoice.query.join(Project).filter(Project.user_id == user.id).count()
    
    # Get user settings
    settings = user.get_or_create_settings()
    
    profile_data = {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'created_at': user.created_at.isoformat(),
        'stats': {
            'projects_count': projects_count,
            'clients_count': clients_count,
            'invoices_count': invoices_count
        },
        'company': {
            'name': settings.company_name,
            'email': settings.company_email,
            'website': settings.company_website,
            'has_logo': settings.invoice_logo is not None
        }
    }
    
    return api_response(data=profile_data)