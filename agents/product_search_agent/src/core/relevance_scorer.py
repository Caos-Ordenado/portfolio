import hashlib
import json
import os
import re
from typing import Dict, Optional, List

from shared.logging import setup_logger
from shared.redis_client import RedisClient
from src.core.utils import url_matches_query

logger = setup_logger("relevance_scorer")


_STOPWORDS = {
    "de", "la", "el", "los", "las", "y", "o", "para", "por", "con", "sin",
    "en", "un", "una", "unos", "unas", "del", "al", "a", "the", "and", "or",
    "for", "with", "on", "of", "to",
}


class RelevanceScorer:
    def __init__(self) -> None:
        self.cache_enabled = os.getenv("RELEVANCE_CACHE_ENABLED", "true").lower() == "true"
        self.cache_ttl_seconds = int(os.getenv("RELEVANCE_CACHE_TTL_SECONDS", "21600"))  # 6 hours

    async def score_candidate(
        self,
        product_query: str,
        url: str,
        title: Optional[str],
        snippet: Optional[str],
    ) -> Dict[str, float]:
        cache_key = self._cache_key(product_query, url)
        if self.cache_enabled:
            cached = await self._get_cached_score(cache_key)
            if cached:
                return cached

        query_terms = self._tokenize(product_query)
        url_text = url or ""
        title_text = title or ""
        snippet_text = snippet or ""

        relevance_score = 0.0

        if url_matches_query(url_text, query_terms):
            relevance_score += 0.4

        relevance_score += 0.3 * self._overlap_ratio(query_terms, self._tokenize(title_text))
        relevance_score += 0.2 * self._overlap_ratio(query_terms, self._tokenize(snippet_text))

        if product_query and product_query.lower() in url_text.lower():
            relevance_score += 0.1

        relevance_score = min(1.0, relevance_score)

        location_score = self._montevideo_score(url_text, title_text, snippet_text)

        combined_score = min(1.0, relevance_score + 0.2 * location_score)

        result = {
            "relevance_score": round(relevance_score, 4),
            "location_score": round(location_score, 4),
            "combined_score": round(combined_score, 4),
        }

        if self.cache_enabled:
            await self._set_cached_score(cache_key, result)

        return result

    def _tokenize(self, text: str) -> List[str]:
        if not text:
            return []
        tokens = re.split(r"[^a-zA-Z0-9]+", text.lower())
        return [t for t in tokens if len(t) > 2 and t not in _STOPWORDS]

    def _overlap_ratio(self, query_terms: List[str], target_terms: List[str]) -> float:
        if not query_terms or not target_terms:
            return 0.0
        query_set = set(query_terms)
        target_set = set(target_terms)
        overlap = len(query_set & target_set)
        return overlap / max(1, len(query_set))

    def _montevideo_score(self, url: str, title: str, snippet: str) -> float:
        haystack = f"{url} {title} {snippet}".lower()
        terms = ("montevideo", "mvd", "mvdeo", "envio a montevideo", "retiro en montevideo")
        for term in terms:
            if term in haystack:
                return 1.0
        return 0.0

    def _cache_key(self, product_query: str, url: str) -> str:
        raw = f"{product_query}|{url}".encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        return f"relevance:{digest}"

    async def _get_cached_score(self, key: str) -> Optional[Dict[str, float]]:
        try:
            async with RedisClient() as redis_client:
                if not await redis_client.health_check():
                    return None
                value = await redis_client.get(key)
                if not value:
                    return None
                return json.loads(value)
        except Exception as e:
            logger.debug(f"Relevance cache read failed: {e}")
            return None

    async def _set_cached_score(self, key: str, value: Dict[str, float]) -> None:
        try:
            async with RedisClient() as redis_client:
                if not await redis_client.health_check():
                    return
                await redis_client.set(key, value, ex=self.cache_ttl_seconds)
        except Exception as e:
            logger.debug(f"Relevance cache write failed: {e}")
