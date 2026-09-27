"""
Shared utilities for agents.
"""

from .logging import setup_logger, log_database_config
from .redis_client import RedisClient
from .web_crawler_client import WebCrawlerClient, CrawlRequest, CrawlResult, CrawlResponse
from .llm_client import (
    LLMClient,
    LLMError,
    MODEL_CODER,
    MODEL_VISION,
    MODEL_REASONING,
    MODEL_EXTRACT,
)
from .renderer_client import RendererClient
from .models import Base, WebPage
from .database import DatabaseContext, DatabaseManager
from .repositories.webpage import WebPageRepository
from .config import DatabaseConfig

# Utility functions
from .utils import (
    strip_json_code_block,
    remove_json_comments,
    fix_truncated_json,
    extract_fields_from_partial_json,
    same_domain,
    normalize_url,
    dedupe_urls_preserve_order,
)

__version__ = "0.1.0"

__all__ = [
    # Logging
    'setup_logger',
    'log_database_config',
    # Clients
    'RedisClient',
    'WebCrawlerClient',
    'CrawlRequest',
    'CrawlResult',
    'CrawlResponse',
    'LLMClient',
    'LLMError',
    'MODEL_CODER',
    'MODEL_VISION',
    'MODEL_REASONING',
    'MODEL_EXTRACT',
    'RendererClient',
    # Database
    'Base',
    'WebPage',
    'DatabaseContext',
    'DatabaseManager',
    'WebPageRepository',
    'DatabaseConfig',
    # Utility functions
    'strip_json_code_block',
    'remove_json_comments',
    'fix_truncated_json',
    'extract_fields_from_partial_json',
    'same_domain',
    'normalize_url',
    'dedupe_urls_preserve_order',
    # Version
    '__version__',
] 