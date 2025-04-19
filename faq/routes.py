from flask import Blueprint, render_template
from flask_login import current_user

faq_bp = Blueprint('faq', __name__, url_prefix='/faq')

@faq_bp.route('/')
def index():
    """Display the main FAQ page with categorized questions and answers."""
    # FAQ categories with questions and answers
    faq_categories = [
        {
            'title': 'Account Management',
            'questions': [
                {
                    'question': 'How do I reset my password?',
                    'answer': 'Click on the "Forgot Password" link on the login page. You will receive an email with instructions to reset your password.'
                },
                {
                    'question': 'How can I update my profile information?',
                    'answer': 'Go to your account settings page by clicking on your username in the top right corner and select "Profile". From there, you can update your information.'
                },
                {
                    'question': 'Is my data secure?',
                    'answer': 'Yes, we use industry-standard encryption and security measures to protect your data. Your password is securely hashed and we never store it in plain text.'
                }
            ]
        },
        {
            'title': 'Clients',
            'questions': [
                {
                    'question': 'How do I add a new client?',
                    'answer': 'Navigate to the Clients section and click the "Add Client" button. Fill in the required information and save. You can optionally create a project for this client during the client creation process.'
                },
                {
                    'question': 'How do I view all projects for a specific client?',
                    'answer': 'Go to the Clients section and click on the client\'s name. This will take you to the client detail page where you can see all projects associated with that client.'
                },
                {
                    'question': 'Can I import client data from other platforms?',
                    'answer': 'Currently, we don\'t support direct imports. However, you can manually add clients to the system.'
                },
                {
                    'question': 'How do I delete a client?',
                    'answer': 'Go to the client\'s detail page and click the "Delete" button. Note that this will also delete all projects and invoices associated with the client.'
                },
                {
                    'question': 'What is the relationship between clients and projects?',
                    'answer': 'Each project must be associated with a client. You can navigate from clients to their projects using the hierarchical navigation system. This makes it easy to see which projects belong to which clients.'
                }
            ]
        },
        {
            'title': 'Projects',
            'questions': [
                {
                    'question': 'How do I track time for a project?',
                    'answer': 'Navigate to the project detail page and use the "Add Time Entry" button to record time. You can specify the start and end time, or use the timer feature.'
                },
                {
                    'question': 'Can I set up recurring tasks?',
                    'answer': 'Yes, when creating or editing a task, you can set it to repeat at specific intervals (daily, weekly, monthly).'
                },
                {
                    'question': 'How do I mark a project as complete?',
                    'answer': 'On the project detail page, click the "Complete Project" button or change the project status to "Completed" in the edit form.'
                },
                {
                    'question': 'How do I create a task directly from a project?',
                    'answer': 'While viewing a project, click the "Add Task" button. This will open the task creation form with the current project already selected. The form will display project-specific information to help maintain context.'
                },
                {
                    'question': 'What happens if I change the project when creating a task?',
                    'answer': 'If you start creating a task from a specific project but then select a different project in the dropdown, the form will dynamically update to show information about the newly selected project. All project-specific banners and context will update automatically.'
                }
            ]
        },
        {
            'title': 'Invoices',
            'questions': [
                {
                    'question': 'How are invoice numbers generated?',
                    'answer': 'Invoice numbers are automatically generated with a unique sequential identifier. You can customize the format in your account settings.'
                },
                {
                    'question': 'Can I customize invoice templates?',
                    'answer': 'Yes, you can customize the look and feel of your invoices from the Settings page. You can add your logo, change colors, and modify the layout.'
                },
                {
                    'question': 'How do I send invoices to clients?',
                    'answer': 'After creating an invoice, click the "Send" button to email it directly to your client, or download the PDF to send it manually.'
                },
                {
                    'question': 'Can I accept online payments?',
                    'answer': 'Not yet, but we\'re working on integrating payment gateways for direct online payments. This feature will be available in a future update.'
                }
            ]
        },
        {
            'title': 'Technical Support',
            'questions': [
                {
                    'question': 'What should I do if I encounter an error?',
                    'answer': 'Try refreshing the page first. If the error persists, please contact our support team with a screenshot and description of what you were doing when the error occurred.'
                },
                {
                    'question': 'Is there a mobile app available?',
                    'answer': 'We don\'t have a dedicated mobile app yet, but our website is fully responsive and works well on mobile devices.'
                },
                {
                    'question': 'Can I export my data?',
                    'answer': 'Yes, you can export client, project, and invoice data in CSV or Excel format from the respective sections.'
                }
            ]
        }
    ]
    
    return render_template('faq/index.html', faq_categories=faq_categories, title='Frequently Asked Questions')