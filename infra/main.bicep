metadata description = 'Content Understanding RAG workshop MVP — resource-group-scoped, keyless, two-image Container Apps topology with Foundry (gpt-5 + text-embedding-3-large) in East US 2 and application/data in Southeast Asia.'

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@minLength(1)
@maxLength(20)
@description('azd environment name; seeds deterministic resource names.')
param environmentName string

@description('Location for all application and data resources.')
param location string = 'southeastasia'

@description('Location for the Microsoft Foundry account and model deployments.')
param foundryLocation string = 'eastus2'

@description('Frontend container image. Defaults to a public bootstrap image for the first provision.')
param frontendImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Shared backend container image for the API, worker, and cleanup job. Defaults to a public bootstrap image for the first provision.')
param backendImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Release identifier surfaced to the running containers.')
param releaseSha string = 'bootstrap'

@description('gpt-5 GlobalStandard capacity in thousands of tokens per minute.')
param gptCapacity int = 10

@description('text-embedding-3-large Standard capacity in thousands of tokens per minute.')
param embeddingCapacity int = 30

@description('Optional object id of the user or CI principal that runs the data-plane bootstrap. When set it receives the same data-plane roles as the application identity.')
param deploymentPrincipalId string = ''

@description('Optional GitHub owner. When both owner and repository are set, a deployment identity with an OIDC federated credential is created.')
param githubOwner string = ''

@description('Optional GitHub repository. When both owner and repository are set, a deployment identity with an OIDC federated credential is created.')
param githubRepository string = ''

@description('Extra tags applied to every resource.')
param tags object = {}

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var resourceToken = toLower(uniqueString(subscription().id, resourceGroup().id, environmentName))
var commonTags = union(tags, { 'azd-env-name': environmentName })

var enableGitHub = !empty(githubOwner) && !empty(githubRepository)
var enableBootstrap = !empty(deploymentPrincipalId)

// Resource names.
var storageAccountName = 'st${resourceToken}'
var searchName = 'srch-${resourceToken}'
var acrName = 'acr${resourceToken}'
var logAnalyticsName = 'log-${resourceToken}'
var appInsightsName = 'appi-${resourceToken}'
var managedEnvName = 'cae-${resourceToken}'
var foundryName = 'aif-${resourceToken}'
var appIdentityName = 'id-app-${resourceToken}'
var acrPullIdentityName = 'id-acrpull-${resourceToken}'
var githubIdentityName = 'id-github-${resourceToken}'
var frontendAppName = 'ca-frontend-${resourceToken}'
var apiAppName = 'ca-api-${resourceToken}'
var workerAppName = 'ca-worker-${resourceToken}'
var cleanupJobName = 'cj-cleanup-${resourceToken}'

// Data-plane object names (kept in sync with backend/app/core/config.py).
var uploadsContainer = 'uploads'
var derivedContainer = 'derived'
var controlContainer = 'control'
var ingestionQueue = 'ingestion'
var cleanupQueue = 'cu-result-cleanup'
var poisonQueue = 'ingestion-poison'
var tableName = 'workshop'
var searchIndexName = 'document-chunks'
var analyzerRouterId = 'business-document-router'
var chatDeployment = 'gpt-5'
var embeddingDeployment = 'text-embedding-3-large'
var embeddingDimensions = '3072'

// Built-in role definition ids (keyless, least privilege).
var roles = {
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  storageBlobDelegator: 'db58b8e5-c6ad-4a2a-8342-4190687cbf4a'
  storageQueueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  storageTableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  cognitiveServicesOpenAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  contentUnderstandingOwner: '4b42bd01-da42-4c92-9b07-15ea5bd6a602'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  acrPush: '8311e382-0749-4cb8-b61a-304f252e45ec'
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  rbacAdministrator: 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'
}

// Deterministic FQDNs computed from the environment domain to avoid a frontend/API dependency cycle.
var frontendFqdn = '${frontendAppName}.${managedEnv.outputs.defaultDomain}'
var apiFqdn = '${apiAppName}.${managedEnv.outputs.defaultDomain}'
var frontendOrigin = 'https://${frontendFqdn}'
var apiUpstream = 'https://${apiFqdn}'

