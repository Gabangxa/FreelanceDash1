# Freelancer Suite - Replit Configuration

## Overview

Freelancer Suite is a comprehensive SaaS platform built with Flask that provides end-to-end project management and business solutions for freelancers. The application offers client management, project tracking, time management, invoicing, and subscription services through a clean, responsive web interface.

## System Architecture

### Backend Architecture
- **Framework**: Flask with SQLAlchemy ORM
- **Database**: SQLite (development) with support for PostgreSQL (production)
- **Authentication**: Flask-Login with session management
- **Email Services**: Flask-Mail with SMTP configuration
- **Error Handling**: Centralized error handling with logging and user-friendly messages
- **Performance Monitoring**: Custom performance monitoring with slow query detection

### Frontend Architecture
- **Templates**: Jinja2 templating engine
- **CSS Framework**: Bootstrap 5 with custom styling
- **JavaScript**: Vanilla JavaScript with Bootstrap components
- **Asset Management**: Custom asset bundling and minification system
- **Responsive Design**: Mobile-first approach with dark/light mode support

### Security Features
- Password hashing with Werkzeug
- CSRF protection via Flask-WTF
- Input validation and sanitization
- Secure session management
- Password reset functionality with time-limited tokens

## Key Components

### Core Modules
1. **User Management** (`auth/`): Registration, login, password reset
2. **Client Management** (`clients/`): Client profiles, contact information, project relationships
3. **Project Management** (`projects/`): Project creation, task management, time tracking
4. **Invoice System** (`invoices/`): Professional invoice generation with PDF support
5. **Settings** (`settings/`): User preferences, company information, customization
6. **Subscription System** (`polar/`): Polar.sh integration for payment processing

### Database Models
- **User**: Core user authentication and profile data
- **Client**: Client information with one-to-many project relationships
- **Project**: Project details linked to clients with task hierarchies
- **Task**: Individual work items within projects
- **TimeEntry**: Time tracking records for tasks and projects
- **Invoice/InvoiceItem**: Billing system with line items and multiple currencies
- **Subscription**: Polar.sh subscription management
- **UserSettings**: Customizable user preferences and company branding

### API Structure
- RESTful API endpoints under `/api/v1/`
- Standardized JSON response format
- Authentication-protected endpoints
- Performance monitoring and request logging

## Data Flow

### User Workflow
1. User registration/authentication through Flask-Login
2. Client creation with optional immediate project assignment
3. Project creation with hierarchical task management
4. Time tracking against specific tasks/projects
5. Invoice generation with automated calculations
6. PDF export and client communication

### Database Relationships
- Users → Clients (one-to-many)
- Clients → Projects (one-to-many)
- Projects → Tasks (one-to-many)
- Tasks → TimeEntries (one-to-many)
- Clients → Invoices (one-to-many)
- Users → Subscriptions (one-to-one)

## External Dependencies

### Required Services
- **SMTP Server**: Email delivery for password resets and notifications
- **Polar.sh API**: Subscription management and payment processing
- **File Storage**: Local storage for user uploads (logos, attachments)

### Third-Party Integrations
- **ReportLab**: PDF generation for invoices
- **Pillow**: Image processing for user uploads
- **Bootstrap**: Frontend framework via CDN
- **Font Awesome**: Icon library via CDN

### Environment Variables
- `FLASK_SECRET_KEY`: Application secret key
- `DATABASE_URL`: Database connection string
- `MAIL_SERVER`, `MAIL_USERNAME`, `MAIL_PASSWORD`: Email configuration
- `POLAR_API_KEY`: Subscription service API key

## Deployment Strategy

### Production Configuration
- Debug mode disabled in production
- Secure session handling with proxy support
- Rotating file logs with size limits
- Database connection pooling
- Asset minification and caching

### Development Setup
- SQLite database for local development
- Debug mode enabled with detailed error pages
- Console logging for development feedback
- Hot reload for template and static file changes

### Error Handling
- Centralized error logging with rotation
- User-friendly error pages (400, 403, 404, 500)
- Database error handling with rollback
- Performance monitoring with slow query detection

## Changelog

- December 07, 2025. Added project completion feature and deadline alert system
  - Projects can now be marked as "completed" or reopened with a single click
  - Configurable deadline alerts (7 days, 3 days, 1 day, or custom interval)
  - Color-coded urgency indicators on dashboard (red/orange/yellow)
  - Deadline alert settings accessible from user dropdown menu
- July 03, 2025. Initial setup

## User Preferences

Preferred communication style: Simple, everyday language.