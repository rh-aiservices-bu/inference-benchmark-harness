# Writing walkthroughs

Write for an operator who needs to run the next step and understand its result.

- State the question the experiment answers.
- Name the actor, command or configuration field that changes something.
- Give one action per numbered step, followed by the expected result and stop condition.
- Use short sentences. Avoid semicolon chains and unnecessary lead-ins such as “Key takeaway:”. Keep punctuation required by code and metric names.
- For comparisons, state what stays fixed, what changes and what measurement determines the next step.
- Name the metric, statistic, unit and population. Use `time_to_first_token.p95` in milliseconds for a particular workload and repeat, rather than “latency”.
- Label example values, observed results and unverified assumptions separately. Name the tested version when behavior depends on it.
- Distinguish valid evidence from a met goal. Keep failures and limits visible.
- Link to the command, configuration or source of truth instead of repeating its contents.
- Keep credentials, customer details and personal meeting notes out of public examples.

## Before publishing

1. Preview documented commands without model traffic. Check paths, field names and relative links.
2. Match claims to the code and retained evidence. A fixture pass does not qualify a serving deployment.
3. Keep code blocks unchanged during a prose-only edit. If a command changes, validate that command separately.
4. Read the steps aloud. Remove wording that adds no action, evidence or necessary limit.