// Shared backend environment (identical for API, worker, and cleanup job).
var backendEnv = [
  { name: 'APP_MODE', value: 'production' }
  { name: 'AZURE_CLIENT_ID', value: appIdentity.outputs.clientId }
  { name: 'STORAGE_ACCOUNT_NAME', value: storageAccountName }
  { name: 'UPLOADS_CONTAINER', value: uploadsContainer }
  { name: 'DERIVED_CONTAINER', value: derivedContainer }
  { name: 'CONTROL_CONTAINER', value: controlContainer }
  { name: 'INGESTION_QUEUE', value: ingestionQueue }
  { name: 'CONTENT_RESULT_CLEANUP_QUEUE', value: cleanupQueue }
  { name: 'INGESTION_POISON_QUEUE', value: poisonQueue }
  { name: 'TABLE_NAME', value: tableName }
  { name: 'SEARCH_ENDPOINT', value: search.outputs.endpoint }
  { name: 'SEARCH_INDEX_NAME', value: searchIndexName }
  { name: 'FOUNDRY_ENDPOINT', value: aiFoundry.properties.endpoint }
  { name: 'ANALYZER_ROUTER_ID', value: analyzerRouterId }
  { name: 'CHAT_DEPLOYMENT', value: chatDeployment }
  { name: 'EMBEDDING_DEPLOYMENT', value: embeddingDeployment }
  { name: 'EMBEDDING_DIMENSIONS', value: embeddingDimensions }
  { name: 'FRONTEND_ORIGIN', value: frontendOrigin }
  { name: 'RELEASE_SHA', value: releaseSha }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.outputs.connectionString }
]

// Backend resource sizing.
var backendResources = { cpu: json('0.5'), memory: '1Gi' }

// Identity resource ids computed from names so they are known at the start of deployment.
var appIdentityResourceId = resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', appIdentityName)
var acrPullIdentityResourceId = resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', acrPullIdentityName)

// Both application identities are attached to every compute resource; AZURE_CLIENT_ID selects the app identity for data-plane calls.
var computeIdentity = {
  type: 'UserAssigned'
  userAssignedIdentities: {
    '${appIdentityResourceId}': {}
    '${acrPullIdentityResourceId}': {}
  }
}
var registries = [
  { server: acr.outputs.loginServer, identity: acrPullIdentityResourceId }
]

// ---------------------------------------------------------------------------
// Identities (AVM)
// ---------------------------------------------------------------------------

module appIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'app-identity'
  params: {
    name: appIdentityName
    location: location
    tags: commonTags
  }
}

module acrPullIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'acrpull-identity'
  params: {
    name: acrPullIdentityName
    location: location
    tags: commonTags
  }
}

module githubIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = if (enableGitHub) {
  name: 'github-identity'
  params: {
    name: githubIdentityName
    location: location
    tags: commonTags
    federatedIdentityCredentials: [
      {
        name: 'github-actions-production'
        audiences: ['api://AzureADTokenExchange']
        issuer: 'https://token.actions.githubusercontent.com'
        subject: 'repo:${githubOwner}/${githubRepository}:environment:production'
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Observability (AVM)
// ---------------------------------------------------------------------------

module logAnalytics 'br/public:avm/res/operational-insights/workspace:0.16.1' = {
  name: 'log-analytics'
  params: {
    name: logAnalyticsName
    location: location
    tags: commonTags
    skuName: 'PerGB2018'
    dataRetention: 30
    dailyQuotaGb: '1'
  }
}

module appInsights 'br/public:avm/res/insights/component:0.8.0' = {
  name: 'app-insights'
  params: {
    name: appInsightsName
    location: location
    tags: commonTags
    workspaceResourceId: logAnalytics.outputs.resourceId
    applicationType: 'web'
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------------------
// Container registry (AVM, keyless: admin disabled, AcrPull by managed identity)
// ---------------------------------------------------------------------------

module acr 'br/public:avm/res/container-registry/registry:0.13.0' = {
  name: 'container-registry'
  params: {
    name: acrName
    location: location
    tags: commonTags
    acrSku: 'Basic'
    acrAdminUserEnabled: false
    roleAssignments: concat(
      [
        {
          roleDefinitionIdOrName: roles.acrPull
          principalId: acrPullIdentity.outputs.principalId
          principalType: 'ServicePrincipal'
        }
      ],
      // The GitHub deployment identity pushes release images to ACR from the GitHub-hosted runner.
      enableGitHub
        ? [
            {
              roleDefinitionIdOrName: roles.acrPush
              principalId: githubIdentity!.outputs.principalId
              principalType: 'ServicePrincipal'
            }
          ]
        : []
    )
  }
}

// ---------------------------------------------------------------------------
// Storage (raw: keyless account with blob/queue/table children, CORS, and 24h lifecycle)
// ---------------------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: storageAccountName
  location: location
  tags: commonTags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Allow', bypass: 'AzureServices' }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    cors: {
      corsRules: [
        {
          allowedOrigins: [frontendOrigin]
          allowedMethods: ['PUT', 'OPTIONS']
          allowedHeaders: ['content-type', 'x-ms-blob-type', 'x-ms-version']
          exposedHeaders: ['etag', 'x-ms-request-id']
          maxAgeInSeconds: 3600
        }
      ]
    }
  }
}

resource blobContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = [
  for name in [uploadsContainer, derivedContainer, controlContainer]: {
    parent: blobService
    name: name
    properties: { publicAccess: 'None' }
  }
]

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2024-01-01' = {
  parent: storage
  name: 'default'
}

resource storageQueues 'Microsoft.Storage/storageAccounts/queueServices/queues@2024-01-01' = [
  for name in [ingestionQueue, cleanupQueue, poisonQueue]: {
    parent: queueService
    name: name
  }
]

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2024-01-01' = {
  parent: storage
  name: 'default'
}

resource workshopTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2024-01-01' = {
  parent: tableService
  name: tableName
}

