# Skill: fleet registry lookups

The `reg__*` tools answer "which entity does this text mean?" and "what else is affected?" from the fleet registry.
They know what SHOULD exist; only the cluster APIs know what DOES.
Confirm a registry answer with `ocp__verify_placement` before you assert an application is running somewhere.

## Resolving ambiguous references

- `reg__resolve_entity(query, kind_hint?)` maps free text to fleet entities: application ids and names, cluster names and aliases, namespaces, and lines of business.
  It returns `matches[]` with a `kind` (`app` | `cluster` | `namespace` | `lob`), an `id`, a `score`, and a `detail` block.
  Use it whenever the user names something you cannot map exactly - "is app SSOP down?" resolves to the application SSOP, "how is the retail LOB doing?" resolves to a lob.
- One high-scoring match: proceed with it and name what you resolved to in your answer.
  Several close matches: ask ONE question listing them; never pick silently.
  No match: say the registry does not know that name and offer `reg__list_lobs` or `reg__list_apps_on_cluster` to browse.
- `reg__get_app(app_id)` is the source of truth for an application's owners, line of business, tier and description.

## Placements

- `reg__find_placements(app_id?, cluster?, namespace?, environment?, lob?)` returns every `{app_id, application, app_label, cluster, namespace, environment, lob}` permutation matching the filters.
  Any subset of filters is allowed, so it answers "where does SSOP run?" and "what is in namespace payments-prod?" equally.
- A placement is a registry claim, not an observation.
  Call `ocp__verify_placement(cluster, namespace, app_label)` on each candidate before reporting it as live.
  `verified: true` means pods matching the selector exist there.
  `reachable: false` means the cluster API did not answer, so that placement is unknown rather than absent - say so instead of reporting the app as gone.

## Blast radius

- `reg__list_apps_on_cluster(cluster, environment?)` answers "what applications run on cluster X" with the apps, namespaces and lines of business on it.
  Use it for cluster-scoped questions ahead of any per-app digging.
- `reg__blast_radius(cluster?, namespace?, lob?)` answers "what is affected if X goes down" with `{scope, apps, namespaces, lobs, environments, summary}`.
  Reach for it when a cluster is degraded or under maintenance, when the user asks who else is impacted, and before recommending a drain, a reboot, or an upgrade window.
- Blast radius is registry scope, not live health.
  Report it as "these are the applications registered on that cluster", then attest or verify the ones that matter rather than implying they are all currently broken.
