"""
Shared API interfaces (DTOs) for the Web Crawler service.
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field, field_validator
from urllib.parse import urlparse


class CrawlRequest(BaseModel):
    urls: List[str] = Field(..., description="List of URLs to crawl")
    max_pages: Optional[int] = Field(default=10000, gt=0)
    max_depth: Optional[int] = Field(default=20, gt=0)
    allowed_domains: Optional[List[str]] = None
    exclude_patterns: Optional[List[str]] = None
    respect_robots: Optional[bool] = Field(default=False)
    timeout: Optional[int] = Field(default=180000, gt=0, description="ms")
    max_total_time: Optional[int] = Field(default=300, gt=0, description="s")
    max_concurrent_pages: Optional[int] = Field(default=10, gt=0)

class CrawlResult(BaseModel):
    url: str
    title: Optional[str]
    text: str
    links: List[str]
    metadata: dict


class CrawlResponse(BaseModel):
    success: bool
    results: List[CrawlResult]
    total_urls: int
    crawled_urls: int
    elapsed_time: float

class SingleCrawlRequest(BaseModel):
    url: str = Field(..., description="URL to crawl")
    timeout: Optional[int] = Field(default=180000, gt=0)

    @field_validator('url')
    @classmethod
    def validate_url(cls, v):
        result = urlparse(v)
        if not all([result.scheme, result.netloc]):
            raise ValueError('Invalid URL format: URL must include scheme and netloc')
        if result.scheme not in ['http', 'https']:
            raise ValueError('Invalid URL scheme: Only http and https are supported')
        return v

class SingleCrawlResponse(BaseModel):
    success: bool
    result: Optional[CrawlResult] = None
    elapsed_time: float
    error: Optional[str] = None


class VisionExtractRequest(BaseModel):
    url: str = Field(..., description="URL to navigate and capture screenshot from")
    fields: Optional[List[str]] = Field(
        default_factory=lambda: ["name", "price", "currency", "availability"],
        description="Fields to extract as JSON keys",
    )
    timeout: Optional[int] = Field(default=60000, gt=0)

    @field_validator('url')
    @classmethod
    def validate_url2(cls, v):
        result = urlparse(v)
        if not all([result.scheme, result.netloc]):
            raise ValueError('Invalid URL format: URL must include scheme and netloc')
        if result.scheme not in ['http', 'https']:
            raise ValueError('Invalid URL scheme: Only http and https are supported')
        return v


class VisionExtractResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    elapsed_time: float
    error: Optional[str] = None


