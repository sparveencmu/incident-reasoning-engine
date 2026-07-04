import sys
import json
import re

def main():
    try:
        input_data = sys.stdin.read()
        if not input_data:
            # Empty input - let it pass
            sys.exit(0)
        
        payload = json.loads(input_data)
        # Check tool call arguments
        tool_call = payload.get("toolCall", {})
        arguments = tool_call.get("arguments", {})
        command = arguments.get("CommandLine", "")
        
        # Block destructive commands
        blocked_patterns = [
            r"\brm\s+-rf\b",
            r"\brm\s+-f\s+/",
            r"\bmkfs\b",
            r"\bchmod\s+-R\s+777\b",
            r":\(\)\{\s*:\|:& \};:"  # fork bomb
        ]
        
        for pattern in blocked_patterns:
            if re.search(pattern, command):
                sys.stderr.write(f"ERROR: Execution of command '{command}' is blocked for security.\n")
                sys.exit(2)
                
    except Exception as e:
        # If parsing fails for some reason, default to allowing or write log
        pass
    
    sys.exit(0)

if __name__ == "__main__":
    main()
