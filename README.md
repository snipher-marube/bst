# AnalyticsMeta

A powerful business intelligence and analytics platform built with Django, enabling teams to create custom data tables, build interactive dashboards, and gain insights from their data.

## 📋 Table of Contents
- [Features](#-features)
- [Tech Stack](#-tech-stack)
- [Docker Setup (Recommended)](#-docker-setup-recommended)
- [Manual Installation](#-manual-installation)
- [Project Structure](#-project-structure)
- [Architecture](#-architecture)
- [Security Features](#-security-features)
- [API Documentation](#-api-documentation)
- [Testing](#-testing)
- [Troubleshooting](#-troubleshooting)
- [Contributing](#-contributing)

## 🚀 Features

### Core Functionality
- **Workspace Management**: Multi-tenant architecture with tiered plans (Free, Starter, Professional, Enterprise)
- **Data Tables**: Create flexible, schema-based tables similar to Airtable with support for multiple field types
- **Interactive Dashboards**: Build custom dashboards with various widget types
- **Team Collaboration**: Role-based access control (Owner, Admin, Editor, Viewer)
- **Real-time Analytics**: Track KPIs and visualize data with charts
- **Professional Data Import**: Step-by-step import wizard for CSV and Excel files with auto-schema detection and column mapping
- **Data Export**: Export data in multiple formats for external use

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
- **PostgreSQL 18**: Primary database (via psycopg 3.3)
- **Celery 5.6**: Asynchronous task processing
- **Redis 7**: Caching and task queue
- **Django Allauth 65.14**: Authentication with OAuth support

### Frontend
- **Tailwind CSS 4.1**: Utility-first CSS framework
- **Alpine.js/HTMX**: Interactive UI components

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
- **Gunicorn**: Production WSGI server

## 🐳 Docker Setup (Recommended)

Docker is the recommended way to run this project as it handles all dependencies and services (PostgreSQL, Redis, Celery) automatically.

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (version 24.0 or higher)
- [Docker Compose](https://docs.docker.com/compose/install/) (version 2.20 or higher)
- Git

### Quick Start with Docker

#### 1. Clone the Repository
```bash
git clone https://github.com/snipher-marube/bst
cd bst
```

#### 2. Configure Environment Variables

Create a `.env` file in the project root (copy from `.env.example` if available):

```env
# Django Settings
SECRET_KEY=your-super-secret-key-change-this-in-production
DEBUG=True
DJANGO_SETTINGS_MODULE=config.settings.development

# PostgreSQL Database (Docker will use these)
PG_DATABASE_NAME_DEV=analyticsmeta
PG_DATABASE_USER_DEV=postgres
PG_DATABASE_PASSWORD_DEV=postgres
# IMPORTANT: Use 'postgres' as host when running with Docker (service name)
PG_DATABASE_HOST_DEV=postgres
PG_DATABASE_PORT_DEV=5432

# Redis Configuration
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0

# Email Configuration (for development)
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=your-email@gmail.com
EMAIL_HOST_PASSWORD=your-app-password
SUPPORT_EMAIL=support@example.com

# OAuth Configuration (optional - for production)
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
LINKEDIN_CLIENT_ID=your-linkedin-client-id
LINKEDIN_CLIENT_SECRET=your-linkedin-client-secret

# Site Configuration
SITE_URL=http://localhost:8000
SITE_NAME=AnalyticsMeta
```

**⚠️ Important Notes:**
- Do **NOT** add comments on the same line as environment variables
- Use `postgres` as the database host (Docker service name), not `localhost`
- Keep the `.env` file secure and never commit it to version control

#### 3. Build and Start Containers

```bash
# Build and start all services
docker-compose -f docker-compose.dev.yml up --build

# Or run in detached mode (background)
docker-compose -f docker-compose.dev.yml up -d --build
```

This will start:
- **PostgreSQL 18** on port 5432
- **Redis 7** on port 6380 (mapped to container's 6379)
- **Django Development Server** on port 8000
- **Celery Worker** for background tasks

#### 4. Run Database Migrations

In a new terminal, run:

```bash
# Run migrations
docker-compose -f docker-compose.dev.yml exec web python manage.py migrate

# Create a superuser (admin account)
docker-compose -f docker-compose.dev.yml exec web python manage.py createsuperuser

# Collect static files
docker-compose -f docker-compose.dev.yml exec web python manage.py collectstatic --noinput
```

#### 5. Access the Application

Open your browser and navigate to:
- **Main Application**: http://localhost:8000
- **Admin Panel**: http://localhost:8000/admin (use the superuser credentials)
- **API Endpoints**: http://localhost:8000/api/v1/

### Docker Commands Reference

#### Container Management
```bash
# Start all services
docker-compose -f docker-compose.dev.yml up -d

# Stop all services
docker-compose -f docker-compose.dev.yml down

# Stop and remove volumes (deletes database data)
docker-compose -f docker-compose.dev.yml down -v

# Rebuild after changes
docker-compose -f docker-compose.dev.yml up --build

# View running containers
docker-compose -f docker-compose.dev.yml ps

# View logs
docker-compose -f docker-compose.dev.yml logs -f

# View specific service logs
docker-compose -f docker-compose.dev.yml logs -f web
docker-compose -f docker-compose.dev.yml logs -f postgres
docker-compose -f docker-compose.dev.yml logs -f redis
```

#### Running Management Commands
```bash
# Django shell
docker-compose -f docker-compose.dev.yml exec web python manage.py shell

# Django shell_plus (if django-extensions is installed)
docker-compose -f docker-compose.dev.yml exec web python manage.py shell_plus

# Check migrations status
docker-compose -f docker-compose.dev.yml exec web python manage.py showmigrations

# Create new app
docker-compose -f docker-compose.dev.yml exec web python manage.py startapp app_name

# Run tests
docker-compose -f docker-compose.dev.yml exec web python manage.py test
```

#### Database Operations
```bash
# Access PostgreSQL directly
docker-compose -f docker-compose.dev.yml exec postgres psql -U postgres -d analyticsmeta

# Backup database
docker-compose -f docker-compose.dev.yml exec postgres pg_dump -U postgres analyticsmeta > backup.sql

# Restore database
cat backup.sql | docker-compose -f docker-compose.dev.yml exec -T postgres psql -U postgres analyticsmeta
```

#### Redis Commands
```bash
# Access Redis CLI
docker-compose -f docker-compose.dev.yml exec redis redis-cli

# Monitor Redis keys
docker-compose -f docker-compose.dev.yml exec redis redis-cli MONITOR
```

### Docker Architecture

The project uses a multi-container setup:

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Network                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │   Web App    │  │   Celery     │  │   Celery     │  │
│  │   (Django)   │  │   Worker     │  │   Beat       │  │
│  │   :8000      │  │              │  │              │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │
│         │                 │                 │           │
│         ▼                 ▼                 ▼           │
│  ┌──────────────────────────────────────────────────┐  │
│  │              Redis (Message Broker)               │  │
│  │                    :6379                         │  │
│  └──────────────────────────────────────────────────┘  │
│         │                                               │
│         ▼                                               │
│  ┌──────────────────────────────────────────────────┐  │
│  │           PostgreSQL (Database)                  │  │
│  │                    :5432                         │  │
│  └──────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### Environment-Specific Docker Files

- **docker-compose.dev.yml**: Development setup with hot-reload and mounted volumes
- **Dockerfile.dev**: Development Docker image with uv package manager

### Hot Reloading

The development setup includes hot reloading:
- Django auto-reloads on code changes (mounted volume)
- Static files are served directly from the mounted directory
- No need to rebuild containers during development

## ⚙️ Manual Installation

If you prefer not to use Docker, you can install manually.

### Prerequisites

- Python 3.14+
- PostgreSQL 15+
- Redis 6+
- Node.js 18+ (for Tailwind CSS)

### Installation Steps

#### 1. Clone the repository
```bash
git clone https://github.com/snipher-marube/bst
cd bst
```

#### 2. Create virtual environment
```bash
# Using uv (recommended)
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
uv pip install -e .

# Or using pip
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

#### 3. Install Node.js dependencies
```bash
npm install
```

#### 4. Configure environment
Create `.env` file (see Docker section for example) and update:
- `PG_DATABASE_HOST_DEV=localhost` (when not using Docker)
- Other database credentials

#### 5. Setup database
```bash
# Create PostgreSQL database
createdb analyticsmeta

# Run migrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser
```

#### 6. Run the application

Start all services:

**Terminal 1 - Django Server:**
```bash
python manage.py runserver
```

**Terminal 2 - Celery Worker:**
```bash
celery -A config worker -l info
```

**Terminal 3 - Celery Beat (optional, for scheduled tasks):**
```bash
celery -A config beat -l info
```

**Terminal 4 - Tailwind CSS (optional):**
```bash
npm run dev
```

Visit `http://localhost:8000`

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
│   ├── insights/              # AI-powered insights
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
│   │   ├── base.py           # Base settings
│   │   ├── development.py    # Development settings
│   │   └── production.py     # Production settings
│   ├── __init__.py
│   ├── asgi.py               # ASGI configuration
│   ├── celery.py             # Celery configuration
│   ├── urls.py               # URL routing
│   └── wsgi.py               # WSGI configuration
├── static/                     # Static files (CSS, JS, images)
├── staticfiles/               # Collected static files
├── templates/                  # Django templates
├── .env                        # Environment variables (gitignored)
├── .env.example                # Example environment variables
├── .gitignore                  # Git ignore file
├── docker-compose.dev.yml     # Docker Compose configuration
├── Dockerfile.dev             # Development Dockerfile
├── manage.py                  # Django management script
├── pyproject.toml             # Python dependencies
├── package.json               # Node.js dependencies
└── README.md                  # This file
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
- **Starter**: 20 tables, 10,000 records, 5 team members
- **Professional**: 100 tables, 100,000 records, 20 team members
- **Enterprise**: Unlimited tables/records, custom solutions

## 🔐 Security Features

- CSRF protection
- SQL injection prevention via Django ORM
- XSS protection
- Rate limiting on API endpoints (5 attempts per 5 minutes for login)
- Secure password hashing (PBKDF2 with 600,000 iterations)
- OAuth2 integration with Google and LinkedIn
- JWT token authentication for API
- Audit logging for all actions
- Account enumeration prevention
- Email verification required for new accounts
- Password complexity requirements

## 📊 API Documentation

RESTful API endpoints are available at `/api/v1/`:

### Authentication
- `POST /api/v1/auth/login/` - Login with email/password
- `POST /api/v1/auth/logout/` - Logout
- `POST /api/v1/auth/register/` - Register new user
- `POST /api/v1/auth/password/reset/` - Password reset

### Workspaces
- `GET /api/v1/workspaces/` - List workspaces
- `POST /api/v1/workspaces/` - Create workspace
- `GET /api/v1/workspaces/{id}/` - Get workspace details
- `PUT /api/v1/workspaces/{id}/` - Update workspace
- `DELETE /api/v1/workspaces/{id}/` - Delete workspace

### Tables
- `GET /api/v1/workspaces/{id}/tables/` - List tables
- `POST /api/v1/workspaces/{id}/tables/` - Create table
- `GET /api/v1/tables/{id}/` - Get table details
- `PUT /api/v1/tables/{id}/` - Update table schema
- `DELETE /api/v1/tables/{id}/` - Delete table

### Records
- `GET /api/v1/tables/{id}/records/` - List records (with pagination)
- `POST /api/v1/tables/{id}/records/` - Create record
- `GET /api/v1/records/{id}/` - Get record
- `PUT /api/v1/records/{id}/` - Update record
- `DELETE /api/v1/records/{id}/` - Delete record

### Dashboards
- `GET /api/v1/workspaces/{id}/dashboards/` - List dashboards
- `POST /api/v1/workspaces/{id}/dashboards/` - Create dashboard
- `GET /api/v1/dashboards/{id}/` - Get dashboard configuration
- `PUT /api/v1/dashboards/{id}/` - Update dashboard
- `DELETE /api/v1/dashboards/{id}/` - Delete dashboard

All API endpoints require authentication via JWT token (Bearer token) or session authentication.

## 🧪 Testing

### Run All Tests
```bash
# With Docker
docker-compose -f docker-compose.dev.yml exec web python manage.py test

# Without Docker
python manage.py test
```

### Run Specific App Tests
```bash
python manage.py test apps.users
python manage.py test apps.workspaces
```

### Run with Coverage
```bash
# Install coverage
uv add coverage

# Run tests with coverage
coverage run manage.py test
coverage report
coverage html  # Generates HTML report in htmlcov/
```

### Test Database
By default, tests use an in-memory SQLite database for speed. Configure test database in settings:

```python
# In development.py
import sys
if 'test' in sys.argv:
    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:'
    }
```

## 📝 Development Workflow

### Code Style
- Follow PEP 8 guidelines
- Use Black for code formatting: `black .`
- Use Flake8 for linting: `flake8 .`
- Write descriptive commit messages
- Add docstrings to functions and classes

### Database Migrations
After model changes:
```bash
# Create migration file
python manage.py makemigrations

# Review migration SQL
python manage.py sqlmigrate app_name migration_number

# Apply migrations
python manage.py migrate

# Create migration for specific app
python manage.py makemigrations app_name
```

### Creating a New App
```bash
# Create app in apps directory
python manage.py startapp app_name apps/app_name

# Add to INSTALLED_APPS in settings
```

### Git Workflow
```bash
# Create feature branch
git checkout -b feature/feature-name

# Make changes and commit
git add .
git commit -m "feat: Add new feature"

# Push and create PR
git push origin feature/feature-name
```

## 🐛 Troubleshooting

### Docker-Related Issues

**Issue: Database connection error - "failed to resolve host ' not localhost!'"**
- **Solution**: Check your `.env` file for inline comments. Move comments to separate lines:
  ```env
  # Wrong:
  PG_DATABASE_HOST_DEV=postgres  # This is the host
  
  # Correct:
  # This is the host
  PG_DATABASE_HOST_DEV=postgres
  ```

**Issue: Port already in use**
- **Solution**: Change ports in docker-compose.dev.yml or stop conflicting services:
  ```bash
  # Check what's using port 8000
  sudo lsof -i :8000
  # Stop the container using different port
  docker stop container_name
  ```

**Issue: Permission denied for volume mounts**
- **Solution**: Ensure proper permissions or use Docker Desktop with automatic permissions

**Issue: Container exits immediately**
- **Solution**: Check logs for errors:
  ```bash
  docker-compose -f docker-compose.dev.yml logs web
  ```

**Issue: Redis connection refused**
- **Solution**: Ensure Redis container is healthy:
  ```bash
  docker-compose -f docker-compose.dev.yml ps
  docker-compose -f docker-compose.dev.yml logs redis
  ```

### Common Development Issues

**Database connection errors**:
- Verify PostgreSQL is running: `docker-compose -f docker-compose.dev.yml ps postgres`
- Check credentials in `.env` file
- Ensure database exists: `docker-compose -f docker-compose.dev.yml exec postgres psql -U postgres -l`

**Celery tasks not executing**:
- Check Redis is running: `docker-compose -f docker-compose.dev.yml exec redis redis-cli ping`
- Verify Celery worker logs: `docker-compose -f docker-compose.dev.yml logs celery-worker`
- Check task registration: `docker-compose -f docker-compose.dev.yml exec web celery -A config inspect registered`

**Static files not loading**:
- Run collectstatic: `docker-compose -f docker-compose.dev.yml exec web python manage.py collectstatic --noinput`
- Check STATIC_URL and STATIC_ROOT settings
- Verify WhiteNoise configuration in settings

**Migration conflicts**:
```bash
# Reset migrations (careful - deletes data!)
docker-compose -f docker-compose.dev.yml exec postgres psql -U postgres -c "DROP DATABASE analyticsmeta;"
docker-compose -f docker-compose.dev.yml exec postgres psql -U postgres -c "CREATE DATABASE analyticsmeta;"
docker-compose -f docker-compose.dev.yml exec web python manage.py migrate
```

**Slow Docker performance on Mac/Windows**:
- Use Docker Desktop with VirtioFS or gRPC FUSE
- Add project to Docker's file sharing exceptions
- Use `:delegated` mount flag for volumes

### Production Deployment Tips

1. **Set DEBUG=False** in production
2. **Use environment-specific settings**:
   ```bash
   DJANGO_SETTINGS_MODULE=config.settings.production
   ```
3. **Configure proper database settings**:
   - Use connection pooling
   - Set CONN_MAX_AGE appropriately
   - Enable SSL for database connections

4. **Use Gunicorn instead of runserver**:
   ```bash
   gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 4 --threads 2
   ```

5. **Set up SSL/TLS** with Let's Encrypt or cloud provider SSL

6. **Configure logging** to file or external service (Sentry, Logstash)

## 📦 Deployment

### Deploy with Docker to Production

1. **Create production Dockerfile** (`Dockerfile.prod`):
```dockerfile
FROM python:3.14-slim-bookworm
# Production-specific setup...
```

2. **Create production docker-compose** (`docker-compose.prod.yml`):
```yaml
services:
  postgres:
    image: postgres:18-alpine
    environment:
      POSTGRES_DB: ${PG_DATABASE_NAME}
      POSTGRES_USER: ${PG_DATABASE_USER}
      POSTGRES_PASSWORD: ${PG_DATABASE_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    networks:
      - analytics_network

  redis:
    image: redis:7-alpine
    networks:
      - analytics_network

  web:
    build:
      context: .
      dockerfile: Dockerfile.prod
    command: gunicorn config.wsgi:application --bind 0.0.0.0:8000
    environment:
      - DJANGO_SETTINGS_MODULE=config.settings.production
    networks:
      - analytics_network

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
      - static_volume:/static
    networks:
      - analytics_network

volumes:
  postgres_data:
  static_volume:

networks:
  analytics_network:
    driver: bridge
```

### Deploy to Cloud Platforms

**Heroku**:
```bash
heroku create analyticsmeta
heroku addons:create heroku-postgresql:hobby-dev
heroku addons:create heroku-redis:hobby-dev
git push heroku main
```

**AWS Elastic Beanstalk**:
```bash
eb init -p docker analyticsmeta
eb create analyticsmeta-env
```

**DigitalOcean App Platform**:
- Use the Dockerfile and environment variables
- Configure PostgreSQL and Redis as attached databases

## 🤝 Contributing

1. **Fork the repository**
2. **Create a feature branch**:
   ```bash
   git checkout -b feature/amazing-feature
   ```
3. **Commit your changes**:
   ```bash
   git commit -m 'feat: Add amazing feature'
   ```
4. **Push to the branch**:
   ```bash
   git push origin feature/amazing-feature
   ```
5. **Open a Pull Request**

### Commit Convention
Use [Conventional Commits](https://www.conventionalcommits.org/):
- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation
- `style:` Formatting
- `refactor:` Code restructuring
- `test:` Testing
- `chore:` Maintenance

## 📄 License

[Add your license information here]

## 📧 Contact & Support

- **Project Maintainer**: sniphermarube@gmail.com
- **Issue Tracker**: [GitHub Issues](https://github.com/snipher-marube/bst/issues)
- **Documentation**: [Wiki](https://github.com/snipher-marube/bst/wiki)

## 🙏 Acknowledgments

- Django Software Foundation
- All open-source contributors
- PostgreSQL and Redis communities
- Tailwind CSS team

---

**Built with ❤️ using Django and modern web technologies**
```
This README provides a comprehensive guide for new developers to set up the project using Docker, along with detailed instructions for manual installation, project structure, architecture, security features, API documentation, testing, troubleshooting, and deployment. It also includes a contributing guide and contact information for support.