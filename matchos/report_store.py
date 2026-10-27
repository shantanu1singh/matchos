"""Private, keyless Blob scorecards in Azure; immutable JSON archives locally."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
from azure.identity.aio import DefaultAzureCredential

ID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


class ReportStore:
    def __init__(self, directory=None, container_url=None):
        self.directory = Path(
            directory
            or os.getenv(
                "MATCHOS_REPORTS_DIR", Path(__file__).resolve().parent.parent / "evals/results/reports"
            )
        )
        self.url = (
            container_url if container_url is not None else os.getenv("MATCHOS_REPORTS_CONTAINER_URL", "")
        ).rstrip("/")
        if self.url:
            parsed = urlsplit(self.url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or not parsed.hostname.endswith(".blob.core.windows.net")
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or len(parsed.path.strip("/").split("/")) != 1
            ):
                raise ValueError(
                    "MATCHOS_REPORTS_CONTAINER_URL must be an Azure Blob container HTTPS URL without credentials"
                )

    async def blob(self, method, name="", *, params=None, data=None):
        async with DefaultAzureCredential() as credential:
            token = await credential.get_token("https://storage.azure.com/.default")
            headers = {"Authorization": f"Bearer {token.token}", "x-ms-version": "2023-11-03"}
            if method == "PUT":
                headers.update(
                    {"x-ms-blob-type": "BlockBlob", "Content-Type": "application/json", "If-None-Match": "*"}
                )
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(
                    method,
                    self.url + ("/" + name if name else ""),
                    params=params,
                    headers=headers,
                    content=data,
                )
                if response.status_code == 404:
                    raise FileNotFoundError(name)
                response.raise_for_status()
                return response

    async def save(self, report, *, publish=False):
        report_id = report["report_id"]
        if not ID.fullmatch(report_id):
            raise ValueError("Invalid report ID")
        body = json.dumps(report, ensure_ascii=False, allow_nan=False).encode()

        def write():
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory / f"{report_id}.json"
            # Same report ID can be refreshed locally with grading; cloud versions
            # use distinct IDs, so a publication cannot overwrite another release.
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(body)
            temporary.replace(target)

        await asyncio.to_thread(write)
        if publish:
            if not self.url:
                raise ValueError("Set MATCHOS_REPORTS_CONTAINER_URL before publishing a scorecard")
            await self.blob("PUT", f"reports/{report_id}.json", data=body)
        return report_id

    async def get(self, report_id):
        if not ID.fullmatch(report_id):
            raise FileNotFoundError(report_id)
        if self.url:
            response = await self.blob("GET", f"reports/{report_id}.json")
            return response.json()
        return json.loads(await asyncio.to_thread((self.directory / f"{report_id}.json").read_text))

    async def list(self):
        if self.url:
            # Report IDs begin with UTC creation time. Page through listings so
            # newer reports are not hidden behind Azure's lexicographic ordering.
            names, marker = [], ""
            for _ in range(20):
                response = await self.blob(
                    "GET",
                    params={
                        "restype": "container",
                        "comp": "list",
                        "prefix": "reports/",
                        "maxresults": "500",
                        "marker": marker,
                    },
                )
                root = ElementTree.fromstring(response.content)
                names.extend(
                    n.text.split("/")[-1][:-5]
                    for n in root.findall("./Blobs/Blob/Name")
                    if n.text.endswith(".json")
                )
                marker = root.findtext("NextMarker")
                if not marker:
                    break
        else:
            names = await asyncio.to_thread(lambda: [p.stem for p in self.directory.glob("*.json")])
        result = []
        runs = set()
        for name in sorted(names, reverse=True)[:30]:
            if ID.fullmatch(name):
                report = await self.get(name)
                # Publishing creates an immutable archive version, not another
                # execution. Show the latest version of each original run.
                source = report.get("source_run_id")
                run_id = source if isinstance(source, str) and ID.fullmatch(source) else report["report_id"]
                if run_id in runs:
                    continue
                runs.add(run_id)
                result.append(
                    {
                        key: report.get(key)
                        for key in (
                            "report_id",
                            "created_at",
                            "mode",
                            "sample_size",
                            "dataset_version",
                            "models",
                        )
                    }
                )
        return {
            "schema_version": "matchos.reports.v1",
            "storage": "azure-blob" if self.url else "local-json",
            "reports": result,
        }
