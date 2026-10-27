targetScope = 'resourceGroup'

param location string = resourceGroup().location
param appName string = 'matchos-prod'
@description('Object ID of the operator who will upload secrets; never a client ID.')
param operatorPrincipalId string
param foundryResourceGroup string
param foundryAccountName string

var suffix = uniqueString(resourceGroup().id)
var tags = { application: 'matchos', environment: 'production', managedBy: 'bicep' }

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'matchos${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${appName}-identity'
  location: location
  tags: tags
}
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'AcrPull')
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'matchos-${suffix}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
    accessPolicies: []
    publicNetworkAccess: 'Enabled'
  }
}
resource readSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, identity.id, 'KeyVaultSecretsUser')
  scope: vault
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}
resource writeSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, operatorPrincipalId, 'KeyVaultSecretsOfficer')
  scope: vault
  properties: {
    principalId: operatorPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  }
}
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${appName}-logs'
  location: location
  tags: tags
  properties: {
    retentionInDays: 30
    sku: { name: 'PerGB2018' }
    workspaceCapping: { dailyQuotaGb: 1 }
  }
}
resource environment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: '${appName}-env'
  location: location
  tags: tags
  properties: {
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}
module inference 'foundry-access.bicep' = {
  name: '${appName}-foundry-access'
  scope: resourceGroup(foundryResourceGroup)
  params: {
    accountName: foundryAccountName
    principalId: identity.properties.principalId
  }
}
output registryName string = registry.name
output registryServer string = registry.properties.loginServer
output identityId string = identity.id
output identityClientId string = identity.properties.clientId
output vaultName string = vault.name
output vaultUri string = vault.properties.vaultUri
output environmentId string = environment.id
output workspaceId string = logs.id

module reports 'reports.bicep' = {
  name: '${appName}-reports'
  params: {
    location: location
    appPrincipalId: identity.properties.principalId
    operatorPrincipalId: operatorPrincipalId
  }
}
output reportsContainerUrl string = reports.outputs.containerUrl
