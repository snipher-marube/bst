# AnalyticsMeta

A powerful business intelligence and analytics platform built with Django, enabling teams to create custom data tables, build interactive dashboards, and gain insights from their data.

## 🚀 Features

### Core Functionality
- **Workspace Management**: Multi-tenant architecture with tiered plans (Free, Starter, Professional, Enterprise)
- **Data Tables**: Create flexible, schema-based tables similar to Airtable with support for multiple field types
- **Interactive Dashboards**: Build custom dashboards with various widget types
- **Team Collaboration**: Role-based access control (Owner, Admin, Editor, Viewer)
- **Real-time Analytics**: Track KPIs and visualize data with charts
- **Professional Data Import**: Step-by-step import wizard for CSV and Excel files with auto-schema detection and column mapping.
- **Data Export**: Export data in multiple formats for external use.

### Field Types Supported
- Text, Number, Date, DateTime
- Boolean (Yes/No)
- Email, URL, Phone
- Currency, Percentage

### Key Applications
- 📊 **Dashboards**: Create and customize analytical dashboards
- 📈 **Charts**: Visualize data with various chart types
- 📋 **Categories**: Organize data into categories
- 🔢 **KPIs**: Track key performance indicators
- 📥 **Imports**: Import data from external sources
- 📤 **Exports**: Export data in multiple formats
- 🔗 **Integrations**: Connect with third-party services
- 📧 **Newsletter**: Manage email campaigns and newsletters
- 🔔 **Notifications**: Real-time alerts and notifications
- 👥 **Teams**: Collaborative workspace management
- 💳 **Subscriptions**: Manage user subscriptions and billing

## 🛠️ Tech Stack

### Backend
- **Django 6.0.2**: Web framework
- **Django REST Framework 3.16**: API development
- **PostgreSQL**: Primary database (via psycopg 3.3)
- **Celery 5.6**: Asynchronous task processing
- **Redis 7.2**: Caching and task queue
- **Django Allauth 65.14**: Authentication with OAuth support

### Frontend
- **Tailwind CSS 4.1**: Utility-first CSS framework
- **Alpine.js/HTMX**: (Inferred from Django templates)

### Authentication & Security
- Google OAuth2
- LinkedIn OAuth2
- Django rate limiting
- JWT authentication
- Cryptography for secure data handling

### Additional Tools
- **Pandas**: Data manipulation and analysis
- **Pillow**: Image processing
- **WhiteNoise**: Static file serving

## 📋 Prerequisites

- Python 3.14+
- PostgreSQL 15+
- Redis 6+
- Node.js 18+ (for Tailwind CSS)

## ⚙️ Installation

### 1. Clone the repository
```bash
git clone https://github.com/snipher-marube/bst
cd bst
```

### 2. Install Python dependencies
Using uv (recommended):
```bash
uv sync
```

Or using pip:
```bash
pip install -e .
```

### 3. Install Node.js dependencies
```bash
npm install
```

### 4. Environment Configuration
Create a `.env` file in the project root:
```env
SECRET_KEY=your-secret-key-here
DEBUG=True
DATABASE_URL=postgresql://user:password@localhost:5432/analyticsmeta
REDIS_URL=redis://localhost:6379/0

# Email Configuration
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_HOST_USER=your-email@gmail.com
EMAIL_HOST_PASSWORD=your-email-password

# OAuth Configuration
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
LINKEDIN_CLIENT_ID=your-linkedin-client-id
LINKEDIN_CLIENT_SECRET=your-linkedin-client-secret

# Celery
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0
```

### 5. Database Setup
```bash
python manage.py migrate
python manage.py createsuperuser
```

### 6. Collect Static Files
```bash
python manage.py collectstatic --noinput
```

## 🚀 Running the Application

### Development Mode

Start all services:

1. **Django Development Server**:
```bash
python manage.py runserver
```

2. **Celery Worker** (in a separate terminal):
```bash
celery -A config worker -l info
```

3. **Celery Beat** (for scheduled tasks, in a separate terminal):
```bash
celery -A config beat -l info
```

