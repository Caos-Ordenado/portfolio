from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool

# Import the shared client and its Pydantic models
from shared.web_crawler_client import WebCrawlerClient, CrawlResponse, CrawlRequest # CrawlRequest might be useful
from shared.logging import setup_logger
import asyncio
import aiohttp

logger = setup_logger("product_search_agent.web_crawler_trigger_tool")

class WebCrawlerLangchainToolInput(BaseModel):
    """Input schema for the Web Crawler Langchain Trigger Tool."""
    urls_to_crawl: List[str] = Field(
        ..., 
        description="A list of starting URLs to crawl."
    )
    max_pages: Optional[int] = Field(
        None, 
        description="Maximum number of pages to crawl. If None, client's default is used."
    )
    max_depth: Optional[int] = Field(
        None, 
        description="Maximum depth to crawl. If None, client's default is used."
    )
    allowed_domains: Optional[List[str]] = Field(
        None, 
        description="List of allowed domains. If None, all domains are allowed (within crawl limits)."
    )
    exclude_patterns: Optional[List[str]] = Field(
        None, 
        description="List of URL patterns to exclude (e.g., ['*.pdf', '*.jpg'])."
    )
    respect_robots: Optional[bool] = Field(
        None, 
        description="Whether to respect robots.txt. If None, client's default is used."
    )
    max_total_time: Optional[int] = Field(
        None, 
        description="Maximum total time in seconds for the crawl. If None, client's default is used."
    )
    # timeout and max_concurrent_pages can be added if needed for LLM control

async def _execute_web_crawl_for_tool(
    urls_to_crawl: List[str],
    max_pages: Optional[int] = None,
    max_depth: Optional[int] = None,
    allowed_domains: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    respect_robots: Optional[bool] = None,
    max_total_time: Optional[int] = None
) -> Dict[str, Any]:
    """
    Internal function to execute web crawling using the shared WebCrawlerClient.
    This function is wrapped by the Langchain StructuredTool.
    """
    if not urls_to_crawl:
        logger.warning("No URLs provided for crawling by the tool.")
        return {"status": "error", "message": "No URLs provided for crawling."}

    logger.info(
        f"Langchain Tool initiating crawl for URLs: {urls_to_crawl} with parameters: "
        f"max_pages={max_pages}, max_depth={max_depth}, allowed_domains={allowed_domains}, "
        f"respect_robots={respect_robots}, exclude_patterns={exclude_patterns}, "
        f"max_total_time={max_total_time}"
    )

    try:
        async with WebCrawlerClient() as client:
            if not await client.health_check():
                logger.error("Web crawler service health check failed. Aborting crawl via tool.")
                return {"status": "error", "message": "Web crawler service is unhealthy or not reachable."}
            
            logger.debug("Web crawler service is healthy. Proceeding with crawl via tool.")

            response: CrawlResponse = await client.crawl(
                urls=urls_to_crawl,
                max_pages=max_pages, # Will use client default if None
                max_depth=max_depth, # Will use client default if None
                allowed_domains=allowed_domains,
                exclude_patterns=exclude_patterns,
                respect_robots=respect_robots, # Will use client default if None
                max_total_time=max_total_time # Will use client default if None
            )

            log_message = (
                f"Crawling finished by tool. Total URLs processed: {response.total_urls}, "
                f"Successfully crawled: {response.crawled_urls}, Elapsed: {response.elapsed_time:.2f}s"
            )

            if response.error:
                logger.warning(f"{log_message}. Service reported an error: {response.error}")
                return {
                    "status": "partial_success", 
                    "message": f"Crawling completed with service error: {response.error}", 
                    "data": response.__dict__
                }
            
            logger.info(log_message)
            return {
                "status": "success", 
                "data": response.__dict__
            }

    except aiohttp.ClientConnectorError as e:
        error_message = f"Connection error: Could not connect to the web crawler service. Details: {e}"
        logger.error(f"Tool-based web crawl failed: {error_message}", exc_info=True)
        return {"status": "error", "message": error_message}
    except asyncio.TimeoutError:
        logger.error("Tool-based web crawl request to the service timed out.", exc_info=True)
        return {"status": "error", "message": "The crawl request to the web crawler service timed out."}
    except Exception as e:
        logger.error(f"Tool-based web crawl failed with an unexpected error: {e}", exc_info=True)
        return {"status": "error", "message": f"An unexpected error occurred during crawling: {str(e)}"} 

# Create the Langchain StructuredTool
web_crawler_trigger_tool = StructuredTool.from_function(
    func=_execute_web_crawl_for_tool, # The new async function that performs the action
    name="WebCrawlerTriggerTool",
    description=(
        "Initiates web crawling for a list of specified URLs. "
        "Use this tool to gather information from websites when you need to find product details, "
        "research topics, or get up-to-date information from specific web pages. "
        "You can specify target URLs, maximum pages/depth, allowed domains, and other parameters to control the crawl. "
        "The tool returns a summary of the crawl, including total URLs processed and any errors."
    ),
    args_schema=WebCrawlerLangchainToolInput, # The Pydantic model for input validation
)

# Example (conceptual, actual usage depends on your agent setup):
# from langchain.agents import AgentExecutor, create_openai_tools_agent
# from langchain_openai import ChatOpenAI
#
# llm = ChatOpenAI(model="gpt-3.5-turbo-1106", temperature=0)
# tools = [web_crawler_trigger_tool]
# prompt = ... # Your agent's prompt
# agent = create_openai_tools_agent(llm, tools, prompt)
# agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)
#
# async def run_agent_example():
# response = await agent_executor.ainvoke({
# "input": "Crawl example.com for up to 3 pages."
# })
# print(response)
#
# if __name__ == "__main__":
# import asyncio
# asyncio.run(run_agent_example()) 