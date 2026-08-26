# Skill: cluster health triage

How to interpret an attestation result and carry it into the conversation.

- Verdict meanings: `healthy` means every check passed; `maintenance` means expected change activity (upgrade progressing, MachineConfigPool updating, cordoned nodes) with nothing broken; `degraded` means a critical check failed and needs attention; `unattestable` means the monitoring pipeline itself cannot be trusted, so treat every other signal from that cluster as suspect and say so.
- Maintenance is context, not an alarm. During a node roll, transient pod restarts, single-replica blips, and router hiccups are expected noise; re-score application symptoms accordingly and say when the roll should settle.
- A degraded ingress operator explains user-facing 503s and route flakiness. A degraded monitoring operator caps your confidence. etcd or kube-apiserver problems explain nearly everything downstream; lead with them when present.
- When a symptom is platform-attributable, say so explicitly, recommend escalation to the platform team, and list the exact evidence rows they will want (operator, condition, since-when, alert names). Do not continue app-level digging as if the platform were healthy.
- When re-attestation shows a change from the cached verdict (recovered or newly degraded), lead your reply with that change before answering the user's question.
