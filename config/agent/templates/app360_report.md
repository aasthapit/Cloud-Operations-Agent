# Application 360 report template

This is the render skeleton for the organization's 18-section Application
360 report (verbatim structure in docs/reference/source-checklists.md).
The console renders the typed report JSON directly; this template defines
the Markdown EXPORT shape and reminds the LLM which fields are narrative.

Narrative fields (LLM-authored, grounded in check results only):

- `executive_summary` - 1-3 sentences
- per-section `findings` - only where checks failed/warned or a pattern matters
- `recommendations` - numbered list of 1-3 concrete actions
- `final_assessment.reason` - 1-3 sentences; status itself is computed

Export skeleton:

```markdown
# OpenShift Application 360 Report

## 1. Executive Summary
- Application Name: {application}
- Namespace: {namespace}
- Cluster: {cluster}
- Environment: {environment}
- Business Owner: {owners.business}
- Technical Owner: {owners.technical}
- Support Team: {owners.support_team}
- Report Date: {report_date}
- Overall Status: {overall_status}
- Summary: {executive_summary}

## <n>. <Section title>            (sections 2-16)
| Item | Value |
|---|---|
| <check name> | <observed> - <status> |
...
**Findings**
- <findings note, when present>

## 17. Recommendations
1. <recommendation>

## 18. Final Assessment
- Status: {overall_status}
- Reason: {final_assessment.reason}
- Next Review Date: {next_review_date}
```
