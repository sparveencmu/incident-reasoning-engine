---
name: log-parser
description: >
  Advanced log parsing and stack-trace extraction instructions. Use this skill
  when parsing complicated stack traces, resolving log messages, or searching
  error logs.
metadata:
  version: 1.0.0
---

# Log Parser Instructions

Use this guide to analyze and extract information from backend stack traces and error logs.

## Log Analysis Workflow
1. Identify the primary Exception or Error Class (e.g. `ConnectionError`, `RuntimeError`).
2. Identify the failing line of code (module name, file name, line number).
3. Check for external resource bottlenecks (e.g. timeout on a database query, HTTP connection failure).
4. Verify if any sensitive data was present in the log and trace its origin.