resource storageLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2024-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          enabled: true
          name: 'expire-application-data'
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'] }
            actions: { baseBlob: { delete: { daysAfterCreationGreaterThan: 1 } } }
          }
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Azure AI Search (AVM, keyless: local auth disabled, semantic + vector on Basic)
// ---------------------------------------------------------------------------

module search 'br/public:avm/res/search/search-service:0.13.0' = {
  name: 'search-service'
  params: {
    name: searchName
    location: location
    tags: commonTags
    sku: 'basic'
    partitionCount: 1
    replicaCount: 1
    semanticSearch: 'standard'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    roleAssignments: concat(
      [
        {
          roleDefinitionIdOrName: roles.searchIndexDataContributor
          principalId: appIdentity.outputs.principalId
          principalType: 'ServicePrincipal'
        }
        {
          roleDefinitionIdOrName: roles.searchServiceContributor
          principalId: appIdentity.outputs.principalId
          principalType: 'ServicePrincipal'
        }
      ],
      enableBootstrap
        ? [
            {
              roleDefinitionIdOrName: roles.searchServiceContributor
              principalId: deploymentPrincipalId
            }
            {
              roleDefinitionIdOrName: roles.searchIndexDataContributor
              principalId: deploymentPrincipalId
            }
          ]
        : [],
      // The GitHub deployment identity bootstraps the Search index during the deploy workflow.
      enableGitHub
        ? [
            {
              roleDefinitionIdOrName: roles.searchServiceContributor
              principalId: githubIdentity!.outputs.principalId
              principalType: 'ServicePrincipal'
            }
            {
              roleDefinitionIdOrName: roles.searchIndexDataContributor
              principalId: githubIdentity!.outputs.principalId
              principalType: 'ServicePrincipal'
            }
          ]
        : []
    )
  }
}

// ---------------------------------------------------------------------------
// Microsoft Foundry (raw: AIServices account, keyless, system identity, two model deployments)
// ---------------------------------------------------------------------------

resource aiFoundry 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: foundryName
  location: foundryLocation
  tags: commonTags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: foundryName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
    networkAcls: { defaultAction: 'Allow' }
  }
}

resource gptDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiFoundry
  name: chatDeployment
  sku: { name: 'GlobalStandard', capacity: gptCapacity }
  properties: {
    model: { format: 'OpenAI', name: chatDeployment }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}

resource embeddingModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiFoundry
  name: embeddingDeployment
  sku: { name: 'Standard', capacity: embeddingCapacity }
  properties: {
    model: { format: 'OpenAI', name: embeddingDeployment }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
  // Cognitive Services serializes deployment creation on a single account.
  dependsOn: [gptDeployment]
}

// ---------------------------------------------------------------------------
// Role assignments (raw, explicit, keyless)
// ---------------------------------------------------------------------------

