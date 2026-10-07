# Database Setup Guide

This bot now uses PostgreSQL to store authorized users instead of hardcoded values.

## Database Schema

The bot uses two tables:

### `authorized_users` table
Stores all authorized users with their information:
- `user_id` (BIGINT, PRIMARY KEY): Telegram user ID
- `username` (VARCHAR): Telegram username
- `first_name` (VARCHAR): User's first name
- `last_name` (VARCHAR): User's last name
- `full_name` (VARCHAR): User's full name
- `is_admin` (BOOLEAN): Whether the user is an admin
- `authorized_at` (TIMESTAMP): When the user was authorized
- `authorized_by` (BIGINT): User ID of the admin who authorized this user
- `last_seen` (TIMESTAMP): Last time the user interacted with the bot
- `is_active` (BOOLEAN): Whether the user is currently active

### `pending_requests` table
Stores pending authorization requests:
- `user_id` (BIGINT, PRIMARY KEY): Telegram user ID
- `username` (VARCHAR): Telegram username
- `first_name` (VARCHAR): User's first name
- `last_name` (VARCHAR): User's last name
- `full_name` (VARCHAR): User's full name
- `requested_at` (TIMESTAMP): When the request was made

## Setup Instructions

### 1. Set Environment Variables

Add the `DATABASE_URL` environment variable to your `.env` file:

```env
DATABASE_URL=postgresql://neondb_owner:npg_jrJo8UzOA2Ze@ep-shy-hall-b5fn5kfv-pooler.c-7.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require
TELEGRAM_BOT_TOKEN=your_bot_token_here
```

### 2. Install Dependencies

Install the PostgreSQL driver:

```bash
pip install -r requirements.txt
```

This will install `psycopg2-binary` along with other dependencies.

### 3. Initialize Database

Run the initialization script to create tables and seed initial users:

```bash
python init_database.py
```

This will:
- Create the `authorized_users` and `pending_requests` tables
- Add indexes for performance
- Seed the database with the initial authorized users:
  - Ozodbek Abdumajidov (Main Admin) - ID: 7657310374
  - Nodir Qodirov - ID: 26181418
  - J.9 - ID: 1296777899
  - islom - ID: 1486771158
  - Ахмадхожа - ID: 7618952154

### 4. Run the Bot

Start the bot normally:

```bash
python bot.py
```

## Admin Commands

- `/users` - List all authorized users (admin only)
- `/revoke <user_id>` - Revoke access for a user (admin only)

## Database Management

### Adding Users Manually

If you need to add users directly to the database:

```python
from database import DatabaseManager

db = DatabaseManager()
db.add_authorized_user(
    user_id=123456789,
    first_name="John",
    last_name="Doe",
    username="johndoe",
    full_name="John Doe",
    is_admin=False,
    authorized_by=7657310374
)
```

### Removing Users Manually

```python
from database import DatabaseManager

db = DatabaseManager()
db.remove_authorized_user(user_id=123456789)
```

### Checking Authorization

```python
from database import DatabaseManager

db = DatabaseManager()
is_authorized = db.is_authorized(user_id=123456789)
print(f"User authorized: {is_authorized}")
```

## Database Connection Details

The bot uses connection pooling to efficiently manage database connections:
- Minimum connections: 1
- Maximum connections: 10
- Automatic connection retry and error handling
- Soft delete (deactivation) for removed users

## Troubleshooting

### Connection Errors

If you see "Database manager not initialized" errors:
1. Check that `DATABASE_URL` is set in your environment
2. Verify the database connection string is correct
3. Ensure the database is accessible from your server

### SSL/TLS Issues

The Neon database requires SSL. The connection string includes `sslmode=require&channel_binding=require`.

If you have SSL issues, you can try:
- `sslmode=require` (without channel_binding)
- Check your SSL certificates are up to date

### Migration from Old Version

The old version stored users in-memory using Python sets and dictionaries. The new version uses PostgreSQL for persistence. All existing users have been migrated to the database during initialization.

## Benefits of Database Storage

1. **Persistence**: User authorization survives bot restarts
2. **Scalability**: Can handle thousands of users efficiently
3. **Auditability**: Track when users were authorized and by whom
4. **History**: Keep track of last seen timestamps
5. **Recovery**: Soft delete allows restoring access if needed
6. **Concurrent Access**: Multiple bot instances can share the same user database
