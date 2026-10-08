"""Verify that all required files exist for the database migration."""

import os
import sys

required_files = [
    'database.py',
    'init_database.py',
    'bot.py',
    'requirements.txt',
    'DATABASE_SETUP.md',
]

print("Checking for required files...\n")

all_exist = True
for file in required_files:
    exists = os.path.exists(file)
    status = "✅ EXISTS" if exists else "❌ MISSING"
    print(f"{status}: {file}")
    
    if not exists:
        all_exist = False

print("\n" + "="*50)

if all_exist:
    print("✅ All files present!")
    print("\nNext steps:")
    print("1. pip install -r requirements.txt")
    print("2. python init_database.py")
    print("3. python bot.py")
else:
    print("❌ Some files are missing!")
    print("\nRun this in Replit Shell:")
    print("git pull origin main")
    sys.exit(1)

# Check if psycopg2 is installed
try:
    import psycopg2
    print("✅ psycopg2-binary is installed")
except ImportError:
    print("❌ psycopg2-binary NOT installed")
    print("   Run: pip install psycopg2-binary")

# Check DATABASE_URL
database_url = os.getenv("DATABASE_URL")
if database_url:
    print("✅ DATABASE_URL is set")
else:
    print("❌ DATABASE_URL is NOT set")
    print("   Add it to your Replit Secrets")