resource storageAppRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in [
    roles.storageBlobDataContributor
    roles.storageBlobDelegator
    roles.storageQueueDataContributor
    roles.storageTableDataContributor
  ]: {
    name: guid(storage.id, appIdentityName, roleId)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: appIdentity.outputs.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource storageBootstrapRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in (enableBootstrap ? [roles.storageBlobDelegator, roles.storageBlobDataContributor] : []): {
    name: guid(storage.id, deploymentPrincipalId, roleId)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: deploymentPrincipalId
    }
  }
]

resource foundryAppRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in [roles.cognitiveServicesOpenAiUser, roles.contentUnderstandingOwner]: {
    name: guid(aiFoundry.id, appIdentityName, roleId)
    scope: aiFoundry
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: appIdentity.outputs.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

// Content Understanding invokes the attached model deployments using the account's own identity.
resource foundrySelfRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiFoundry.id, 'self', roles.cognitiveServicesOpenAiUser)
  scope: aiFoundry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.cognitiveServicesOpenAiUser
    )
    principalId: aiFoundry.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryBootstrapRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in (enableBootstrap ? [roles.cognitiveServicesOpenAiUser, roles.contentUnderstandingOwner] : []): {
    name: guid(aiFoundry.id, deploymentPrincipalId, roleId)
    scope: aiFoundry
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: deploymentPrincipalId
    }
  }
]

