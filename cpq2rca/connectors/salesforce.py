"""Live Salesforce connector (REST API).

Authentication options:
  * Salesforce CLI
  * Access token
  * Client credentials
  * Username/password

Read-only: describe and SOQL query calls (REST and Tooling API).
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


class SfCliError(RuntimeError):
    """A Salesforce CLI command failed; the message is the CLI's own."""


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
        self._describe_cache: dict = {}

    def _set_token(self, access_token: str) -> None:
        self.s.headers["Authorization"] = f"Bearer {access_token}"

    # Authentication helpers

    @staticmethod
    def _run_sf(sf: str, args: list[str]) -> dict:
        """Run an sf CLI command with --json and return its "result".

        Raises SfCliError carrying the CLI's own message (not a bare
        CalledProcessError) so the user can see what went wrong.
        """
        proc = subprocess.run(
            [sf, *args, "--json"], capture_output=True, text=True, timeout=120
        )
        try:
            body = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            body = {}
        if proc.returncode != 0 or body.get("status", 0) != 0:
            msg = body.get("message") or proc.stderr.strip() or proc.stdout.strip()
            raise SfCliError(f"'sf {' '.join(args)}' failed: {msg or f'exit code {proc.returncode}'}")
        return body.get("result") or {}

    @staticmethod
    def _usable(token) -> bool:
        return bool(token) and not str(token).startswith("[REDACTED")

    @classmethod
    def _sf_cli_token(cls, sf: str, alias: str) -> str:
        """Get an access token from the Salesforce CLI.

        Tries the commands that work across CLI versions, in order:
          1. sf org auth show-access-token   (newer CLIs)
          2. sf org display --verbose        (older CLIs; token in result)
        """
        errors = []
        attempts = (
            ["org", "auth", "show-access-token", "--target-org", alias],
            ["org", "display", "--verbose", "--target-org", alias],
        )
        for args in attempts:
            try:
                token = cls._run_sf(sf, args).get("accessToken")
            except (SfCliError, subprocess.TimeoutExpired) as e:
                errors.append(str(e))
                continue
            if cls._usable(token):
                return token
            errors.append(f"'sf {' '.join(args)}' returned no usable access token")
        raise RuntimeError(
            f"Could not get an access token for org '{alias}' from the Salesforce CLI. "
            f"Check the alias with 'sf org list' and log in again with 'sf org login web -a {alias}'. "
            "Details: " + " | ".join(errors)
        )

    @classmethod
    def from_sf_cli(cls, alias: str) -> "SalesforceConnector":
        sf = shutil.which("sf")

        if not sf:
            raise RuntimeError(
                "Salesforce CLI ('sf') not found on PATH. Install it with "
                "'npm install --global @salesforce/cli'."
            )

        try:
            res = cls._run_sf(sf, ["org", "display", "--target-org", alias])
        except SfCliError as e:
            raise RuntimeError(
                f"Salesforce CLI does not know org '{alias}'. Check 'sf org list' "
                f"or log in with 'sf org login web -a {alias}'. Details: {e}"
            ) from e

        instance_url = res.get("instanceUrl")
        if not instance_url:
            raise RuntimeError(f"Salesforce CLI returned no instance URL for org '{alias}'")

        return cls(
            instance_url,
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

    def _describe(self, sobject: str, tooling: bool = False) -> dict:
        key = (sobject, tooling)
        if key not in self._describe_cache:
            base = f"{self.api}/tooling" if tooling else self.api
            self._describe_cache[key] = self._get(f"{base}/sobjects/{sobject}/describe")
        return self._describe_cache[key]

    def describe_fields(self, sobject: str) -> set[str]:
        return {f["name"] for f in self._describe(sobject)["fields"]}

    def describe(self, sobject: str) -> list[dict]:
        return [
            {
                "name": f["name"],
                "label": f.get("label"),
                "type": f.get("type"),
                "custom": bool(f.get("custom")),
                "formula": bool(f.get("calculated")),
                "reference_to": f.get("referenceTo") or []
            }
            for f in self._describe(sobject)["fields"]
        ]

    def _query(self, soql: str, tooling: bool = False) -> list[dict]:
        base = f"{self.api}/tooling" if tooling else self.api
        page = self._get(f"{base}/query", {"q": soql})
        records = list(page["records"])
        while not page.get("done", True):
            page = self._get(self.instance_url + page["nextRecordsUrl"])
            records.extend(page["records"])
        for r in records:
            r.pop("attributes", None)
        return records

    def _select(self, sobject: str, wanted: list[str], tooling: bool = False) -> list[str]:
        """The wanted fields that exist in this org/API version (keeps queries valid)."""
        have = {f["name"] for f in self._describe(sobject, tooling)["fields"]}
        return [f for f in wanted if f in have]

    # ---------------------------------------------------------- automation

    def _entities(self, objects: list[str]) -> dict[str, dict]:
        """Map every way an object can be referenced (API name, DurableId and its
        15-character form) to {"name", "label"}."""
        out = {}
        for i in range(0, len(objects), 100):
            names = ", ".join(f"'{o}'" for o in objects[i:i + 100])
            for r in self._query(
                "SELECT DurableId, QualifiedApiName, Label FROM EntityDefinition "
                f"WHERE QualifiedApiName IN ({names})"
            ):
                info = {"name": r["QualifiedApiName"], "label": r.get("Label")}
                for k in (r["QualifiedApiName"], r.get("DurableId")):
                    if k:
                        out[k] = info
                        out[k[:15]] = info
        return out

    def automation(self, objects: list[str]) -> dict:
        result = {"apex_triggers": [], "validation_rules": [], "flows": [], "errors": []}
        try:
            ents = self._entities(objects)
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"EntityDefinition: {e}")
            ents = {o: {"name": o, "label": o} for o in objects}
        by_label = {v["label"]: v for v in ents.values() if v.get("label")}

        def resolve(ref, label=None):
            if ref and (ref in ents or str(ref)[:15] in ents):
                return ents.get(ref) or ents[str(ref)[:15]]
            if label and label in by_label:
                return by_label[label]
            return None

        def run(kind, fn):
            try:
                fn()
            except ApiLimitExceeded:
                raise
            except Exception as e:  # noqa: BLE001 - keep the other inventories
                result["errors"].append(f"{kind}: {e.__class__.__name__}: {str(e)[:300]}")

        def triggers():
            cols = self._select("ApexTrigger", ["Id", "Name", "TableEnumOrId", "Status", "NamespacePrefix",
                                                "LengthWithoutComments", "ApiVersion", "UsageBeforeInsert",
                                                "UsageAfterInsert", "UsageBeforeUpdate", "UsageAfterUpdate",
                                                "UsageBeforeDelete", "UsageAfterDelete"], tooling=True)
            for r in self._query(f"SELECT {', '.join(cols)} FROM ApexTrigger", tooling=True):
                ent = resolve(r.get("TableEnumOrId"))
                if not ent or r.get("NamespacePrefix"):
                    continue  # not on a CPQ object, or part of a managed package
                events = [k[5:] for k, v in r.items() if k.startswith("Usage") and v]
                result["apex_triggers"].append({
                    "id": r["Id"], "name": r["Name"], "object": ent["name"],
                    "active": r.get("Status") == "Active", "lines": r.get("LengthWithoutComments"),
                    "api_version": r.get("ApiVersion"), "events": events})

        def validation_rules():
            cols = self._select("ValidationRule", ["Id", "ValidationName", "Active", "ErrorMessage",
                                                   "ErrorDisplayField", "EntityDefinitionId",
                                                   "NamespacePrefix", "Description"], tooling=True)
            for r in self._query(f"SELECT {', '.join(cols)} FROM ValidationRule", tooling=True):
                ent = resolve(r.get("EntityDefinitionId"))
                if not ent or r.get("NamespacePrefix"):
                    continue
                result["validation_rules"].append({
                    "id": r["Id"], "name": r["ValidationName"], "object": ent["name"],
                    "active": bool(r.get("Active")), "error_message": r.get("ErrorMessage"),
                    "error_field": r.get("ErrorDisplayField"), "description": r.get("Description")})

        def flows():
            cols = self._select("FlowDefinitionView", ["DurableId", "ApiName", "Label", "ProcessType",
                                                       "TriggerType", "TriggerObjectOrEventId",
                                                       "TriggerObjectOrEventLabel", "IsActive",
                                                       "NamespacePrefix", "Description"])
            for r in self._query(f"SELECT {', '.join(cols)} FROM FlowDefinitionView WHERE IsActive = true"):
                ent = resolve(r.get("TriggerObjectOrEventId"), r.get("TriggerObjectOrEventLabel"))
                if not ent or r.get("NamespacePrefix"):
                    continue  # only record-triggered flows / processes on CPQ objects
                result["flows"].append({
                    "id": r.get("DurableId") or r["ApiName"], "name": r.get("Label") or r["ApiName"],
                    "api_name": r["ApiName"], "object": ent["name"], "active": True,
                    "process_type": r.get("ProcessType"), "trigger_type": r.get("TriggerType"),
                    "description": r.get("Description")})

        run("Apex triggers", triggers)
        run("Validation rules", validation_rules)
        run("Flows", flows)
        return result

    def query_all(
        self,
        sobject: str,
        fields: list[str],
        where: str = ""
    ) -> list[dict]:
        soql = f"SELECT {', '.join(fields)} FROM {sobject}"
        if where:
            soql += f" WHERE {where}"
        return self._query(soql + " ORDER BY Id")

    def count(self, sobject: str, where: str = "") -> int:
        soql = f"SELECT COUNT() FROM {sobject}"
        if where:
            soql += f" WHERE {where}"
        return self._get(f"{self.api}/query", {"q": soql})["totalSize"]
