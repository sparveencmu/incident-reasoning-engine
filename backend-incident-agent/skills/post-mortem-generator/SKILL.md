---
name: post-mortem-generator
description: >
  Generate standardized post-mortem and incident summary reports. Use this skill
  when creating a summary document or SRE incident report after mitigation.
metadata:
  version: 1.0.0
---

# Post-Mortem Generator Instructions

Use this guide to generate structured SRE post-mortem reports.

## Structure
Each post-mortem report must include:
1. **Title**: Service Name - Incident Summary (Date)
2. **Status**: Mitigated / Resolved
3. **Severity**: Severity level of the incident
4. **Root Cause**: Explanation of what went wrong
5. **Mitigation Steps**: Details of the action taken
6. **Action Items**: Long-term prevention steps
7. **Redacted Data**: Summarize categories of PII redacted (SSNs, CCs, etc.) during processing.
