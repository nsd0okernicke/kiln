# Repository mode

Prefer observable project evidence over questions. Read the relevant files identified by the
inventory, then inspect the surrounding source and test layout where it changes the result.

Establish, where evidence exists:

- project purpose and supported capabilities from primary project documentation;
- languages, runtime versions, dependency managers, and framework versions;
- build, unit, integration, acceptance, property, coverage, mutation, lint, format, and
  type-check commands;
- CI commands and platform/runtime matrices;
- source modules, bounded contexts, dependency direction, and public boundaries;
- persistence, messaging, network services, and other external integrations;
- quality gates and contribution rules actually enforced by configuration or CI.

Treat a command as established only when a manifest, wrapper, configuration file, CI workflow,
or maintained contributor document supports it. Do not turn an installed tool, an old report,
or a dependency name alone into a project rule.

Ask the user only when evidence is absent, contradictory, or describes mechanics without the
intent needed for a durable rule. Name the conflicting sources in the question and recommend
the answer best supported by the repository.

For an incomplete repository, retain every supported fact and switch to interview mode only for
the gaps. Do not replace evidence with generic examples.
