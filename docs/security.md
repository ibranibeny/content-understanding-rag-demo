# Security and data-handling model

This explanation defines the MVP workshop boundary. It is not approval for confidential workloads.

## Data classification

Use synthetic, public, non-sensitive data only. Do not upload confidential, regulated, personal, customer, production, export-controlled, or credential-bearing documents. Anonymous sessions are a workshop isolation mechanism, not enterprise identity or authorization.

## Regional data flow

The frontend, API, worker, cleanup job, Storage, Azure AI Search, and ACR are deployed in Southeast Asia. Microsoft Foundry, Content Understanding, `gpt-5`, and `text-embedding-3-large` are deployed in East US 2. Document content and derived evidence cross from Southeast Asia to East US 2 for AI processing and return for indexing or display. Do not use the workshop where that transfer is not approved.

## Identity and secrets

- Azure runtime access uses managed identities and Microsoft Entra tokens.
- Browser uploads use a short-lived, one-blob user-delegation SAS with create/write only.
- Storage shared-key access, Search local authentication, Foundry key access, and ACR admin access are disabled by infrastructure policy.
- GitHub deployment uses an environment-scoped OIDC federated credential. No Azure client secret is required or stored.
- Never log cookies, authorization headers, SAS query strings, tokens, document content, extraction JSON, full questions, or prompts.

## Isolation, retention, and deletion

Session tokens are random, stored only as SHA-256 hashes server-side, and sent as secure HTTP-only cookies in production. Search requests enforce a server-built session filter and then recheck document lifecycle before evidence reaches `gpt-5`. Deletion first tombstones the record, making it invisible, then a lease-fenced cleanup removes Blob and Search artifacts. Sessions expire after 24 hours; deleted tombstones are retained briefly for safe convergence before purge.

## AI grounding boundary

The application runtime model is fixed to `gpt-5`; embeddings use `text-embedding-3-large` with 3,072 dimensions. Retrieved document text is delimited as untrusted evidence, not inserted into system instructions. Citation IDs are assigned and validated by the server. The application can still make mistakes: verify citations and source content before relying on an answer.

## Software supply chain and delivery

Python packages resolve through the Microsoft enterprise feed declared in `backend/pyproject.toml`. CI runs backend/frontend checks and Bicep policy tests. CodeQL analyzes Python and JavaScript/TypeScript. The GitHub ruleset can request Copilot review focused on session isolation, Search filters, SAS leakage, keyless access, prompt injection, citations, idempotency, Bicep-only infrastructure, and the `gpt-5` lock. Container images are deployed by immutable digest after ACR Tasks build them.

## MVP limitations

- The API is public with CORS restricted to the frontend origin in the simplified MVP.
- Deployment is a single-revision rollout; candidate labels, traffic shifting, and automated rollback state-machine behavior were descoped.
- Live ACR build, Azure provisioning, role propagation, GitHub OIDC exchange, and end-to-end deployment evidence are Task 19 gates.
- This workshop has no enterprise user sign-in, private networking, customer-managed keys, DLP, malware scanning, or formal compliance controls.

For deployment and role/quota recovery, use [the deployment guide](deployment.md).
