# Keel agent entry point

For client enrollment or setup under a Google account, start with [the operator runbook](docs/agent-onboarding.md). Use its existing-server path for a new client and its fresh-project path for a new server. A new client can use the current subscription independently of its Google account.

Use the target owner's Google identity, project, credit status and device label. Keep credentials, subscription URLs, generated profiles, payment details and local configuration outside Git. Pass the user only the private client delivery file, the dashboard link, the device label and verification results. For a credit-only task, establish the billing protection in the runbook before creating resources.

For repository changes, keep the main working directory on trunk, edit in a fresh linked worktree, and remove it after its changes are preserved. Synchronize both README files for user-visible changes, update Unreleased if a CHANGELOG exists, and run the documented tests. Use `docs-sync` and its verifier when installed. Follow the repository owner's release workflow before tagging a release.
