# Olivia's Web Search Tool

A simple, API-free web search and page reading tool for independent research.

## Features

- **Web Search** - Search using DuckDuckGo (no API key needed)
- **Page Reading** - Extract main content from web pages
- **Combined Search & Read** - Search and automatically read top results
- **Respectful Scraping** - Built-in delays between requests
- **Simple API** - Easy to use in Python

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Basic Search

```python
from websearch import WebSearchTool

tool = WebSearchTool()
results = tool.search("Python programming", num_results=5)

for result in results:
    print(f"Title: {result['title']}")
    print(f"URL: {result['url']}")
    print(f"Snippet: {result['snippet']}\n")
```

### Read a Web Page

```python
content = tool.read_page("https://example.com")
print(content)
```

### Search and Read Together

```python
results = tool.search_and_read("machine learning basics", num_results=3)

for result in results:
    print(f"Title: {result['title']}")
    print(f"Content preview:\n{result['content'][:500]}...\n")
```

## API Reference

### `WebSearchTool(timeout=10, delay=1.0)`

Initialize the tool.

- `timeout`: Request timeout in seconds (default: 10)
- `delay`: Delay between requests in seconds (default: 1.0)

### `search(query, num_results=10)`

Search the web.

**Args:**
- `query` (str): Search query
- `num_results` (int): Number of results to return

**Returns:** List of dicts with keys: `title`, `url`, `snippet`

### `read_page(url)`

Extract content from a web page.

**Args:**
- `url` (str): URL to read

**Returns:** Extracted text content (str) or None if failed

### `search_and_read(query, num_results=3)`

Search and automatically read top results.

**Args:**
- `query` (str): Search query
- `num_results` (int): Number of results to read

**Returns:** List of dicts with keys: `title`, `url`, `snippet`, `content`

## Notes

- Uses DuckDuckGo for searching (no API key required)
- Includes respectful delays between requests
- Automatically extracts main content from pages
- Handles errors gracefully

## Example

Run the included examples:

```bash
python websearch.py
```

## License

Free to use for research and learning purposes.
