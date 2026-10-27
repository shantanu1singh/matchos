# Run MatchOS on Azure

MatchOS runs as one Python application in Azure Container Apps. The same container serves the Watch, Broadcast, Evaluations, Telemetry and Lab pages, WebSocket match playback, the standalone graphics window and speech requests. No separate frontend host is required.

The demo environment uses `rg-matchos-prod` in East US 2. Its shared-password endpoint is [Open MatchOS](https://matchos-prod.niceground-69655e6e.eastus2.azurecontainerapps.io). This guide describes the resources and operating requirements; deployment history belongs in Azure deployment/revision records.

## Azure resources

| Resource | Purpose | Provisioning |
|---|---|---|
| Resource group | Holds the application infrastructure. | Deployment script creates or reuses the requested group. |
| Container Apps environment and Container App | Runs the application with HTTPS ingress, WebSockets and health probes. | `infra/foundation.bicep` and `infra/app.bicep`. |
| Azure Container Registry, Basic tier | Builds and stores the Linux application image. Registry admin credentials are disabled. | `infra/foundation.bicep`. |
| User-assigned managed identity | Pulls images, calls Foundry, reads secrets and reads evaluation reports. | `infra/foundation.bicep` and scoped role assignments. |
| Key Vault | Holds the shared access password, Application Insights connection string and optional Speech key. | `infra/foundation.bicep`; the deployment script uploads the allowlisted values. |
| Log Analytics workspace | Stores Container Apps platform and console logs. | `infra/foundation.bicep`. |
| StorageV2 account and private Blob container named `evaluations` | Persists saved evaluation scorecards across app deployments and restarts. | `infra/reports.bicep`, called by the foundation template. |
| Foundry resource with `Decision-1` and `gpt-6-luna` deployments | Performs story selection and audience-specific writing through Microsoft Agent Framework. | Supply an existing resource and model deployments. The templates grant the app identity inference access. |
| Application Insights linked to a Foundry project | Receives application traces and makes them available in Foundry. | Supply an existing resource, link it to the project and configure its connection string. |
| Azure Speech-capable resource | Generates spoken commentary when enabled. | Supply an existing resource key and region; it can be the existing Foundry resource if Speech is supported there. |

A Foundry project and a compatible judge deployment are needed for optional cloud grading. Grading is a separate operator action, not part of serving the app. See [evaluation setup](evals/README.md).

## Broadcast output and durable storage

The Broadcast page and `/broadcast` graphics window consume the stories already published by the application. They do not run another simulation or make additional model calls. The graphics window must be opened from the source MatchOS tab in the same browser/profile; cross-window updates use the browser's BroadcastChannel API.

The storage account serves a different purpose: keeping evaluation results available after a rollout.

| Data | Where it lives |
|---|---|
| Match simulation, audience decisions, developing-story timeline and personalized recaps | Connection-local application memory. A disconnect or reset can clear them. |
| Local execution telemetry and speech registration/cache | Application memory. Cloud traces are exported separately to Application Insights. |
| Published evaluation scorecards | Private `evaluations` Blob container, under `reports/`. The app reads them through authenticated API routes. |
| Full evaluation inputs, outputs and local JSON/Markdown/JSONL reports | The operator's Git-ignored `evals/results/` directory. These full reports are not automatically uploaded. |

`infra/reports.bicep` configures Standard LRS storage, HTTPS only, TLS 1.2 minimum, disabled public Blob access, disabled shared-key access, Blob versioning and seven-day soft delete. Access uses Microsoft Entra identities:

- The application identity receives **Storage Blob Data Reader** on the container.
- The deployment operator receives **Storage Blob Data Contributor** on the container to publish scorecards.
- The browser receives reports through the authenticated app, with no storage key or SAS URL.

The foundation output `reportsContainerUrl` becomes the app setting `MATCHOS_REPORTS_CONTAINER_URL`. It is an address, not a secret. The existing environment uses `https://matchosf272ydyn24aus.blob.core.windows.net/evaluations`.

Provisioning storage does not generate test results. Run the evaluation suite and publish a saved report explicitly:

```sh
# In .env.local, set MATCHOS_REPORTS_CONTAINER_URL to the foundation output.
# Use az login with the identity granted Blob Data Contributor.
.venv/bin/python -m matchos.evaluation run --local
.venv/bin/python -m matchos.evaluation publish evals/results/latest.json
```

Remove `--local` to evaluate the configured cloud models; that incurs inference charges. Publishing a saved report does not rerun inference. The Evaluations tab shows the latest archived version of each execution. It never launches tests or paid grading from the browser.

## Deployment prerequisites

Use the project virtual environment, Azure CLI login and Bicep (`az bicep install`). The operator needs permission to create resources and assign roles in the application resource group and at the existing Foundry resource scope. Infrastructure grants the operator Key Vault Secrets Officer on the application vault and Blob Data Contributor on the evaluation container.

Ensure the subscription has the resource providers registered for Container Apps, Container Registry, Managed Identity, Key Vault, Log Analytics and Storage: `Microsoft.App`, `Microsoft.ContainerRegistry`, `Microsoft.ManagedIdentity`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights` and `Microsoft.Storage`.

Configure local files without committing credentials:

| File / setting | Required configuration |
|---|---|
| `.env.local`: `APPLICATIONINSIGHTS_CONNECTION_STRING` | Connection string of the Application Insights resource linked to the Foundry project. |
| `.env.local`: `AZURE_DECISION_DEPLOYMENT`, `AZURE_REASONING_DEPLOYMENT` | Deployment names; defaults are `Decision-1` and `gpt-6-luna` in the supplied Foundry resource. |
| `.env.local`: optional speech | `MATCHOS_SPEECH_ENABLED=1`, `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`, and optionally `AZURE_SPEECH_MODEL`. |
| `.env.production.local`: app access | `MATCHOS_BASIC_AUTH_ENABLED=1`, `MATCHOS_BASIC_AUTH_USERNAME=judges`, and `MATCHOS_BASIC_AUTH_PASSWORD` with a strong random password of at least 16 characters. Keep this ignored file mode 0600. |

The deployment script loads `.env`, `.env.local`, then `.env.production.local`; process variables take precedence. It uploads only the Application Insights connection string, enabled Speech key and enabled access password to Key Vault, using restricted temporary files rather than secret values in command-line arguments. Production model calls use managed identity; inference API keys are not uploaded.

The app itself does not load `.env.production.local` for local development. To test the access gate on local HTTP, set the auth values in `.env.local` and `MATCHOS_BASIC_AUTH_SECURE_COOKIE=0`. Azure uses secure cookies.

## Provision and deploy

Run from the repository root. A local Docker daemon is not required; ACR performs the build.

```sh
.venv/bin/python scripts/deploy_azure.py \
  --subscription '<subscription-id>' \
  --resource-group rg-matchos-prod \
  --location eastus2 \
  --foundry-resource-group '<existing-foundry-resource-group>' \
  --foundry-account '<existing-foundry-resource-name>' \
  --operator-object-id '<signed-in-user-or-CI-principal-object-id>'
```

For a signed-in user, obtain the object ID with `az ad signed-in-user show --query id -o tsv`. A CI principal needs its object ID, not its application/client ID. Commands target the supplied subscription explicitly.

This command creates or updates the foundation, including the storage account, private container and roles; uploads allowlisted secrets; builds the application; then deploys it with model, tracing, speech, authentication and storage settings. The foundation reuses its deterministically named resources. Reapplying it preserves existing Blob reports.

For app-only releases, add `--skip-foundation` once the foundation is complete. When infrastructure changes, run without that flag so resources and deployment outputs are updated first. In particular, an older foundation without `reportsContainerUrl` must be updated before the app can read the durable archive.

The container image uses Python 3.13, a non-root user and a dependency lock with package hashes. The build context contains allowlisted runtime files and excludes local credentials, virtual environments, Git history and videos. Image references are pinned for repeatability; use Azure's deployment/revision history to track individual releases.

## Verify the running app

```sh
# Health, authenticated assets, evaluation archive and WebSocket handshake;
# does not start paid model work.
.venv/bin/python scripts/smoke_deployment.py \
  'https://<app-fqdn>' --auth-env-file .env.production.local

# Optional: real Azure model calls, published graphics/recaps and speech.
# This check incurs model and Speech charges.
.venv/bin/python scripts/smoke_deployment.py \
  'https://<app-fqdn>' --auth-env-file .env.production.local --judge-demo --speech
```

In the browser, check automatic playback, the three Judge demo checkpoints, audience stories and evidence, Broadcast graphics/recaps, saved evaluations and Listen. Model logs should identify the Azure deployments rather than a local fallback. An empty evaluation archive is valid until an operator publishes a scorecard; an archive error indicates a storage configuration or access issue.

## Access, monitoring and rollback

The access gate uses an HTTP Basic browser prompt plus a signed 12-hour HttpOnly/Secure/SameSite cookie for WebSocket and audio requests. All routes require authentication except `/api/health`. Share the login privately with judges. Rotate access by changing the password in `.env.production.local` and redeploying; old signed cookies become invalid. This is shared demo access, not individual accounts or SSO.

Container Apps **Log stream** shows model call IDs, names, durations and safe error categories. **Revisions and replicas** shows health and restarts. Compact OpenTelemetry traces go to the existing Application Insights / Foundry integration. Production disables prompt/response content logging and trace-content export.

Startup, readiness and liveness probes use `/api/health`. Provider failures do not by themselves make the process unhealthy. Log Analytics retains 30 days with a 1 GB/day ingestion cap. This is an ingestion limit, not an application spending cap. Use Azure Cost Management budgets and owner notifications for ongoing operation.

To roll back, use the same deployment command with `--skip-foundation --image '<previous-tested-image-reference>'`. This reuses the existing infrastructure and secrets, skips the build and secret upload, and creates a new app revision. Preserve the matching speech/model/access configuration and use an image that supports the access gate. Deployments and rollbacks can disconnect active matches; routing uses a single active revision.

## Runtime limits and scaling

Keep **one worker and one replica** until session and task state are externalized. The deployment uses one warm replica, 1 vCPU / 2 GiB and admission control for at most eight open match WebSockets. This is not a load-tested throughput guarantee. Sticky routing does not preserve sessions across restarts, rollouts or failures.

Blob-backed evaluation reports survive restarts; live matches, recaps and speech caches do not. For a larger audience, add shared match snapshots, durable event processing, resumable session IDs, external audio storage, shared concurrency limits and per-user quotas before increasing replicas. A licensed live feed can then justify Event Hubs and shared evidence workers. Those services are future architecture, not prerequisites for the current app.

CI validates code, tests, the offline evaluation baseline, packaging, Bicep and container HTTP/WebSocket behavior. Automatic CD/OIDC, canary routing, uptime alert delivery and horizontal scaling are not configured by these templates. See [ARCHITECTURE.md](ARCHITECTURE.md) for the system design and [evaluation operations](evals/README.md) for tracing, grading and report publishing.
