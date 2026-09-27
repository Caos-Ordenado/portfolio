"""
Base model class for SQLAlchemy models.
"""

from sqlalchemy.orm import declarative_base
from typing import Dict, Any

class Base:
    """Base class for all models."""
    
    def to_redis_data(self) -> Dict[str, Any]:
        """Convert instance to Redis-storable format."""
        raise NotImplementedError("Implement to_redis_data for Redis support")
    
    @classmethod
    def from_redis_data(cls, data: Dict[str, Any]) -> 'Base':
        """Create instance from Redis data."""
        raise NotImplementedError("Implement from_redis_data for Redis support")

Base = declarative_base(cls=Base)
