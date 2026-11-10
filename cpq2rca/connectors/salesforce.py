"""Live Salesforce connector (REST API).

Authentication options (choose one):
  * Salesforce CLI:     SalesforceConnector.from_sf_cli("my-sandbox-alias")
  * Access token:       SalesforceConnector(instance_url, access_token)
  * Client credentials: SalesforceConnector.client_credentials(domain, client_id, secret)
  * Username/password:  SalesforceConnector.password_flow(domain, client_id, secret, user, pwd+token)

Read-only: the connector only issues describe and SOQL query calls.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

import requests

from .base import Connector, ObjectNotFound

API_VERSION = "v62.0"


class SalesforceConnector(Connector):
    def __init__(self, instance_url: str, access_token: str, api_version: str = API_VERSION,
                 timeout: int = 120):
        self.instance_url = instance_url.rstrip("/")
        self.api = f"{self.instance_url}/services/data/{api_version}"
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {access_token}",
                               "Accept": "application/json"})
        self.timeout = timeout

    # ------------------------------------------------------------ auth helpers
    
@classmethod
def from_sf_cli(cls, alias: str) -> "SalesforceConnector":
    sf = shutil.which("sf")
    if not sf:
        raise RuntimeError("Salesforce CLI ('sf') not found on PATH")

    out = subprocess.run(
        [sf, "org", "display", "--target-org", alias, "--json"],
        capture_output=True, text=True, check=True
    )
    res = json.loads(out.stdout)["result"]

    token_out = subprocess.run(
        [sf, "org", "auth", "show-access-token", "--target-org", alias, "--json"],
        capture_output=True, text=True, check=True
    )
    token_result = json.loads(token_out.stdout)
    access_token = token_result.get("result", {}).get("accessToken")

    if not access_token:
        raise RuntimeError("Salesforce CLI did not return an access token")

    return cls(res["instanceUrl"], access_token)


    @classmethod
    def _token(cls, domain: str, data: dict) -> "SalesforceConnector":
        r = requests.post(f"https://{domain}/services/oauth2/token", data=data, timeout=60)
        r.raise_for_status()
        body = r.json()
        return cls(body["instance_url"], body["access_token"])

    @classmethod
    def client_credentials(cls, domain, client_id, client_secret):
        return cls._token(domain, {"grant_type": "client_credentials",
                                   "client_id": client_id, "client_secret": client_secret})

    @classmethod
    def password_flow(cls, domain, client_id, client_secret, username, password):
        return cls._token(domain, {"grant_type": "password", "client_id": client_id,
                                   "client_secret": client_secret, "username": username,
                                   "password": password})

    # ------------------------------------------------------------------- calls
    def _get(self, url: str, params=None) -> dict:
        for attempt in range(4):
            r = self.s.get(url, params=params, timeout=self.timeout)
            if r.status_code == 404:
                raise ObjectNotFound(url)
            if r.status_code in (429, 503) and attempt < 3:
                time.sleep(2 ** attempt * 5)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Salesforce API error {r.status_code}: {r.text[:500]}")
            return r.json()
        raise RuntimeError("Salesforce API retry limit reached")

    def org_info(self) -> dict:
        try:
            q = self._get(f"{self.api}/query", {"q": "SELECT Id, Name, OrganizationType, IsSandbox FROM Organization"})
            org = q["records"][0]
            pkgs = self._installed_packages()
            return {"org_id": org["Id"], "org_name": org["Name"], "edition": org.get("OrganizationType"),
                    "is_sandbox": org.get("IsSandbox"), "instance_url": self.instance_url,
                    "packages": pkgs, "source": "salesforce"}
        except Exception as e:  # noqa: BLE001 - info is best effort
            return {"instance_url": self.instance_url, "source": "salesforce", "warning": str(e)}

    def _installed_packages(self) -> list[dict]:
        try:
            q = self._get(f"{self.api}/tooling/query", {"q": (
                "SELECT SubscriberPackage.NamespacePrefix, SubscriberPackage.Name, "
                "SubscriberPackageVersion.MajorVersion, SubscriberPackageVersion.MinorVersion "
                "FROM InstalledSubscriberPackage")})
            out = []
            for r in q.get("records", []):
                sp, v = r.get("SubscriberPackage") or {}, r.get("SubscriberPackageVersion") or {}
                out.append({"namespace": sp.get("NamespacePrefix"), "name": sp.get("Name"),
                            "version": f"{v.get('MajorVersion')}.{v.get('MinorVersion')}"})
            return out
        except Exception:  # noqa: BLE001
            return []

    def describe_fields(self, sobject: str) -> set[str]:
        d = self._get(f"{self.api}/sobjects/{sobject}/describe")
        return {f["name"] for f in d["fields"]}

    def query_all(self, sobject: str, fields: list[str], where: str = "") -> list[dict]:
        soql = f"SELECT {', '.join(fields)} FROM {sobject}"
        if where:
            soql += f" WHERE {where}"
        soql += " ORDER BY Id"
        page = self._get(f"{self.api}/query", {"q": soql})
        records = list(page["records"])
        while not page.get("done", True):
            page = self._get(self.instance_url + page["nextRecordsUrl"])
            records.extend(page["records"])
        for r in records:
            r.pop("attributes", None)
        return records

    def count(self, sobject: str) -> int:
        return self._get(f"{self.api}/query", {"q": f"SELECT COUNT() FROM {sobject}"})["totalSize"]
