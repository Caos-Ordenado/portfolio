"""
Redis client for agent utilities.
"""

import os
import asyncio
from typing import Optional, Any, Dict
import json
import aioredis
from .logging import setup_logger
from contextlib import asynccontextmanager

logger = setup_logger(__name__)

class RedisClient:
    """Async Redis client for agent utilities."""
    
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        db: Optional[int] = None,
        password: Optional[str] = None,
        decode_responses: bool = True,
        **kwargs
    ):
        """Initialize Redis client.
        
        Args:
            host: Redis host (default: from REDIS_HOST env var)
            port: Redis port (default: from REDIS_PORT env var)
            db: Redis database number (default: from REDIS_DB env var)
            password: Redis password (default: from REDIS_PASSWORD env var)
            decode_responses: Whether to decode byte responses to strings
            **kwargs: Additional arguments passed to aioredis.from_url
        """
        self.host = host or os.getenv("REDIS_HOST", "home.server")
        self.port = port or int(os.getenv("REDIS_PORT", "6379"))
        self.db = db or int(os.getenv("REDIS_DB", "0"))
        self.password = password or os.getenv("REDIS_PASSWORD", "")
        self.decode_responses = decode_responses
        self.kwargs = kwargs
        self.client: Optional[aioredis.Redis] = None
        
    async def __aenter__(self) -> 'RedisClient':
        """Async context manager entry."""
        if not self.client:
            redis_url = f"redis://{self.host}:{self.port}/{self.db}"
            logger.debug(f"Connecting to Redis at {self.host}:{self.port}/{self.db}")
            
            # Add retry logic for concurrent connection race conditions
            max_retries = 3
            retry_delay = 0.1  # 100ms
            
            for attempt in range(max_retries):
                try:
                    self.client = await aioredis.from_url(
                        redis_url,
                        password=self.password,
                        decode_responses=self.decode_responses,
                        **self.kwargs
                    )
                    break  # Success!
                except Exception as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"Redis connection attempt {attempt + 1} failed: {e}. Retrying in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        logger.error(f"Redis connection failed after {max_retries} attempts: {e}")
                        raise
            
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.client:
            await self.client.close()
            self.client = None
            
    async def health_check(self) -> bool:
        """Check Redis connection health.
        
        Returns:
            bool: True if Redis is healthy, False otherwise
        """
        try:
            if not self.client:
                return False
            await self.client.ping()
            return True
        except Exception as e:
            logger.error(f"Redis health check failed: {str(e)}")
            return False
            
    async def get(self, key: str, default: Any = None) -> Any:
        """Get value from Redis.
        
        Args:
            key: Redis key
            default: Default value if key doesn't exist
            
        Returns:
            Any: Value from Redis or default
        """
        if not self.client:
            raise RuntimeError("Redis client not initialized")
            
        try:
            value = await self.client.get(key)
            if value is None:
                return default
                
            return value
                
        except Exception as e:
            logger.error(f"Error getting key {key}: {str(e)}")
            return default
            
    async def set(
        self,
        key: str,
        value: Any,
        ex: Optional[int] = None,
        px: Optional[int] = None,
        nx: bool = False,
        xx: bool = False
    ) -> bool:
        """Set value in Redis.
        
        Args:
            key: Redis key
            value: Value to store
            ex: Expire time in seconds
            px: Expire time in milliseconds
            nx: Only set if key doesn't exist
            xx: Only set if key exists
            
        Returns:
            bool: True if value was set, False otherwise
        """
        if not self.client:
            raise RuntimeError("Redis client not initialized")
            
        try:
            # Convert value to JSON if it's not a string
            if not isinstance(value, (str, bytes)):
                value = json.dumps(value)
                
            await self.client.set(
                key,
                value,
                ex=ex,
                px=px,
                nx=nx,
                xx=xx
            )
            return True
            
        except Exception as e:
            logger.error(f"Error setting key {key}: {str(e)}")
            return False
            
    async def delete(self, key: str) -> bool:
        """Delete key from Redis.
        
        Args:
            key: Redis key
            
        Returns:
            bool: True if key was deleted, False otherwise
        """
        if not self.client:
            raise RuntimeError("Redis client not initialized")
            
        try:
            await self.client.delete(key)
            return True
        except Exception as e:
            logger.error(f"Error deleting key {key}: {str(e)}")
            return False
            
    @asynccontextmanager
    async def pipeline(self):
        """Get Redis pipeline for batch operations.
        
        Usage:
            async with redis.pipeline() as pipe:
                await pipe.set("key1", "value1")
                await pipe.set("key2", "value2")
                await pipe.execute()
        """
        if not self.client:
            raise RuntimeError("Redis client not initialized")
            
        pipe = self.client.pipeline()
        try:
            yield pipe
            await pipe.execute()
        except Exception as e:
            logger.error(f"Pipeline error: {str(e)}")
            raise
        finally:
            await pipe.reset() 