param location string = resourceGroup().location
param appName string = 'matchos-prod'
@description('Immutable registry digest or unique build tag; never latest.')
param image string
param environmentId string
param identityId string
param identityClientId string
param registryServer string
param vaultUri string
param foundryEndpoint string
param decisionDeployment string = 'Decision-1'
param reasoningDeployment string = 'gpt-6-luna'
param speechEnabled bool = false
param speechRegion string = 'eastus2'
param speechModel string = 'MAI-Voice-2.1-Flash'
param basicAuthEnabled bool = true
param basicAuthUsername string = 'judges'
param reportsContainerUrl string = ''

resource app 'Microsoft.App/containerApps@2025-07-01' = {
  name: appName
  location: location
  tags: { application: 'matchos', environment: 'production', managedBy: 'bicep' }
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        stickySessions: { affinity: 'sticky' }
      }
      registries: [{ server: registryServer, identity: identityId }]
      secrets: concat([
        { name: 'appinsights', keyVaultUrl: '${vaultUri}secrets/applicationinsights-connection-string', identity: identityId }
      ], speechEnabled ? [
        { name: 'speech-key', keyVaultUrl: '${vaultUri}secrets/azure-speech-key', identity: identityId }
      ] : [], basicAuthEnabled ? [
        { name: 'access-password', keyVaultUrl: '${vaultUri}secrets/matchos-basic-auth-password', identity: identityId }
      ] : [])
    }
    template: {
      terminationGracePeriodSeconds: 45
      containers: [{
        name: 'matchos'
        image: image
        resources: { cpu: json('1.0'), memory: '2Gi' }
        env: concat([
          { name: 'AZURE_CLIENT_ID', value: identityClientId }
          { name: 'AZURE_AUTH_MODE', value: 'entra' }
          { name: 'AZURE_FOUNDRY_ENDPOINT', value: foundryEndpoint }
          { name: 'AZURE_DECISION_DEPLOYMENT', value: decisionDeployment }
          { name: 'AZURE_REASONING_DEPLOYMENT', value: reasoningDeployment }
          { name: 'DECISION_PROVIDER', value: 'azure' }
          { name: 'REASONING_PROVIDER', value: 'azure' }
          { name: 'DECISION_TIMEOUT_S', value: '20' }
          { name: 'REASONING_TIMEOUT_S', value: '30' }
          { name: 'MATCHOS_MAX_SESSIONS', value: '8' }
          { name: 'MATCHOS_MODEL_LOG_CONTENT', value: '0' }
          { name: 'MATCHOS_OTEL_ENABLED', value: '1' }
          { name: 'MATCHOS_TRACE_MODE', value: 'compact' }
          { name: 'MATCHOS_TRACE_CONTENT', value: '0' }
          { name: 'OTEL_SERVICE_NAME', value: 'matchos-prod' }
          { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights' }
          { name: 'MATCHOS_SPEECH_ENABLED', value: speechEnabled ? '1' : '0' }
          { name: 'AZURE_SPEECH_REGION', value: speechRegion }
          { name: 'AZURE_SPEECH_MODEL', value: speechModel }
          { name: 'MATCHOS_BASIC_AUTH_ENABLED', value: basicAuthEnabled ? '1' : '0' }
          { name: 'MATCHOS_BASIC_AUTH_USERNAME', value: basicAuthUsername }
          { name: 'MATCHOS_REPORTS_CONTAINER_URL', value: reportsContainerUrl }
          { name: 'MATCHOS_BASIC_AUTH_SECURE_COOKIE', value: '1' }
        ], speechEnabled ? [{ name: 'AZURE_SPEECH_KEY', secretRef: 'speech-key' }] : [],
          basicAuthEnabled ? [{ name: 'MATCHOS_BASIC_AUTH_PASSWORD', secretRef: 'access-password' }] : [])
        probes: [for kind in ['Startup', 'Readiness', 'Liveness']: {
          type: kind
          httpGet: { path: '/api/health', port: 8000, scheme: 'HTTP' }
          initialDelaySeconds: kind == 'Liveness' ? 15 : 2
          periodSeconds: kind == 'Liveness' ? 30 : 5
          timeoutSeconds: 3
          failureThreshold: kind == 'Startup' ? 30 : 3
        }]
      }]
      // In-memory speech registration and sessions need one process and replica.
      // Do not increase until shared state and reconnect restoration are implemented.
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}
output url string = 'https://${app.properties.configuration.ingress.fqdn}'
output appId string = app.id
