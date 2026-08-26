# Skill: Application 360 analysis

How to author the narrative fields of the Application 360 report.

- You only write four things: the Executive Summary, per-section findings notes, Recommendations, and the Final Assessment reason. Every fact table row is already populated by deterministic checks; never contradict or restate a table wholesale.
- The Executive Summary is 1-3 sentences: overall posture, the single most important finding, and whether the platform contributes.
- Findings notes exist only where a section has something worth saying (a failing or warning check, or a notable pattern). A healthy section gets no note.
- Recommendations are numbered, concrete, and actionable by the reader: name the resource and the change ("raise the memory limit on payments-api from 512Mi toward 768Mi"), not generalities ("consider reviewing resources").
- The Final Assessment status is computed for you (Healthy / At Risk / Critical); write the 1-3 sentence reason grounded in the failing checks, and propose the next review date.
- Cross-attribute honestly: when the host cluster attested degraded or in maintenance, weave that into the summary and findings so an application team does not chase a platform problem.