4. **Tailwind CSS** (in watch mode, in a separate terminal):
```bash
npm run dev
```

Visit `http://localhost:8000` in your browser.

### Production Deployment

1. Set `DEBUG=False` in your environment
2. Configure your production database
3. Set up a production-ready web server (Gunicorn, uWSGI)
4. Use a reverse proxy (Nginx, Caddy)
5. Set up SSL/TLS certificates
6. Configure Celery with a process manager (Supervisor, systemd)

Example production server start:
```bash
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 4
```

## 📁 Project Structure

```
analyticsmeta/
├── apps/                       # Django applications
│   ├── categories/            # Data categorization
│   ├── charts/                # Chart visualization
│   ├── core/                  # Core functionality
│   ├── dashboards/            # Dashboard management
│   ├── exports/               # Data export functionality
│   ├── fields/                # Custom field types
│   ├── formulas/              # Formula calculations
│   ├── imports/               # Data import functionality
│   ├── integrations/          # Third-party integrations
│   ├── kpis/                  # KPI tracking
│   ├── newsletter/            # Newsletter management
│   ├── notifications/         # Notification system
│   ├── records/               # Data records
│   ├── subscriptions/         # Subscription management
│   ├── teams/                 # Team collaboration
│   ├── templates_app/         # Template management
│   ├── users/                 # User management
│   └── workspaces/            # Workspace management
├── config/                     # Django configuration
│   ├── settings/              # Environment-specific settings
│   │   ├── base.py
│   │   ├── development.py
│   │   └── production.py
│   ├── celery.py              # Celery configuration
│   ├── urls.py                # URL routing
│   └── wsgi.py                # WSGI configuration
├── static/                     # Static files
├── templates/                  # Django templates
├── manage.py                   # Django management script
├── pyproject.toml             # Python dependencies
└── package.json               # Node.js dependencies
```

## 🏗️ Architecture

### Data Model Hierarchy
1. **Workspace**: Top-level organization unit, supports multiple tiers
2. **DataTable**: Schema-based tables within workspaces
3. **Record**: Individual data entries in tables
4. **Dashboard**: Visual representation of data
5. **Widget**: Individual visualization components

### Access Control
- **Owner**: Full workspace control
- **Admin**: Workspace settings management
- **Editor**: Create and edit dashboards
- **Viewer**: Read-only access

### Tier Limits
- **Free**: 5 tables, 1000 records per table, 1 team member
- **Starter**: Increased limits
- **Professional**: Higher limits, advanced features
- **Enterprise**: Unlimited, custom solutions

## 🔐 Security Features

- CSRF protection
- SQL injection prevention via Django ORM
- XSS protection
- Rate limiting on API endpoints
- Secure password hashing
- OAuth2 integration
- JWT token authentication
- Audit logging for all actions

## 📊 API Documentation

RESTful API endpoints are available for:
- Workspace management
- Table CRUD operations
- Record management
- Dashboard configuration
- User authentication

API endpoints are prefixed with `/api/v1/` and require authentication via JWT tokens or session authentication.

## 🧪 Testing

Run tests with:
```bash
python manage.py test
```

Run tests with coverage:
```bash
coverage run manage.py test
coverage report
```

## 📝 Development

### Code Style
- Follow PEP 8 guidelines
- Use Django best practices
- Write descriptive commit messages
- Add docstrings to functions and classes

### Database Migrations
After model changes:
```bash
python manage.py makemigrations
python manage.py migrate
```

### Creating a New App
```bash
python manage.py startapp app_name apps/app_name
```

## 🐛 Troubleshooting

### Common Issues

**Database connection errors**:
- Verify PostgreSQL is running
- Check DATABASE_URL in .env file

**Celery tasks not executing**:
- Ensure Redis is running
- Check Celery worker logs

**Static files not loading**:
- Run `python manage.py collectstatic`
- Check STATIC_ROOT configuration

## 📜 License

[Add your license information here]

## 👥 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📧 Contact

sniphermarube@gmail.com

## 🙏 Acknowledgments

- Django Software Foundation
- All open-source contributors

---

**Built with ❤️ using Django and modern web technologies**
