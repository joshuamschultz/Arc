"""
Olivia's Tool Loader
Loads and initializes all available tools
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import olivia_tools
sys.path.insert(0, str(Path(__file__).parent.parent))

def load_all_tools():
    """Load all available tools"""
    from olivia_tools.websearch import WebSearchTool
    
    tools = {
        'websearch': WebSearchTool()
    }
    
    return tools

def get_tool(tool_name):
    """Get a specific tool by name"""
    tools = load_all_tools()
    if tool_name not in tools:
        raise ValueError(f"Tool '{tool_name}' not found. Available tools: {list(tools.keys())}")
    return tools[tool_name]

if __name__ == '__main__':
    print("Loading Olivia's Tools...")
    tools = load_all_tools()
    print(f"✅ Loaded {len(tools)} tool(s):")
    for name, tool in tools.items():
        print(f"   - {name}: {tool.__class__.__name__}")