// The GitHub deployment identity bootstraps Content Understanding defaults during the deploy workflow.
resource githubFoundryRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in (enableGitHub ? [roles.cognitiveServicesOpenAiUser, roles.contentUnderstandingOwner] : []): {
    name: guid(aiFoundry.id, githubIdentityName, roleId)
    scope: aiFoundry
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: githubIdentity!.outputs.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource githubResourceGroupRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in (enableGitHub ? [roles.contributor, roles.rbacAdministrator] : []): {
    name: guid(resourceGroup().id, githubIdentityName, roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: githubIdentity!.outputs.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

// ---------------------------------------------------------------------------
// Container Apps environment (AVM)
// ---------------------------------------------------------------------------

module managedEnv 'br/public:avm/res/app/managed-environment:0.15.0' = {
  name: 'managed-environment'
  params: {
    name: managedEnvName
    location: location
    tags: commonTags
    zoneRedundant: false
    appInsightsConnectionString: appInsights.outputs.connectionString
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Frontend Container App (public ingress, same-origin /api proxy)
// ---------------------------------------------------------------------------

resource frontendApp 'Microsoft.App/containerApps@2025-01-01' = {
  name: frontendAppName
  location: location
  tags: union(commonTags, { 'azd-service-name': 'frontend' })
  identity: computeIdentity
  dependsOn: [appIdentity]
  properties: {
    managedEnvironmentId: managedEnv.outputs.resourceId
    configuration: {
      activeRevisionsMode: 'Multiple'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        traffic: [{ latestRevision: true, weight: 100 }]
      }
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'frontend'
          image: frontendImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'API_UPSTREAM', value: apiUpstream }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8080 }
              periodSeconds: 30
              failureThreshold: 3
              timeoutSeconds: 5
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 8080 }
              periodSeconds: 15
              failureThreshold: 6
              timeoutSeconds: 5
            }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

// ---------------------------------------------------------------------------
// API Container App (public ingress for MVP simplicity; CORS restricted to the frontend origin)
// ---------------------------------------------------------------------------

resource apiApp 'Microsoft.App/containerApps@2025-01-01' = {
  name: apiAppName
  location: location
  tags: union(commonTags, { 'azd-service-name': 'api' })
  identity: computeIdentity
  properties: {
    managedEnvironmentId: managedEnv.outputs.resourceId
    configuration: {
      activeRevisionsMode: 'Multiple'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        traffic: [{ latestRevision: true, weight: 100 }]
        corsPolicy: {
          allowedOrigins: [frontendOrigin]
          allowedMethods: ['GET', 'POST', 'DELETE', 'OPTIONS']
          allowedHeaders: ['*']
          allowCredentials: true
        }
      }
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'api'
          image: backendImage
          command: ['api']
          resources: backendResources
          env: backendEnv
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health/live', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 30
              failureThreshold: 3
              timeoutSeconds: 5
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health/ready', port: 8000 }
              initialDelaySeconds: 5
              periodSeconds: 15
              failureThreshold: 6
              timeoutSeconds: 5
            }
            {
              type: 'Startup'
              httpGet: { path: '/health/live', port: 8000 }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 30
              timeoutSeconds: 5
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
        rules: [
          { name: 'http', http: { metadata: { concurrentRequests: '20' } } }
        ]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Worker Container App (no ingress, KEDA queue scaling by managed identity)
// ---------------------------------------------------------------------------

resource workerApp 'Microsoft.App/containerApps@2025-01-01' = {
  name: workerAppName
  location: location
  tags: commonTags
  identity: computeIdentity
  properties: {
    managedEnvironmentId: managedEnv.outputs.resourceId
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: backendImage
          command: ['worker']
          resources: backendResources
          env: backendEnv
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 3
        rules: [
          {
            name: 'ingestion'
            azureQueue: {
              accountName: storageAccountName
              queueName: ingestionQueue
              queueLength: 16
              identity: appIdentityResourceId
            }
          }
          {
            name: 'cu-result-cleanup'
            azureQueue: {
              accountName: storageAccountName
              queueName: cleanupQueue
              queueLength: 8
              identity: appIdentityResourceId
            }
          }
        ]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Cleanup Container Apps Job (hourly schedule)
// ---------------------------------------------------------------------------

resource cleanupJob 'Microsoft.App/jobs@2025-01-01' = {
  name: cleanupJobName
  location: location
  tags: commonTags
  identity: computeIdentity
  properties: {
    environmentId: managedEnv.outputs.resourceId
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 1800
      replicaRetryLimit: 1
      scheduleTriggerConfig: {
        cronExpression: '0 * * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'cleanup'
          image: backendImage
          command: ['cleanup']
          resources: backendResources
          env: backendEnv
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output AZURE_LOCATION string = location
output AZURE_FOUNDRY_LOCATION string = foundryLocation
output AZURE_RESOURCE_GROUP string = resourceGroup().name

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.outputs.loginServer
output AZURE_CONTAINER_REGISTRY_NAME string = acr.outputs.name

output STORAGE_ACCOUNT_NAME string = storage.name
output SEARCH_ENDPOINT string = search.outputs.endpoint
output SEARCH_SERVICE_NAME string = search.outputs.name
output SEARCH_INDEX_NAME string = searchIndexName

output FOUNDRY_ENDPOINT string = aiFoundry.properties.endpoint
output FOUNDRY_ACCOUNT_NAME string = aiFoundry.name
output CHAT_DEPLOYMENT string = gptDeployment.name
output EMBEDDING_DEPLOYMENT string = embeddingModelDeployment.name
output EMBEDDING_DIMENSIONS string = embeddingDimensions
output ANALYZER_ROUTER_ID string = analyzerRouterId

output CONTAINER_APPS_ENVIRONMENT_NAME string = managedEnv.outputs.name
output FRONTEND_CONTAINER_APP_NAME string = frontendApp.name
output API_CONTAINER_APP_NAME string = apiApp.name
output WORKER_CONTAINER_APP_NAME string = workerApp.name
output CLEANUP_JOB_NAME string = cleanupJob.name

output FRONTEND_URL string = 'https://${frontendApp.properties.configuration.ingress.fqdn}'
output API_URL string = 'https://${apiApp.properties.configuration.ingress.fqdn}'
output FRONTEND_FQDN string = frontendApp.properties.configuration.ingress.fqdn
output API_FQDN string = apiApp.properties.configuration.ingress.fqdn

output APPLICATIONINSIGHTS_CONNECTION_STRING string = appInsights.outputs.connectionString
output APP_IDENTITY_CLIENT_ID string = appIdentity.outputs.clientId
output APP_IDENTITY_PRINCIPAL_ID string = appIdentity.outputs.principalId
output APP_IDENTITY_RESOURCE_ID string = appIdentity.outputs.resourceId
output ACR_PULL_IDENTITY_RESOURCE_ID string = acrPullIdentity.outputs.resourceId

output GITHUB_IDENTITY_CLIENT_ID string = enableGitHub ? githubIdentity!.outputs.clientId : ''
output GITHUB_IDENTITY_PRINCIPAL_ID string = enableGitHub ? githubIdentity!.outputs.principalId : ''
