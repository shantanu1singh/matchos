param location string = resourceGroup().location
param appPrincipalId string
param operatorPrincipalId string

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'matchos${uniqueString(resourceGroup().id)}'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
  }
}
resource service 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: { enabled: true, days: 7 }
  }
}
resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: service
  name: 'evaluations'
  properties: { publicAccess: 'None' }
}
resource appReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(container.id, appPrincipalId, 'ReportsReader')
  scope: container
  properties: {
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
  }
}
resource operatorWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(container.id, operatorPrincipalId, 'ReportsWriter')
  scope: container
  properties: {
    principalId: operatorPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}
output containerUrl string = '${storage.properties.primaryEndpoints.blob}evaluations'
