1. API Design Rules

Every endpoint must have a response_model
Use proper HTTP status codes (201 for creation, 204 for delete, etc.)
Path parameters and query parameters must use clear names
Do not return raw database models — always use Pydantic response schemas
<!-- Breaking changes must go through a versioned endpoint (/v2/...) -->

2. Code Structure & Organization

Business logic must live in services/, not inside the route functions
Database access only through repository / dependency injection
<!-- Maximum function length (e.g. 40–50 lines) -->
File and folder naming conventions (snake_case, specific folder structure)

3. Dependency & Configuration Rules

Never hard-code configuration — use pydantic-settings or environment variables
All external services (DB, Redis, external APIs) must be injected via Depends
<!-- Do not create global database sessions -->

4. Async & Performance Rules

No blocking calls (time.sleep, sync SQLAlchemy, requests) inside async def endpoints
Database sessions must be properly scoped and closed
Use BackgroundTasks or proper task queues for heavy work

5. Error Handling & Logging

Do not expose internal exception messages to the client
Use custom exception handlers
Every error must be logged with a clear message and request ID
Forbidden to use bare except:

6. Security Rules

Every non-public endpoint must have an authentication dependency
Never log passwords, tokens, or full credit card numbers
Input validation must be done via Pydantic (no manual validation in routes)
CORS must not be set to * in production

7. Testing Rules

New endpoints must come with at least one test
Critical business logic must have unit tests
Do not disable or delete existing tests without discussion

8. Documentation Rules

Every public endpoint must have a clear OpenAPI description (summary + description)
Response models must have Field descriptions for important fields
Breaking changes must update the changelog