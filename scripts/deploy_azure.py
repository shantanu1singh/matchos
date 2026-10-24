"""Repeatable Container Apps deployment; Azure CLI login, no stored Azure credentials.

Run with the project venv. Only allowlisted access/Speech/App Insights values are copied
from local dotenv files to Key Vault; inference uses managed identity.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def az(*args, json_output=False):
    command = ['az', *map(str, args), '--only-show-errors', '-o', 'json' if json_output else 'none']
    # No credential values are ever passed in argv or printed.
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        # CLI response bodies can contain secret values: don't echo arbitrary output.
        raise RuntimeError(f'Azure command failed: az {args[0]} {args[1]}; inspect its resource deployment/operation status in Azure.')
    return json.loads(result.stdout) if json_output else None


def build_context(destination: Path):
    """Stage an allowlist, independent of CLI dockerignore implementation."""
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('Dockerfile', '.dockerignore', 'requirements.txt', 'requirements-observability.txt', 'requirements-production.lock'):
        shutil.copy2(ROOT / name, destination / name)
    for source in [*(ROOT / 'matchos').glob('*.py'), *(ROOT / 'matchos' / 'web').glob('*')]:
        if source.is_file() and not source.is_symlink() and source.suffix in {'.py', '.html', '.js', '.css', '.svg', '.woff2'}:
            target = destination / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def put_secret(subscription, vault, name, value, directory):
    path = directory / name
    path.write_text(value)
    path.chmod(0o600)
    try:
        for attempt in range(12):
            try:
                az('keyvault', 'secret', 'set', '--subscription', subscription, '--vault-name', vault,
                   '--name', name, '--file', path, '--encoding', 'utf-8')
                return
            except RuntimeError:
                if attempt == 11:
                    raise
                time.sleep(10)  # Initial Key Vault RBAC propagation.
    finally:
        path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subscription', required=True)
    parser.add_argument('--resource-group', default='rg-matchos-prod')
    parser.add_argument('--location', default='eastus2')
    parser.add_argument('--app-name', default='matchos-prod')
    parser.add_argument('--foundry-resource-group', required=True)
    parser.add_argument('--foundry-account', required=True)
    parser.add_argument('--operator-object-id', required=True)
    parser.add_argument('--skip-foundation', action='store_true', help='Reuse existing matchos-foundation deployment outputs.')
    parser.add_argument('--image', help='Deploy an existing immutable image to roll back; skips build and secret sync.')
    args = parser.parse_args()
    common = ['--subscription', args.subscription, '--resource-group', args.resource_group]
    config = {**dotenv_values(ROOT / '.env'), **dotenv_values(ROOT / '.env.local'),
              **dotenv_values(ROOT / '.env.production.local'), **os.environ}
    # Fail before spending money if required local configuration is missing.
    if not args.image and not config.get('APPLICATIONINSIGHTS_CONNECTION_STRING'):
        parser.error('APPLICATIONINSIGHTS_CONNECTION_STRING must be configured locally for tracing.')
    speech = config.get('MATCHOS_SPEECH_ENABLED', '').lower() in {'1', 'true'}
    if speech and not args.image and not config.get('AZURE_SPEECH_KEY'):
        parser.error('Speech is enabled but AZURE_SPEECH_KEY is missing.')
    basic_auth = config.get('MATCHOS_BASIC_AUTH_ENABLED', '1').lower() in {'1', 'true'}
    if basic_auth and not args.image and len(config.get('MATCHOS_BASIC_AUTH_PASSWORD', '')) < 16:
        parser.error('Set MATCHOS_BASIC_AUTH_PASSWORD (at least 16 characters) in .env.production.local.')
    if not args.skip_foundation:
        print('Provisioning production infrastructure...', flush=True)
        az('group', 'create', '--subscription', args.subscription, '--name', args.resource_group, '--location', args.location)
        deployment = az('deployment', 'group', 'create', *common, '--name', 'matchos-foundation',
            '--template-file', ROOT / 'infra/foundation.bicep', '--parameters',
            f'location={args.location}', f'appName={args.app_name}', f'operatorPrincipalId={args.operator_object_id}',
            f'foundryResourceGroup={args.foundry_resource_group}', f'foundryAccountName={args.foundry_account}', json_output=True)
    else:
        deployment = az('deployment', 'group', 'show', *common, '--name', 'matchos-foundation', json_output=True)
    outputs = {key: value['value'] for key, value in deployment['properties']['outputs'].items()}
    with tempfile.TemporaryDirectory(prefix='matchos-deploy-') as temporary:
        work = Path(temporary)
        image = args.image
        if not image:
            print('Syncing allowlisted secrets to Key Vault...', flush=True)
            put_secret(args.subscription, outputs['vaultName'], 'applicationinsights-connection-string',
                       config['APPLICATIONINSIGHTS_CONNECTION_STRING'], work)
            if speech:
                put_secret(args.subscription, outputs['vaultName'], 'azure-speech-key', config['AZURE_SPEECH_KEY'], work)
            if basic_auth:
                put_secret(args.subscription, outputs['vaultName'], 'matchos-basic-auth-password', config['MATCHOS_BASIC_AUTH_PASSWORD'], work)
            context = work / 'build'
            build_context(context)
            tag = f'release-{time.strftime("%Y%m%d%H%M%S", time.gmtime())}-{uuid.uuid4().hex[:8]}'
            print(f'Building linux/amd64 image {tag} in ACR (credentials excluded)...', flush=True)
            az('acr', 'build', '--subscription', args.subscription, '--registry', outputs['registryName'],
               '--image', f'matchos:{tag}', '--platform', 'linux/amd64', '--no-logs', context)
            metadata = az('acr', 'manifest', 'show-metadata', '--subscription', args.subscription,
                          '--registry', outputs['registryName'], '--name', f'matchos:{tag}', json_output=True)
            image = f'{outputs["registryServer"]}/matchos@{metadata["digest"]}'
        print('Deploying pinned image with probes and managed identity...', flush=True)
        deployment = az('deployment', 'group', 'create', *common, '--name', 'matchos-app',
            '--template-file', ROOT / 'infra/app.bicep', '--parameters', f'location={args.location}',
            f'appName={args.app_name}', f'image={image}', f'environmentId={outputs["environmentId"]}',
            f'identityId={outputs["identityId"]}', f'identityClientId={outputs["identityClientId"]}',
            f'registryServer={outputs["registryServer"]}', f'vaultUri={outputs["vaultUri"]}',
            f'foundryEndpoint=https://{args.foundry_account}.services.ai.azure.com',
            f'decisionDeployment={config.get("AZURE_DECISION_DEPLOYMENT", "Decision-1")}',
            f'reasoningDeployment={config.get("AZURE_REASONING_DEPLOYMENT", "gpt-6-luna")}',
            f'speechEnabled={str(speech).lower()}', f'speechRegion={config.get("AZURE_SPEECH_REGION", "eastus2")}',
            f'basicAuthEnabled={str(basic_auth).lower()}', f'basicAuthUsername={config.get("MATCHOS_BASIC_AUTH_USERNAME", "judges")}',
            f'speechModel={config.get("AZURE_SPEECH_MODEL", "MAI-Voice-2.1-Flash")}', json_output=True)
    url = deployment['properties']['outputs']['url']['value']
    print(f'Deployed {image}\nURL: {url}\nVerify: python scripts/smoke_deployment.py {url}', flush=True)


if __name__ == '__main__':
    main()
