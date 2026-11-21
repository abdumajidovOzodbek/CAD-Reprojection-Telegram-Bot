"""Database module for user authorization management using PostgreSQL."""

import logging
import os
import threading
import time
from typing import Dict, List, Optional, Set
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

LOGGER = logging.getLogger("cad-reprojection-bot.database")


class DatabaseManager:
    """Manages PostgreSQL database connections and user authorization data."""
    
    def __init__(self, database_url: Optional[str] = None):
        """
        Initialize database connection pool.
        
        Args:
            database_url: PostgreSQL connection URL. If None, reads from DATABASE_URL env variable.
        """
        self.database_url = database_url or os.getenv("DATABASE_URL")
        if not self.database_url:
            raise ValueError("DATABASE_URL must be provided or set in environment")
        
        # Create thread-safe connection pool with keepalive settings for Railway/Neon
        pool_cls = getattr(
            psycopg2.pool, "ThreadedConnectionPool", psycopg2.pool.SimpleConnectionPool
        )
        self._conn_lock = threading.Lock()
        self._conn_last_used: Dict[int, float] = {}
        self._idle_ping_seconds = 15.0
        try:
            self.connection_pool = pool_cls(
                1,  # min connections
                20,  # max connections (increased for better concurrency)
                self.database_url,
                # Add keepalive settings to prevent connection drops
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5,
                connect_timeout=10
            )
            LOGGER.info("Database connection pool created successfully")
        except Exception as exc:
            LOGGER.error(f"Failed to create database connection pool: {exc}")
            raise

    def _mark_conn_used(self, conn) -> None:
        with self._conn_lock:
            self._conn_last_used[id(conn)] = time.monotonic()

    def _forget_conn(self, conn) -> None:
        with self._conn_lock:
            self._conn_last_used.pop(id(conn), None)

    def _needs_ping(self, conn) -> bool:
        with self._conn_lock:
            last_used = self._conn_last_used.get(id(conn))
        if last_used is None:
            return True
        return (time.monotonic() - last_used) >= self._idle_ping_seconds

    def _acquire_healthy_connection(self, max_retries: int = 3):
        """Obtain a live connection from the pool, retrying stale Neon sockets before yielding."""
        for retry_count in range(max_retries):
            conn = None
            try:
                conn = self.connection_pool.getconn()
                if getattr(conn, "closed", 0):
                    self._forget_conn(conn)
                    self.connection_pool.putconn(conn, close=True)
                    conn = None
                    raise psycopg2.InterfaceError("Pooled connection was closed")

                if self._needs_ping(conn):
                    with conn.cursor() as test_cursor:
                        test_cursor.execute("SELECT 1")
                    conn.rollback()
                    self._mark_conn_used(conn)

                return conn
            except (psycopg2.InterfaceError, psycopg2.OperationalError) as exc:
                LOGGER.warning(
                    f"Database connection error (attempt {retry_count + 1}/{max_retries}): {exc}"
                )
                if conn is not None:
                    self._forget_conn(conn)
                    try:
                        self.connection_pool.putconn(conn, close=True)
                    except Exception:
                        pass
                if retry_count < max_retries - 1:
                    time.sleep(0.5)
                    continue
                LOGGER.error(f"Database operation failed after {max_retries} retries")
                raise

    @contextmanager
    def get_connection(self, readonly: bool = False):
        """Context manager for database connections (retries happen before yield, never across yield)."""
        conn = self._acquire_healthy_connection()
        returned_to_pool = False
        try:
            yield conn
            if readonly:
                conn.rollback()
            else:
                conn.commit()
            self._mark_conn_used(conn)
        except (psycopg2.InterfaceError, psycopg2.OperationalError) as exc:
            LOGGER.warning(f"Database connection error during query: {exc}")
            self._forget_conn(conn)
            try:
                self.connection_pool.putconn(conn, close=True)
                returned_to_pool = True
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            LOGGER.error(f"Database operation failed: {exc}")
            raise
        finally:
            if not returned_to_pool:
                try:
                    is_closed = bool(getattr(conn, "closed", 0))
                    if is_closed:
                        self._forget_conn(conn)
                    self.connection_pool.putconn(conn, close=is_closed)
                except Exception:
                    pass

    def _run_with_retry(self, operation, max_attempts: int = 2):
        """Retry an entire DB operation if a pooled socket drops mid-query."""
        for attempt in range(max_attempts):
            try:
                return operation()
            except (psycopg2.InterfaceError, psycopg2.OperationalError):
                if attempt < max_attempts - 1:
                    time.sleep(0.25)
                    continue
                raise
    
    def initialize_schema(self):
        """Create database tables if they don't exist."""
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                # Create authorized_users table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS authorized_users (
                        user_id BIGINT PRIMARY KEY,
                        username VARCHAR(255),
                        first_name VARCHAR(255) NOT NULL,
                        last_name VARCHAR(255),
                        full_name VARCHAR(510),
                        is_admin BOOLEAN DEFAULT FALSE,
                        authorized_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        authorized_by BIGINT,
                        last_seen TIMESTAMP,
                        is_active BOOLEAN DEFAULT TRUE
                    )
                """)
                
                # Create pending_requests table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS pending_requests (
                        user_id BIGINT PRIMARY KEY,
                        username VARCHAR(255),
                        first_name VARCHAR(255) NOT NULL,
                        last_name VARCHAR(255),
                        full_name VARCHAR(510),
                        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Create index on username for faster lookups
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_authorized_users_username 
                    ON authorized_users(username)
                """)
                
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_authorized_users_is_active 
                    ON authorized_users(is_active)
                """)
                
                LOGGER.info("Database schema initialized successfully")
    
    def add_authorized_user(
        self, 
        user_id: int, 
        first_name: str,
        last_name: Optional[str] = None,
        username: Optional[str] = None,
        full_name: Optional[str] = None,
        is_admin: bool = False,
        authorized_by: Optional[int] = None
    ) -> None:
        """
        Add a user to the authorized users table.
        
        Args:
            user_id: Telegram user ID
            first_name: User's first name
            last_name: User's last name (optional)
            username: User's username (optional)
            full_name: User's full name (optional)
            is_admin: Whether the user is an admin
            authorized_by: User ID of the admin who authorized this user
        """
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO authorized_users 
                    (user_id, username, first_name, last_name, full_name, is_admin, authorized_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) 
                    DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        full_name = EXCLUDED.full_name,
                        is_admin = EXCLUDED.is_admin,
                        is_active = TRUE
                """, (user_id, username, first_name, last_name, full_name, is_admin, authorized_by))
                
                LOGGER.info(f"User {user_id} ({first_name}) added to authorized users")
    
    def remove_authorized_user(self, user_id: int) -> bool:
        """
        Deactivate a user (soft delete).
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            True if user was deactivated, False if user doesn't exist
        """
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE authorized_users 
                    SET is_active = FALSE
                    WHERE user_id = %s AND is_active = TRUE
                """, (user_id,))
                
                if cursor.rowcount > 0:
                    LOGGER.info(f"User {user_id} deactivated")
                    return True
                return False
    
    def is_authorized(self, user_id: int) -> bool:
        """
        Check if a user is authorized and active.
        Fast lookup with minimal overhead; raises on persistent database errors
        so callers do not mistake an outage for missing user access.
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            True if user is authorized and active
        """
        def _query() -> bool:
            with self.get_connection(readonly=True) as conn:
                with conn.cursor() as cursor:
                    # Use EXISTS for faster check (stops at first match)
                    cursor.execute("""
                        SELECT EXISTS(
                            SELECT 1 FROM authorized_users 
                            WHERE user_id = %s AND is_active = TRUE
                        )
                    """, (user_id,))
                    return bool(cursor.fetchone()[0])

        return self._run_with_retry(_query)
    
    def is_admin(self, user_id: int) -> bool:
        """
        Check if a user is an admin.
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            True if user is an admin
        """
        with self.get_connection(readonly=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT is_admin FROM authorized_users 
                    WHERE user_id = %s AND is_active = TRUE
                """, (user_id,))
                
                result = cursor.fetchone()
                return result[0] if result else False
    
    def get_all_authorized_users(self) -> List[Dict]:
        """
        Get all active authorized users.
        
        Returns:
            List of user dictionaries
        """
        with self.get_connection(readonly=True) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT user_id, username, first_name, last_name, full_name, 
                           is_admin, authorized_at, last_seen
                    FROM authorized_users 
                    WHERE is_active = TRUE
                    ORDER BY authorized_at DESC
                """)
                
                return [dict(row) for row in cursor.fetchall()]
    
    def get_authorized_user_ids(self) -> Set[int]:
        """
        Get set of all active authorized user IDs.
        
        Returns:
            Set of user IDs
        """
        with self.get_connection(readonly=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT user_id FROM authorized_users 
                    WHERE is_active = TRUE
                """)
                
                return {row[0] for row in cursor.fetchall()}
    
    def get_user_info(self, user_id: int) -> Optional[Dict]:
        """
        Get user information.
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            User info dictionary or None if not found
        """
        with self.get_connection(readonly=True) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT user_id, username, first_name, last_name, full_name, 
                           is_admin, authorized_at, last_seen
                    FROM authorized_users 
                    WHERE user_id = %s
                """, (user_id,))
                
                result = cursor.fetchone()
                return dict(result) if result else None
    
    def refresh_user_profile(
        self,
        user_id: int,
        first_name: str,
        last_name: Optional[str] = None,
        username: Optional[str] = None,
        full_name: Optional[str] = None,
    ) -> None:
        """
        Update an active user's profile fields and last_seen without altering is_admin.
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE authorized_users
                        SET username = %s,
                            first_name = %s,
                            last_name = %s,
                            full_name = %s,
                            last_seen = CURRENT_TIMESTAMP
                        WHERE user_id = %s AND is_active = TRUE
                    """, (username, first_name, last_name, full_name, user_id))
        except Exception as exc:
            LOGGER.debug(f"Could not refresh profile for user {user_id}: {exc}")

    def update_last_seen(self, user_id: int) -> None:
        """
        Update user's last seen timestamp (non-blocking, fire and forget).
        
        Args:
            user_id: Telegram user ID
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE authorized_users 
                        SET last_seen = CURRENT_TIMESTAMP
                        WHERE user_id = %s AND is_active = TRUE
                    """, (user_id,))
        except Exception as exc:
            # Don't fail the main operation if last_seen update fails
            LOGGER.debug(f"Could not update last_seen for user {user_id}: {exc}")
    
    # Pending requests management
    
    def add_pending_request(
        self, 
        user_id: int, 
        first_name: str,
        last_name: Optional[str] = None,
        username: Optional[str] = None,
        full_name: Optional[str] = None
    ) -> None:
        """
        Add a pending authorization request.
        
        Args:
            user_id: Telegram user ID
            first_name: User's first name
            last_name: User's last name (optional)
            username: User's username (optional)
            full_name: User's full name (optional)
        """
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO pending_requests 
                    (user_id, username, first_name, last_name, full_name)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) 
                    DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        full_name = EXCLUDED.full_name,
                        requested_at = CURRENT_TIMESTAMP
                """, (user_id, username, first_name, last_name, full_name))
                
                LOGGER.info(f"Pending request added for user {user_id} ({first_name})")
    
    def remove_pending_request(self, user_id: int) -> bool:
        """
        Remove a pending authorization request.
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            True if request was removed, False if it doesn't exist
        """
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    DELETE FROM pending_requests 
                    WHERE user_id = %s
                """, (user_id,))
                
                return cursor.rowcount > 0
    
    def is_pending(self, user_id: int) -> bool:
        """
        Check if user has a pending authorization request.
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            True if request is pending
        """
        with self.get_connection(readonly=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT 1 FROM pending_requests 
                    WHERE user_id = %s
                """, (user_id,))
                
                return cursor.fetchone() is not None
    
    def get_pending_request(self, user_id: int) -> Optional[Dict]:
        """
        Get pending request information.
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            Request info dictionary or None if not found
        """
        with self.get_connection(readonly=True) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT user_id, username, first_name, last_name, full_name, requested_at
                    FROM pending_requests 
                    WHERE user_id = %s
                """, (user_id,))
                
                result = cursor.fetchone()
                return dict(result) if result else None
    
    def close(self):
        """Close all database connections."""
        if hasattr(self, 'connection_pool'):
            self.connection_pool.closeall()
            LOGGER.info("Database connection pool closed")
