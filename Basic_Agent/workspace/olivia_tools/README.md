# 🛠️ Olivia's Tool Suite

A collection of tools for research, automation, and productivity.

## 📦 Available Tools

### WebSearch Tool
Search the web and read web pages for research.

**Location:** `tools/websearch/`

**Features:**
- Search the web using DuckDuckGo (no API key needed)
- Read and extract content from web pages
- Combine search and read operations

**Usage:**
```python
from tools import WebSearchTool

# Initialize the tool
tool = WebSearchTool()

# Search for something
results = tool.search("Python web scraping")

# Read a web page
content = tool.read_page("https://example.com")

# Search and read top results
data = tool.search_and_read("machine learning", num_results=3)
```

## 📋 Tool Directory Structure

```
tools/
├── __init__.py           # Main package init (loads all tools)
├── loader.py             # Tool loader utility
├── README.md             # This file
└── websearch/            # WebSearch tool
    ├── __init__.py       # WebSearch package init
    ├── websearch.py      # Main WebSearch implementation
    ├── requirements.txt  # Dependencies
    └── README.md         # WebSearch documentation
```

## 🚀 Quick Start

```bash
# Tools are already installed with dependencies
# Import and use directly:

python -c "from tools import WebSearchTool; tool = WebSearchTool(); print(tool.search('example'))"
```

## 🔧 Adding New Tools

1. Create a new directory: `tools/new_tool/`
2. Add `__init__.py` and your tool files
3. Update `tools/__init__.py` to import the new tool
4. Update `tools/loader.py` to include the new tool in `load_all_tools()`

## ✅ Verification

All tools are loaded and ready to use:
- ✅ WebSearchTool - Functional
- ✅ Dependencies installed
- ✅ Package structure organized
