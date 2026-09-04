using './main.bicep'

// Non-secret defaults. The deployment scripts (Task 16) override the image parameters with
// immutable ACR digests; the first provision uses the public bootstrap images.
param environmentName = readEnvironmentVariable('AZURE_ENV_NAME', 'cudemo')
param location = readEnvironmentVariable('AZURE_LOCATION', 'southeastasia')
param foundryLocation = readEnvironmentVariable('AZURE_FOUNDRY_LOCATION', 'eastus2')
param frontendImage = readEnvironmentVariable('AZURE_FRONTEND_IMAGE', 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest')
param backendImage = readEnvironmentVariable('AZURE_BACKEND_IMAGE', 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest')
param releaseSha = readEnvironmentVariable('AZURE_RELEASE_SHA', 'bootstrap')
param deploymentPrincipalId = readEnvironmentVariable('AZURE_PRINCIPAL_ID', '')
param githubOwner = readEnvironmentVariable('AZURE_GITHUB_OWNER', '')
param githubRepository = readEnvironmentVariable('AZURE_GITHUB_REPOSITORY', '')
param githubOwnerId = readEnvironmentVariable('AZURE_GITHUB_OWNER_ID', '')
param githubRepositoryId = readEnvironmentVariable('AZURE_GITHUB_REPOSITORY_ID', '')
