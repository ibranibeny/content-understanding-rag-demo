using '../main.bicep'

// Fixed values that also exercise the optional bootstrap and GitHub-deployment paths so the
// policy test compiles every conditional resource.
param environmentName = 'test'
param location = 'southeastasia'
param foundryLocation = 'eastus2'
param frontendImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
param backendImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
param releaseSha = 'test'
param deploymentPrincipalId = '00000000-0000-0000-0000-000000000001'
param githubOwner = 'contoso'
param githubRepository = 'content-understanding-rag-demo'
param githubOwnerId = '12345678'
param githubRepositoryId = '87654321'
