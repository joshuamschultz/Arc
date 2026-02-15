"""
Olivia's Web Search Tool
A simple web search and page reading tool for independent research.
"""

import requests
from bs4 import BeautifulSoup
from urllib.parse import quote
import time
from typing import List, Dict, Optional

class WebSearchTool:
    """Web search and page reading tool."""
    
    def __init__(self, timeout: int = 10, delay: float = 1.0):
        """
        Initialize the web search tool.
        
        Args:
            timeout: Request timeout in seconds
            delay: Delay between requests in seconds (be respectful!)
        """
        self.timeout = timeout
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def search(self, query: str, num_results: int = 10) -> List[Dict[str, str]]:
        """
        Search the web using DuckDuckGo.
        
        Args:
            query: Search query
            num_results: Number of results to return
            
        Returns:
            List of dicts with 'title', 'url', and 'snippet' keys
        """
        try:
            url = f"https://duckduckgo.com/html/?q={quote(query)}"
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            
            # Find search result links
            for result in soup.find_all('a', {'class': 'result__a'})[:num_results]:
                title = result.get_text(strip=True)
                link = result.get('href', '')
                
                # Find snippet
                snippet_elem = result.find_parent('article')
                snippet = ''
                if snippet_elem:
                    snippet_div = snippet_elem.find('a', {'class': 'result__snippet'})
                    if snippet_div:
                        snippet = snippet_div.get_text(strip=True)
                
                if link and title:
                    results.append({
                        'title': title,
                        'url': link,
                        'snippet': snippet
                    })
            
            return results
        
        except Exception as e:
            print(f"Search error: {e}")
            return []
    
    def read_page(self, url: str) -> Optional[str]:
        """
        Read and extract main content from a web page.
        
        Args:
            url: URL to read
            
        Returns:
            Extracted text content or None if failed
        """
        try:
            time.sleep(self.delay)  # Be respectful
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Remove script and style elements
            for script in soup(['script', 'style', 'nav', 'footer']):
                script.decompose()
            
            # Try to find main content
            main_content = None
            for selector in ['article', 'main', '[role="main"]', '.content', '.post', '.entry']:
                main_content = soup.select_one(selector)
                if main_content:
                    break
            
            # Fall back to body if no main content found
            if not main_content:
                main_content = soup.find('body')
            
            if main_content:
                text = main_content.get_text(separator='\n', strip=True)
                # Clean up excessive whitespace
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                return '\n'.join(lines)
            
            return None
        
        except Exception as e:
            print(f"Read page error: {e}")
            return None
    
    def search_and_read(self, query: str, num_results: int = 3) -> List[Dict[str, str]]:
        """
        Search and automatically read the top results.
        
        Args:
            query: Search query
            num_results: Number of results to read
            
        Returns:
            List of dicts with 'title', 'url', 'snippet', and 'content' keys
        """
        search_results = self.search(query, num_results)
        
        for result in search_results:
            print(f"Reading: {result['title']}...")
            content = self.read_page(result['url'])
            result['content'] = content[:2000] if content else None  # First 2000 chars
        
        return search_results


# Example usage
if __name__ == "__main__":
    tool = WebSearchTool()
    
    # Example 1: Basic search
    print("=== Search Example ===")
    results = tool.search("Python web scraping", num_results=3)
    for i, result in enumerate(results, 1):
        print(f"\n{i}. {result['title']}")
        print(f"   URL: {result['url']}")
        print(f"   Snippet: {result['snippet'][:100]}...")
    
    # Example 2: Read a page
    print("\n\n=== Read Page Example ===")
    if results:
        content = tool.read_page(results[0]['url'])
        if content:
            print(f"Content preview:\n{content[:500]}...")
