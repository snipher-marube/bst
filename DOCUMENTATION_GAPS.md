# Documentation Gaps Audit - AnalyticsMeta

This document identifies missing or incomplete information in the current project documentation.

## 1. Environment Configuration
- **Missing Variables**: The `README.md` and `.env.example` do not list all required environment variables for development (e.g., `PG_DATABASE_NAME_DEV`, `PG_DATABASE_USER_DEV`, etc.).
- **OIDC/Social Auth**: While Google and LinkedIn are mentioned, more detailed setup instructions for these providers (like callback URLs) are missing.

## 2. API Documentation
- **Endpoints**: No comprehensive list of available API endpoints is provided.
- **Authentication**: JWT authentication is mentioned but there are no examples of how to obtain and use the token.
- **Request/Response Formats**: No schemas or examples for common API requests (like creating a record or updating a dashboard).

## 3. Advanced Features
- **Formulas**: The `formulas` app is mentioned in the project structure, but there is no documentation on how to use them within a `DataTable`.
- **Integrations**: No details on which third-party services are currently supported or how to add new ones.
- **Custom Field Types**: Instructions on how to add or customize field types in `apps/fields` are missing.

## 4. Deployment
- **Docker**: There is no mention of Docker or Docker Compose for local development or production deployment, which is common in modern web projects.
- **Celery & Redis**: Production-grade configuration for Celery and Redis is not fully covered.

## 5. Development Guide
- **Testing**: While the `test` command is provided, there is no information on how to add new tests or what the testing strategy is.
- **Frontend**: The use of Alpine.js and HTMX is inferred from templates, but there is no explicit documentation on the frontend architecture.
- **Template Tags**: Custom template tags (like `dashboard_filters`) are not documented.

## 6. Automated Insights Engine
- **Algorithm**: The logic used by `WorkspaceInsightService` to discover KPIs and categorical insights is not documented.
- **Widget Configuration**: There are no docs explaining how the `viz_config` maps to Chart.js options or how users can manually customize auto-generated widgets.
