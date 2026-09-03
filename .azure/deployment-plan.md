# Azure Deployment Plan

**Status:** In Progress

## Goal
Deploy the functional Content Understanding RAG workshop MVP.

## Scope
- Frontend React container on Azure Container Apps
- FastAPI API and queue worker on Azure Container Apps
- Azure Storage for uploads, state, and queues
- Azure AI Search for vector/hybrid retrieval
- Microsoft Foundry in East US 2 with `gpt-5` and `text-embedding-3-large`
- Application resources in Southeast Asia
- Managed identities and keyless runtime access
- Azure Container Registry
- Application Insights / Log Analytics

## Deployment method
- Azure Developer CLI (`azd`)
- Bicep only
- Docker images for frontend and shared backend

## Functional gate
Upload a fixture, extract with Content Understanding, index it, ask a grounded question through GPT-5, verify a citation, then delete the fixture.

## Security
Managed Identity for runtime services, GitHub OIDC for CI/CD, no Azure API keys or client secrets in source control.

## Validation
Compile Bicep, validate configuration and role assignments, build containers, then run Azure preflight before deployment.
