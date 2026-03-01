"""
Shared logger configuration for agents.
"""

import os
import sys

def _redact_secret(value: str) -> str:
    if not value:
        return "<unset>"
    show_sensitive = os.getenv("LOG_SENSITIVE_CONFIG", "false").lower() == "true"
    if not show_sensitive:
        return "<redacted>"
    if len(value) <= 2:
        return "*" * len(value)
    return f"{'*' * (len(value) - 2)}{value[-2:]}"


def log_database_config(logger_instance=None):
    """Log database configuration details at debug level (secrets redacted)."""
    from loguru import logger
    log = logger_instance or logger

    # PostgreSQL Configuration
    log.debug("PostgreSQL Configuration:")
    log.debug(f"  Host: {os.getenv('POSTGRES_HOST')}")
    log.debug(f"  Port: {os.getenv('POSTGRES_PORT')}")
    log.debug(f"  Database: {os.getenv('POSTGRES_DB')}")
    log.debug(f"  User: {os.getenv('POSTGRES_USER')}")
    log.debug(f"  Password: {_redact_secret(os.getenv('POSTGRES_PASSWORD'))}")

    # Redis Configuration
    log.debug("Redis Configuration:")
    log.debug(f"  Host: {os.getenv('REDIS_HOST')}")
    log.debug(f"  Port: {os.getenv('REDIS_PORT')}")
    log.debug(f"  Password: {_redact_secret(os.getenv('REDIS_PASSWORD'))}")
    log.debug(f"  DB: {os.getenv('REDIS_DB')}")

def setup_logger(name: str):
    """Configure logger for an agent.
    
    Args:
        name: Name of the agent for log identification
        
    Returns:
        Configured logger instance
    """
    from loguru import logger
    
    # Remove default logger
    logger.remove()
    
    # Get log level from environment or use DEBUG as default
    log_level = os.getenv("LOG_LEVEL", "DEBUG")
    
    # Format strings - add timestamps to server.log when DEBUG level
    base_format = "<level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    file_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | " + base_format
        if log_level == "DEBUG"
        else base_format
    )
    
    # Add file handler with rotation and retention
    # Timestamps included only when LOG_LEVEL=DEBUG
    logger.add(
        "server.log",
        rotation="100 MB",
        retention="5 days",
        compression="zip",
        level=log_level,
        enqueue=True,  # Thread-safe logger
        format=file_format
    )
    
    # Add stderr handler for console output (no timestamps to keep it clean)
    logger.add(
        sys.stderr,
        level=log_level,
        format=base_format
    )
    
    logger_instance = logger.bind(name=name)
    
    return logger_instance 