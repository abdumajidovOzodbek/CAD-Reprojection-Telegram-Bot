"""Initialize database schema and seed with initial authorized users."""

import logging
import os
from pathlib import Path
from database import DatabaseManager

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    load_dotenv(env_path)
except ImportError:
    # dotenv not installed, rely on environment variables
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

LOGGER = logging.getLogger("database-init")

# Initial authorized users to seed the database
INITIAL_USERS = [
    {
        "user_id": 7657310374,
        "first_name": "Ozodbek",
        "last_name": "Abdumajidov 🇸🇪",
        "username": "abdumajidov_ozodbek",
        "is_admin": True,  # Main admin
    },
    {
        "user_id": 26181418,
        "first_name": "Nodir",
        "last_name": "Qodirov",
        "username": "Nodir_DKYAT",
        "is_admin": False,
    },
    {
        "user_id": 288494560,
        "first_name": "7777777",
        "last_name": "",
        "username": "AfaromeoS",
        "is_admin": False,
    },
    {
        "user_id": 640009039,
        "first_name": "𝚜𝚊𝚒𝚍𝚊𝚣𝚒𝚣",
        "last_name": "",
        "username": "",
        "is_admin": False,
    },
    {
        "user_id": 1296777899,
        "first_name": "J.9",
        "last_name": "",
        "username": "",
        "is_admin": False,
    },
    {
        "user_id": 1486771158,
        "first_name": "islom",
        "last_name": "",
        "username": "Islom0720",
        "is_admin": False,
    },
    {
        "user_id": 7618952154,
        "first_name": "Ахмадхожа",
        "last_name": "",
        "username": "",
        "is_admin": False,
    },
]


def main():
    """Initialize database and seed with initial users."""
    database_url = os.getenv("DATABASE_URL")
    
    if not database_url:
        LOGGER.error("DATABASE_URL environment variable is not set")
        return
    
    LOGGER.info("Connecting to database...")
    db = DatabaseManager(database_url)
    
    try:
        # Initialize schema
        LOGGER.info("Initializing database schema...")
        db.initialize_schema()
        LOGGER.info("✓ Database schema initialized")
        
        # Seed initial users
        LOGGER.info("Seeding initial authorized users...")
        for user in INITIAL_USERS:
            db.add_authorized_user(
                user_id=user["user_id"],
                first_name=user["first_name"],
                last_name=user.get("last_name"),
                username=user.get("username"),
                full_name=f"{user['first_name']} {user.get('last_name', '')}".strip(),
                is_admin=user.get("is_admin", False),
                authorized_by=7657310374  # Authorized by main admin
            )
            LOGGER.info(f"✓ Added user: {user['first_name']} (ID: {user['user_id']})")
        
        # Verify setup
        authorized_count = len(db.get_all_authorized_users())
        LOGGER.info(f"\n✅ Database initialization complete!")
        LOGGER.info(f"Total authorized users: {authorized_count}")
        
    except Exception as exc:
        LOGGER.error(f"❌ Database initialization failed: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
