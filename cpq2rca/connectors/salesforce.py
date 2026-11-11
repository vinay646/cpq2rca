"""Live Salesforce connector (REST API).

Authentication options:
  * Salesforce CLI
  * Access token
  * Client credentials
  * Username/password

Read-only: describe and SOQL query calls.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

import requests

from .base import ApiLimitExceeded, Connector, ObjectNotFound

API_VERSION = "v62.0"
MAX_ATTEMPTS = 4
RETRY_STATUS = (429, 500, 502, 503, 504)


class SalesforceConnector(Connector):

    def __init__(
        self,
        instance_url: str,
        access_token: str,
        api_version: str = API_VERSION,
        timeout: int = 120,
        token_refresher=None
    ):
        self.instance_url = instance_url.rstrip("/")
        self.api = f"{self.instance_url}/services/data/{api_version}"
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json"})
        self._set_token(access_token)
        self.timeout = timeout
        # Optional callable returning a fresh access token; used once when the
        # session expires (401) part-way through a long extract.
        self.token_refresher = token_refresher

    def _set_token(self, access_token: str) -> None:
        self.s.headers["Authorization"] = f"Bearer {access_token}"

    # Authentication helpers

    @staticmethod
    def _sf_cli_token(sf: str, alias: str) -> str:
        token_out = subprocess.run(
            [sf, "org", "auth", "show-access-token", "--target-org", alias, "--json"],
            capture_output=True,
            text=True,
            check=True
        )
        access_token = json.loads(token_out.stdout).get("result", {}).get("accessToken")
        if not access_token or access_token.startswith("[REDACTED]"):
            raise RuntimeError("Salesforce CLI did not return a usable access token")
        return access_token

    @classmethod
    def from_sf_cli(cls, alias: str) -> "SalesforceConnector":
        sf = shutil.which("sf")

        if not sf:
            raise RuntimeError("Salesforce CLI ('sf') not found on PATH")

        out = subprocess.run(
            [sf, "org", "display", "--target-org", alias, "--json"],
            capture_output=True,
            text=True,
            check=True
        )
        res = json.loads(out.stdout)["result"]

        return cls(
            res["instanceUrl"],
            cls._sf_cli_token(sf, alias),
            token_refresher=lambda: cls._sf_cli_token(sf, alias)
        )

    @classmethod
    def _token(cls, domain: str, data: dict) -> "SalesforceConnector":
        r = requests.post(
            f"https://{domain}/services/oauth2/token",
            data=data,
            timeout=60
        )
        r.raise_for_status()
        body = r.json()
        return cls(body["instance_url"], body["access_token"])

    @classmethod
    def client_credentials(cls, domain, client_id, client_secret):
        return cls._token(
            domain,
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret
            }
        )

    @classmethod
    def password_flow(
        cls, domain, client_id, client_secret, username, password
    ):
        return cls._token(
            domain,
            {
                "grant_type": "password",
                "client_id": client_id,
                "client_secret": client_secret,
                "username": username,
                "password": password
            }
        )

    # API calls

    def _get(self, url: str, params=None) -> dict:
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2 ** attempt * 5)
                    continue
                raise RuntimeError(
                    f"Salesforce API unreachable after {attempt} attempts: {e}"
                ) from e

            if r.status_code == 404:
                raise ObjectNotFound(url)

            if r.status_code == 401 and self.token_refresher and not refreshed:
                # Session expired mid-run: get a new token once and retry.
                self._set_token(self.token_refresher())
                refreshed = True
                continue

            if r.status_code == 403 and "REQUEST_LIMIT_EXCEEDED" in r.text:
                raise ApiLimitExceeded(
                    "Salesforce API request limit exceeded for this org. "
                    "Wait for the 24-hour limit to reset or ask the client to "
                    f"raise it, then re-run. Details: {r.text[:300]}"
                )

            if r.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                retry_after = r.headers.get("Retry-After", "")
                time.sleep(int(retry_after) if retry_after.isdigit() else 2 ** attempt * 5)
                continue

            if r.status_code >= 400:
                raise RuntimeError(
                    f"Salesforce API error {r.status_code}: {r.text[:500]}"
                )

            return r.json()

    def org_info(self) -> dict:
        try:
            q = self._get(
                f"{self.api}/query",
                {
                    "q": (
                        "SELECT Id, Name, OrganizationType, IsSandbox "
                        "FROM Organization"
                    )
                }
            )

            org = q["records"][0]
            pkgs = self._installed_packages()

            return {
                "org_id": org["Id"],
                "org_name": org["Name"],
                "edition": org.get("OrganizationType"),
                "is_sandbox": org.get("IsSandbox"),
                "instance_url": self.instance_url,
                "packages": pkgs,
                "currencies": self._currencies(),
                "source": "salesforce"
            }

        except Exception as e:
            return {
                "instance_url": self.instance_url,
                "source": "salesforce",
                "warning": str(e)
            }

    def _currencies(self) -> list[dict]:
        """Active currencies. CurrencyType only exists when multi-currency is
        enabled, so an empty list means a single-currency org."""
        try:
            q = self._get(
                f"{self.api}/query",
                {
                    "q": (
                        "SELECT IsoCode, IsCorporate, ConversionRate "
                        "FROM CurrencyType WHERE IsActive = true"
                    )
                }
            )
        except (ObjectNotFound, RuntimeError):
            return []
        return [
            {
                "iso_code": r["IsoCode"],
                "corporate": bool(r.get("IsCorporate")),
                "conversion_rate": r.get("ConversionRate")
            }
            for r in q.get("records", [])
        ]

    def _installed_packages(self) -> list[dict]:
        try:
            q = self._get(
                f"{self.api}/tooling/query",
                {
                    "q": (
                        "SELECT SubscriberPackage.NamespacePrefix, "
                        "SubscriberPackage.Name, "
                        "SubscriberPackageVersion.MajorVersion, "
                        "SubscriberPackageVersion.MinorVersion "
                        "FROM InstalledSubscriberPackage"
                    )
                }
            )

            out = []

            for r in q.get("records", []):
                sp = r.get("SubscriberPackage") or {}
                v = r.get("SubscriberPackageVersion") or {}

                out.append({
                    "namespace": sp.get("NamespacePrefix"),
                    "name": sp.get("Name"),
                    "version": (
                        f"{v.get('MajorVersion')}.{v.get('MinorVersion')}"
                    )
                })

            return out

        except Exception:
            return []

    def describe_fields(self, sobject: str) -> set[str]:
        d = self._get(
            f"{self.api}/sobjects/{sobject}/describe"
        )
        return {f["name"] for f in d["fields"]}

    def query_all(
        self,
        sobject: str,
        fields: list[str],
        where: str = ""
    ) -> list[dict]:

        soql = f"SELECT {', '.join(fields)} FROM {sobject}"

        if where:
            soql += f" WHERE {where}"

        soql += " ORDER BY Id"

        page = self._get(
            f"{self.api}/query",
            {"q": soql}
        )

        records = list(page["records"])

        while not page.get("done", True):
            page = self._get(
                self.instance_url + page["nextRecordsUrl"]
            )
            records.extend(page["records"])

        for r in records:
            r.pop("attributes", None)

        return records

    def count(self, sobject: str) -> int:
        return self._get(
            f"{self.api}/query",
            {"q": f"SELECT COUNT() FROM {sobject}"}
        )["totalSize"]
