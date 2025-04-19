# Freelancer Suite

A comprehensive SaaS platform designed to streamline freelance project management and time tracking workflows. This application provides end-to-end solutions for freelancers to manage professional tasks, monitor project progress, and optimize their productivity.

## Features

### User Management
- Secure registration and authentication system
- Password reset functionality with email verification
- User profile management

### Client Management
- Create and manage client profiles
- Store client contact information and addresses
- Organize projects by client
- Simplified client creation with optional immediate project assignment
- Clear client-to-project relationship visualization

### Project Management
- Create detailed project profiles
- Set project timeframes with start and end dates
- Track project status (active, completed, on-hold)
- Link projects to specific clients
- Hierarchical client-to-project navigation

### Task Management
- Create tasks within projects with context-aware task creation
- Set due dates and track completion status
- Organize work into manageable units
- Seamless workflow from projects to related tasks

### Time Tracking
- Record time entries for specific tasks and projects
- Calculate duration automatically
- Provide insights into time allocation

### Invoice Management
- Generate professional invoices
- Support for multiple currencies
- Create detailed line items
- Track invoice status (draft, pending, paid, cancelled)
- Customizable invoice templates

### Customization
- Company profile settings
- Customizable invoice templates with different designs
- Company logo upload capability
- Color scheme customization for invoices
- Custom footer text for invoices

### Knowledge Base
- FAQ section for application guidance
- Informative content for new users

## Technical Stack

- **Backend**: Python Flask with enhanced error handling
- **Database**: PostgreSQL with data integrity constraints
- **Frontend**: HTML/CSS/JavaScript with Bootstrap
- **Email Integration**: Flask-Mail for automated communications
- **Authentication**: Flask-Login for user management
- **Forms**: Flask-WTF with comprehensive validation
- **PDF Generation**: ReportLab for invoice PDFs
- **Server**: Gunicorn production server configuration

## Environment Setup

### Required Environment Variables

The application requires the following environment variables to be set:

- `DATABASE_URL`: PostgreSQL database connection string
- `FLASK_SECRET_KEY`: Secret key for secure sessions
- `MAIL_SERVER`: SMTP server address (e.g., smtp.gmail.com)
- `MAIL_PORT`: SMTP server port (typically 587 for TLS)
- `MAIL_USERNAME`: Email address for sending emails
- `MAIL_PASSWORD`: Email password or app password
- `MAIL_USE_TLS`: Set to True for TLS encryption
- `MAIL_DEFAULT_SENDER`: Default sender email address
- `APP_URL`: Application URL for email links

### Setting Up the Database

The application automatically creates all necessary database tables on startup using SQLAlchemy's migration tools.

## Installation and Running

1. Clone the repository
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Set up environment variables (see above)
4. Initialize the database:
   ```
   flask db upgrade
   ```
5. Start the development server:
   ```
   python main.py
   ```
6. For production deployment, use Gunicorn:
   ```
   gunicorn --bind 0.0.0.0:5000 --workers 4 --timeout 120 main:app
   ```

## Project Structure

```
├── auth/               # Authentication related functionality
│   ├── forms.py        # Login, registration forms
│   └── routes.py       # Auth routes
├── clients/            # Client management
│   ├── forms.py
│   └── routes.py
├── faq/                # FAQ section
│   └── routes.py
├── invoices/           # Invoice management
│   ├── forms.py
│   └── routes.py
├── logs/               # Application logs
├── projects/           # Project management
│   ├── forms.py
│   └── routes.py
├── settings/           # User settings
│   ├── forms.py
│   └── routes.py
├── static/             # Static assets
│   ├── css/
│   └── js/
├── templates/          # HTML templates
├── app.py              # Application initialization
├── errors.py           # Error handling
├── mail.py             # Email functionality
├── main.py             # Application entry point
├── models.py           # Database models
└── performance.py      # Performance monitoring
```

## Security Features

- Password hashing using Werkzeug's security functions
- CSRF protection on all forms
- Secure session management
- Rate limiting for sensitive operations
- User input validation
- Parameterized database queries to prevent SQL injection

## Logging

The application includes comprehensive logging with different log levels:

- Error logs for application errors
- Security logs for login attempts
- Performance logs for slow database queries and requests
- Component-specific logs (auth, clients, projects, invoices)

## License

This project is licensed under the MIT License - see the LICENSE file for details.