"""CRM tools over HubSpot (CRM v3, private-app token) and Salesforce (REST + SOQL, OAuth refresh / client credentials
/ access token): customers (contacts, companies, leads), deals, tasks and a pipeline summary by stage.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin import _http as H
from app.tools.builtin._oauth import bearer, invalidate, salesforce_session
from app.tools.connectors import ConnectorField, ConnectorKindSpec, register_connector_kind

CRM_KINDS = ("hubspot", "salesforce")
HUBSPOT = "https://api.hubapi.com"
SF_VERSION = "v61.0"
UTC = timezone.utc
CustomerKind = Literal["contact", "company", "lead", "any"]


def _split_name(name: str | None) -> tuple[str | None, str | None]:
    parts = (name or "").strip().split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return None, parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _num(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _date(v: str | None, *, default_days: int | None = None) -> str | None:
    if v:
        d = H.parse_datetime(v) if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()) else datetime.fromisoformat(v.strip())
        if d is None:
            raise ToolError(f"Invalid date '{v}' — use YYYY-MM-DD.")
        return d.date().isoformat()
    if default_days is not None:
        return (datetime.now(UTC) + timedelta(days=default_days)).date().isoformat()
    return None


class CRMAdapter:
    kind = ""
    label = ""

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        self.ctx = ctx
        self.cfg = cfg


# =========================================================================== HubSpot
CONTACT_PROPS = ["firstname", "lastname", "email", "phone", "company", "jobtitle", "lifecyclestage", "hs_lead_status",
                 "hubspot_owner_id", "lastmodifieddate", "createdate"]
COMPANY_PROPS = ["name", "domain", "industry", "city", "country", "numberofemployees", "annualrevenue",
                 "lifecyclestage", "hubspot_owner_id", "hs_lastmodifieddate"]
DEAL_PROPS = ["dealname", "amount", "dealstage", "pipeline", "closedate", "hubspot_owner_id", "hs_lastmodifieddate",
              "createdate", "deal_currency_code"]
TASK_PROPS = ["hs_task_subject", "hs_task_body", "hs_task_status", "hs_task_priority", "hs_timestamp",
              "hubspot_owner_id", "hs_task_type"]
HS_ASSOC = {("deals", "contacts"): 3, ("deals", "companies"): 5, ("tasks", "contacts"): 204,
            ("tasks", "companies"): 192, ("tasks", "deals"): 216}


class HubSpotAdapter(CRMAdapter):
    kind = "hubspot"
    label = "HubSpot"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        self.api = H.RestClient("HubSpot", HUBSPOT, headers=bearer(cfg, "token", service="HubSpot"))
        self._pipelines: list[dict] | None = None

    # --- normalisers --------------------------------------------------------------------------
    @staticmethod
    def contact(o: dict) -> dict:
        p = o.get("properties") or {}
        return H.compact({"id": o.get("id"), "type": "contact",
                          "name": " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x) or None,
                          "email": p.get("email"), "phone": p.get("phone"), "company": p.get("company"),
                          "title": p.get("jobtitle"), "stage": p.get("lifecyclestage"), "lead_status": p.get("hs_lead_status"),
                          "owner_id": p.get("hubspot_owner_id"), "updated": p.get("lastmodifieddate")})

    @staticmethod
    def company(o: dict) -> dict:
        p = o.get("properties") or {}
        return H.compact({"id": o.get("id"), "type": "company", "name": p.get("name"), "domain": p.get("domain"),
                          "industry": p.get("industry"), "city": p.get("city"), "country": p.get("country"),
                          "employees": p.get("numberofemployees"), "revenue": _num(p.get("annualrevenue")),
                          "stage": p.get("lifecyclestage"), "owner_id": p.get("hubspot_owner_id"),
                          "updated": p.get("hs_lastmodifieddate")})

    async def pipelines(self) -> list[dict]:
        if self._pipelines is None:
            data = await self.api.get("/crm/v3/pipelines/deals") or {}
            self._pipelines = data.get("results") or []
        return self._pipelines

    async def stage_label(self, stage_id: str | None) -> str | None:
        for p in await self.pipelines():
            for s in p.get("stages") or []:
                if s.get("id") == stage_id:
                    return s.get("label")
        return stage_id

    async def stage_id(self, ref: str, pipeline: str | None = None) -> tuple[str, str]:
        r = ref.strip().lower()
        for p in await self.pipelines():
            if pipeline and pipeline.lower() not in (str(p.get("id")).lower(), str(p.get("label")).lower()):
                continue
            for s in p.get("stages") or []:
                if r in (str(s.get("id")).lower(), str(s.get("label")).lower()):
                    return s["id"], p["id"]
        opts = ", ".join(f"{s.get('label')}" for p in await self.pipelines() for s in p.get("stages") or [])
        raise ToolError(f"Unknown deal stage '{ref}'. Stages: {opts}.")

    async def deal(self, o: dict) -> dict:
        p = o.get("properties") or {}
        return H.compact({"id": o.get("id"), "name": p.get("dealname"), "amount": _num(p.get("amount")),
                          "currency": p.get("deal_currency_code"), "stage": await self.stage_label(p.get("dealstage")),
                          "close_date": (p.get("closedate") or "")[:10] or None, "pipeline": p.get("pipeline"),
                          "owner_id": p.get("hubspot_owner_id"), "updated": p.get("hs_lastmodifieddate")})

    @staticmethod
    def task(o: dict) -> dict:
        p = o.get("properties") or {}
        return H.compact({"id": o.get("id"), "subject": p.get("hs_task_subject"), "status": p.get("hs_task_status"),
                          "priority": p.get("hs_task_priority"), "due": p.get("hs_timestamp"),
                          "type": p.get("hs_task_type"), "body": H.clip(p.get("hs_task_body"), 500),
                          "owner_id": p.get("hubspot_owner_id")})

    # --- customers ----------------------------------------------------------------------------
    async def search_customer(self, query, kind, limit):
        kinds = ["contacts", "companies"] if kind == "any" else ["companies" if kind == "company" else "contacts"]
        if kind == "lead":
            kinds = ["contacts"]
        out: list[dict] = []
        for k in kinds:
            data = await self.api.post(f"/crm/v3/objects/{k}/search", json={
                "query": query, "limit": limit, "properties": CONTACT_PROPS if k == "contacts" else COMPANY_PROPS}) or {}
            out += [self.contact(o) if k == "contacts" else self.company(o) for o in data.get("results", [])]
        return out[: limit * len(kinds)]

    async def get_customer(self, customer_id, kind):
        for k in (["companies"] if kind == "company" else ["contacts", "companies"] if kind == "any" else ["contacts"]):
            try:
                o = await self.api.get(f"/crm/v3/objects/{k}/{customer_id}", params={
                    "properties": ",".join(CONTACT_PROPS if k == "contacts" else COMPANY_PROPS),
                    "associations": "companies,deals" if k == "contacts" else "contacts,deals"})
            except H.ApiError as e:
                if e.status == 404 and kind == "any" and k == "contacts":
                    continue
                raise
            out = self.contact(o) if k == "contacts" else self.company(o)
            assoc = o.get("associations") or {}
            for name, block in assoc.items():
                ids = [r.get("id") for r in (block or {}).get("results", [])][:20]
                if ids:
                    out[f"{name}_ids"] = ids
            return out
        raise ToolError(f"No HubSpot record '{customer_id}'.")

    async def create_customer(self, kind, fields):
        if kind == "company":
            props = H.compact({"name": fields.get("name") or fields.get("company"), "domain": fields.get("domain"),
                               "phone": fields.get("phone"), **(fields.get("properties") or {})})
            if not props.get("name"):
                raise ToolError("A company needs a name.")
            return {"created": True, **self.company(await self.api.post("/crm/v3/objects/companies", json={"properties": props}))}
        first, last = _split_name(fields.get("name"))
        props = H.compact({"firstname": first, "lastname": last, "email": fields.get("email"), "phone": fields.get("phone"),
                           "company": fields.get("company"), "jobtitle": fields.get("title"),
                           "lifecyclestage": "lead" if kind == "lead" else fields.get("stage"),
                           **(fields.get("properties") or {})})
        if not (props.get("email") or props.get("lastname")):
            raise ToolError("A contact needs at least a name or an email.")
        return {"created": True, **self.contact(await self.api.post("/crm/v3/objects/contacts", json={"properties": props}))}

    async def update_customer(self, customer_id, kind, fields):
        if kind == "company":
            props = H.compact({"name": fields.get("name"), "domain": fields.get("domain"), "phone": fields.get("phone"),
                               "lifecyclestage": fields.get("stage"), **(fields.get("properties") or {})})
            path, norm = f"/crm/v3/objects/companies/{customer_id}", self.company
        else:
            first, last = _split_name(fields.get("name"))
            props = H.compact({"firstname": first, "lastname": last, "email": fields.get("email"),
                               "phone": fields.get("phone"), "company": fields.get("company"),
                               "jobtitle": fields.get("title"), "lifecyclestage": fields.get("stage"),
                               **(fields.get("properties") or {})})
            path, norm = f"/crm/v3/objects/contacts/{customer_id}", self.contact
        if not props:
            raise ToolError("Nothing to update.")
        return {"updated": True, "changed": list(props), **norm(await self.api.patch(path, json={"properties": props}))}

    # --- deals --------------------------------------------------------------------------------
    async def list_deals(self, stage, owner, limit, query, include_closed):
        filters: list[dict] = []
        if stage:
            sid, _ = await self.stage_id(stage)
            filters.append({"propertyName": "dealstage", "operator": "EQ", "value": sid})
        elif not include_closed:
            closed = [s["id"] for p in await self.pipelines() for s in p.get("stages") or []
                      if str((s.get("metadata") or {}).get("isClosed")).lower() == "true"]
            if closed:
                filters.append({"propertyName": "dealstage", "operator": "NOT_IN", "values": closed})
        if owner:
            filters.append({"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner})
        body: dict[str, Any] = {"limit": limit, "properties": DEAL_PROPS,
                                "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}]}
        if filters:
            body["filterGroups"] = [{"filters": filters}]
        if query:
            body["query"] = query
        data = await self.api.post("/crm/v3/objects/deals/search", json=body) or {}
        return [await self.deal(o) for o in data.get("results", [])]

    async def get_deal(self, deal_id):
        o = await self.api.get(f"/crm/v3/objects/deals/{deal_id}", params={
            "properties": ",".join(DEAL_PROPS + ["description"]), "associations": "contacts,companies"})
        out = await self.deal(o)
        if (o.get("properties") or {}).get("description"):
            out["description"] = H.clip(o["properties"]["description"], 2000)
        for name, block in (o.get("associations") or {}).items():
            ids = [r.get("id") for r in (block or {}).get("results", [])][:20]
            if ids:
                out[f"{name}_ids"] = ids
        return out

    def _assoc(self, src: str, targets: dict[str, str | None]) -> list[dict]:
        out = []
        for kind, oid in targets.items():
            if oid:
                out.append({"to": {"id": str(oid)}, "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                                                "associationTypeId": HS_ASSOC[(src, kind)]}]})
        return out

    async def create_deal(self, f):
        props: dict[str, Any] = {"dealname": f["name"]}
        if f.get("amount") is not None:
            props["amount"] = str(f["amount"])
        if f.get("stage"):
            props["dealstage"], props["pipeline"] = await self.stage_id(f["stage"], f.get("pipeline"))
        else:
            pipes = await self.pipelines()
            if pipes and pipes[0].get("stages"):
                props["pipeline"] = pipes[0]["id"]
                props["dealstage"] = sorted(pipes[0]["stages"], key=lambda s: s.get("displayOrder", 0))[0]["id"]
        if f.get("close_date"):
            props["closedate"] = f["close_date"]
        props.update(f.get("properties") or {})
        body: dict[str, Any] = {"properties": props}
        assoc = self._assoc("deals", {"contacts": f.get("contact_id"), "companies": f.get("company_id")})
        if assoc:
            body["associations"] = assoc
        return {"created": True, **await self.deal(await self.api.post("/crm/v3/objects/deals", json=body))}

    async def update_deal(self, deal_id, f):
        props = H.compact({"dealname": f.get("name"), "amount": str(f["amount"]) if f.get("amount") is not None else None,
                           "closedate": f.get("close_date"), **(f.get("properties") or {})})
        if f.get("stage"):
            props["dealstage"], props["pipeline"] = await self.stage_id(f["stage"], f.get("pipeline"))
        if not props:
            raise ToolError("Nothing to update.")
        o = await self.api.patch(f"/crm/v3/objects/deals/{deal_id}", json={"properties": props})
        return {"updated": True, "changed": list(props), **await self.deal(o)}

    # --- tasks --------------------------------------------------------------------------------
    async def list_tasks(self, status, limit):
        filters = [{"propertyName": "hs_task_status", "operator": "EQ", "value": status.upper().replace(" ", "_")}] if status \
            else [{"propertyName": "hs_task_status", "operator": "NEQ", "value": "COMPLETED"}]
        data = await self.api.post("/crm/v3/objects/tasks/search", json={
            "filterGroups": [{"filters": filters}], "properties": TASK_PROPS, "limit": limit,
            "sorts": [{"propertyName": "hs_timestamp", "direction": "ASCENDING"}]}) or {}
        return [self.task(o) for o in data.get("results", [])]

    async def create_task(self, f):
        due = f.get("due") or _date(None, default_days=1)
        props = H.compact({"hs_task_subject": f["subject"], "hs_task_body": f.get("body"), "hs_task_status": "NOT_STARTED",
                           "hs_task_priority": (f.get("priority") or "").upper() or None,
                           "hs_timestamp": f"{due}T09:00:00Z" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) else due})
        body: dict[str, Any] = {"properties": props}
        assoc = self._assoc("tasks", {"contacts": f.get("contact_id"), "deals": f.get("deal_id"),
                                      "companies": f.get("company_id")})
        if assoc:
            body["associations"] = assoc
        return {"created": True, **self.task(await self.api.post("/crm/v3/objects/tasks", json=body))}

    async def update_task(self, task_id, f):
        status = f.get("status")
        if status:
            status = "COMPLETED" if status.lower() in ("done", "complete", "completed") else status.upper().replace(" ", "_")
        due = f.get("due")
        props = H.compact({"hs_task_subject": f.get("subject"), "hs_task_body": f.get("body"), "hs_task_status": status,
                           "hs_task_priority": (f.get("priority") or "").upper() or None,
                           "hs_timestamp": (f"{due}T09:00:00Z" if due and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) else due)})
        if not props:
            raise ToolError("Nothing to update.")
        return {"updated": True, "changed": list(props),
                **self.task(await self.api.patch(f"/crm/v3/objects/tasks/{task_id}", json={"properties": props}))}

    # --- pipeline -----------------------------------------------------------------------------
    async def pipeline_summary(self, pipeline, include_closed):
        pipes = await self.pipelines()
        if not pipes:
            raise ToolError("No deal pipelines found in HubSpot.")
        pipe = pipes[0]
        if pipeline:
            match = [p for p in pipes if pipeline.lower() in (str(p.get("id")).lower(), str(p.get("label")).lower())]
            if not match:
                raise ToolError(f"Unknown pipeline '{pipeline}'. Pipelines: {', '.join(p.get('label', '') for p in pipes)}.")
            pipe = match[0]
        stages = sorted(pipe.get("stages") or [], key=lambda s: s.get("displayOrder", 0))
        agg: dict[str, dict[str, float]] = {s["id"]: {"deals": 0, "amount": 0.0} for s in stages}
        after = None
        fetched = 0
        truncated = False
        while True:
            body: dict[str, Any] = {"limit": 100, "properties": ["amount", "dealstage"],
                                    "filterGroups": [{"filters": [{"propertyName": "pipeline", "operator": "EQ",
                                                                   "value": pipe["id"]}]}]}
            if after:
                body["after"] = after
            data = await self.api.post("/crm/v3/objects/deals/search", json=body) or {}
            for o in data.get("results", []):
                p = o.get("properties") or {}
                a = agg.setdefault(p.get("dealstage") or "?", {"deals": 0, "amount": 0.0})
                a["deals"] += 1
                a["amount"] += _num(p.get("amount")) or 0.0
            fetched += len(data.get("results", []))
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
            if fetched >= 2000:
                truncated = True
                break
        rows, total, weighted_total, count = [], 0.0, 0.0, 0
        for s in stages:
            meta = s.get("metadata") or {}
            closed = str(meta.get("isClosed")).lower() == "true"
            if closed and not include_closed:
                continue
            a = agg.get(s["id"], {"deals": 0, "amount": 0.0})
            prob = _num(meta.get("probability"))
            weighted = a["amount"] * prob if prob is not None else None
            rows.append(H.compact({"stage": s.get("label"), "deals": int(a["deals"]), "amount": round(a["amount"], 2),
                                   "probability": prob, "weighted": round(weighted, 2) if weighted is not None else None,
                                   "closed": closed or None}))
            total += a["amount"]
            count += int(a["deals"])
            weighted_total += weighted or 0.0
        out = {"pipeline": pipe.get("label"), "stages": rows,
               "totals": {"deals": count, "amount": round(total, 2), "weighted": round(weighted_total, 2)}}
        if truncated:
            out["note"] = "Summarized the first 2000 deals."
        return out


# =========================================================================== Salesforce
_SF_PREFIX = {"001": "Account", "003": "Contact", "00Q": "Lead", "006": "Opportunity", "00T": "Task", "005": "User"}


def soql_str(v: str) -> str:
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def soql_like(v: str) -> str:
    esc = str(v).replace("\\", "\\\\").replace("'", "\\'").replace("%", "\\%").replace("_", "\\_")
    return f"'%{esc}%'"


def _sf_clean(rec: dict, limit: int = 40) -> dict:
    out: dict[str, Any] = {}
    for k, v in rec.items():
        if k == "attributes" or v in (None, "", [], {}):
            continue
        if isinstance(v, dict):
            v = v.get("Name") or {kk: vv for kk, vv in v.items() if kk != "attributes"}
        out[k] = H.clip(v, 1000) if isinstance(v, str) else v
        if len(out) >= limit:
            break
    return out


class SalesforceAdapter(CRMAdapter):
    kind = "salesforce"
    label = "Salesforce"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        self._api: H.RestClient | None = None
        self.instance = ""

    async def api(self) -> H.RestClient:
        if self._api is None:
            _, instance = await salesforce_session(self.cfg)
            self.instance = instance

            async def auth() -> dict[str, str]:
                tok, _ = await salesforce_session(self.cfg)
                return {"Authorization": f"Bearer {tok}"}

            self._api = H.RestClient("Salesforce", f"{instance}/services/data/{SF_VERSION}", auth=auth,
                                     on_unauthorized=lambda: invalidate(self.cfg))
        return self._api

    async def soql(self, q: str) -> list[dict]:
        data = await (await self.api()).get("/query", params={"q": q}) or {}
        return data.get("records") or []

    def url(self, rid: str | None) -> str | None:
        return f"{self.instance}/{rid}" if rid and self.instance else None

    def sobject(self, rid: str, kind: str | None) -> str:
        if kind and kind != "any":
            return {"contact": "Contact", "company": "Account", "lead": "Lead"}.get(kind, kind)
        t = _SF_PREFIX.get(str(rid)[:3])
        if not t:
            raise ToolError("Can't tell the Salesforce record type from that id — pass kind.")
        return t

    def customer(self, r: dict) -> dict:
        t = (r.get("attributes") or {}).get("type") or ""
        kind = {"Account": "company", "Contact": "contact", "Lead": "lead"}.get(t, t.lower())
        return H.compact({"id": r.get("Id"), "type": kind, "name": r.get("Name"), "email": r.get("Email"),
                          "phone": r.get("Phone"), "company": r.get("Company") or (r.get("Account") or {}).get("Name"),
                          "title": r.get("Title"), "industry": r.get("Industry"), "website": r.get("Website"),
                          "status": r.get("Status"), "url": self.url(r.get("Id"))})

    def deal(self, r: dict) -> dict:
        return H.compact({"id": r.get("Id"), "name": r.get("Name"), "amount": _num(r.get("Amount")),
                          "stage": r.get("StageName"), "close_date": r.get("CloseDate"),
                          "probability": _num(r.get("Probability")), "company": (r.get("Account") or {}).get("Name"),
                          "owner": (r.get("Owner") or {}).get("Name"), "url": self.url(r.get("Id"))})

    def task(self, r: dict) -> dict:
        return H.compact({"id": r.get("Id"), "subject": r.get("Subject"), "status": r.get("Status"),
                          "priority": r.get("Priority"), "due": r.get("ActivityDate"),
                          "who": (r.get("Who") or {}).get("Name"), "related_to": (r.get("What") or {}).get("Name"),
                          "owner": (r.get("Owner") or {}).get("Name"), "url": self.url(r.get("Id"))})

    async def search_customer(self, query, kind, limit):
        if len(query.strip()) < 2:
            raise ToolError("Search text must be at least 2 characters.")
        objs = {"contact": ["Contact"], "company": ["Account"], "lead": ["Lead"]}.get(kind, ["Contact", "Account", "Lead"])
        fields = {"Contact": "Id,Name,Email,Phone,Title", "Account": "Id,Name,Industry,Website,Phone",
                  "Lead": "Id,Name,Email,Company,Status,Phone"}
        params: list[tuple[str, str]] = [("q", query.strip()), ("overallLimit", str(limit))]
        for o in objs:
            params += [("sobject", o), (f"{o}.fields", fields[o]), (f"{o}.limit", str(limit))]
        data = await (await self.api()).get("/parameterizedSearch/", params=params) or {}
        return [self.customer(r) for r in data.get("searchRecords", [])][:limit * len(objs)]

    async def get_customer(self, customer_id, kind):
        t = self.sobject(customer_id, kind)
        rec = await (await self.api()).get(f"/sobjects/{t}/{customer_id}")
        return {"type": t, "url": self.url(customer_id), **_sf_clean(rec)}

    async def _account_id(self, company: str | None) -> str | None:
        if not company:
            return None
        if re.fullmatch(r"001[A-Za-z0-9]{12,15}", company):
            return company
        rows = await self.soql(f"SELECT Id FROM Account WHERE Name = {soql_str(company)} LIMIT 1")
        return rows[0]["Id"] if rows else None

    async def create_customer(self, kind, f):
        api = await self.api()
        first, last = _split_name(f.get("name"))
        if kind == "company":
            t, body = "Account", H.compact({"Name": f.get("name") or f.get("company"), "Website": f.get("domain"),
                                            "Phone": f.get("phone")})
            if not body.get("Name"):
                raise ToolError("A company needs a name.")
        elif kind == "lead":
            t, body = "Lead", H.compact({"FirstName": first, "LastName": last or f.get("email"), "Email": f.get("email"),
                                         "Phone": f.get("phone"), "Company": f.get("company") or "Unknown",
                                         "Title": f.get("title")})
        else:
            t, body = "Contact", H.compact({"FirstName": first, "LastName": last or f.get("email"), "Email": f.get("email"),
                                            "Phone": f.get("phone"), "Title": f.get("title"),
                                            "AccountId": await self._account_id(f.get("company"))})
        body.update(f.get("properties") or {})
        res = await api.post(f"/sobjects/{t}", json=body)
        return {"created": True, "id": res.get("id"), "type": t, "url": self.url(res.get("id"))}

    async def update_customer(self, customer_id, kind, f):
        t = self.sobject(customer_id, kind)
        first, last = _split_name(f.get("name"))
        if t == "Account":
            body = H.compact({"Name": f.get("name"), "Website": f.get("domain"), "Phone": f.get("phone")})
        else:
            body = H.compact({"FirstName": first, "LastName": last, "Email": f.get("email"), "Phone": f.get("phone"),
                              "Title": f.get("title"), "Status": f.get("stage") if t == "Lead" else None})
        body.update(f.get("properties") or {})
        if not body:
            raise ToolError("Nothing to update.")
        await (await self.api()).patch(f"/sobjects/{t}/{customer_id}", json=body)
        return {"updated": True, "id": customer_id, "type": t, "changed": list(body), "url": self.url(customer_id)}

    async def list_deals(self, stage, owner, limit, query, include_closed):
        where = [] if include_closed else ["IsClosed = false"]
        if stage:
            where.append(f"StageName = {soql_str(stage)}")
        if owner:
            where.append(f"Owner.Name = {soql_str(owner)}" if "@" not in owner else f"Owner.Email = {soql_str(owner)}")
        if query:
            where.append(f"Name LIKE {soql_like(query)}")
        q = ("SELECT Id, Name, Amount, StageName, CloseDate, Probability, Account.Name, Owner.Name FROM Opportunity"
             + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY CloseDate ASC LIMIT {int(limit)}")
        return [self.deal(r) for r in await self.soql(q)]

    async def get_deal(self, deal_id):
        rec = await (await self.api()).get(f"/sobjects/Opportunity/{deal_id}")
        return {"url": self.url(deal_id), **_sf_clean(rec)}

    async def create_deal(self, f):
        body = H.compact({"Name": f["name"], "StageName": f.get("stage") or self.cfg.get("default_stage") or "Prospecting",
                          "CloseDate": f.get("close_date") or _date(None, default_days=30),
                          "Amount": f.get("amount"), "AccountId": await self._account_id(f.get("company_id"))})
        body.update(f.get("properties") or {})
        api = await self.api()
        res = await api.post("/sobjects/Opportunity", json=body)
        if f.get("contact_id"):
            await api.post("/sobjects/OpportunityContactRole", json={"OpportunityId": res.get("id"),
                                                                     "ContactId": f["contact_id"], "IsPrimary": True})
        return {"created": True, "id": res.get("id"), "stage": body["StageName"], "close_date": body["CloseDate"],
                "url": self.url(res.get("id"))}

    async def update_deal(self, deal_id, f):
        body = H.compact({"Name": f.get("name"), "StageName": f.get("stage"), "CloseDate": f.get("close_date"),
                          "Amount": f.get("amount")})
        body.update(f.get("properties") or {})
        if not body:
            raise ToolError("Nothing to update.")
        await (await self.api()).patch(f"/sobjects/Opportunity/{deal_id}", json=body)
        return {"updated": True, "id": deal_id, "changed": list(body), "url": self.url(deal_id)}

    async def list_tasks(self, status, limit):
        where = f"Status = {soql_str(status)}" if status else "IsClosed = false"
        q = ("SELECT Id, Subject, Status, Priority, ActivityDate, Who.Name, What.Name, Owner.Name FROM Task "
             f"WHERE {where} ORDER BY ActivityDate ASC NULLS LAST LIMIT {int(limit)}")
        return [self.task(r) for r in await self.soql(q)]

    async def create_task(self, f):
        prio = {"high": "High", "medium": "Normal", "normal": "Normal", "low": "Low"}.get((f.get("priority") or "").lower())
        body = H.compact({"Subject": f["subject"], "Description": f.get("body"), "ActivityDate": f.get("due"),
                          "Priority": prio, "Status": "Not Started", "WhoId": f.get("contact_id"),
                          "WhatId": f.get("deal_id") or f.get("company_id")})
        res = await (await self.api()).post("/sobjects/Task", json=body)
        return {"created": True, "id": res.get("id"), "url": self.url(res.get("id"))}

    async def update_task(self, task_id, f):
        status = f.get("status")
        if status and status.lower() in ("done", "complete", "completed"):
            status = "Completed"
        prio = {"high": "High", "medium": "Normal", "normal": "Normal", "low": "Low"}.get((f.get("priority") or "").lower())
        body = H.compact({"Subject": f.get("subject"), "Description": f.get("body"), "ActivityDate": f.get("due"),
                          "Priority": prio, "Status": status})
        if not body:
            raise ToolError("Nothing to update.")
        await (await self.api()).patch(f"/sobjects/Task/{task_id}", json=body)
        return {"updated": True, "id": task_id, "changed": list(body), "url": self.url(task_id)}

    async def pipeline_summary(self, pipeline, include_closed):
        stages = await self.soql("SELECT MasterLabel, SortOrder, IsClosed, IsWon, DefaultProbability FROM OpportunityStage "
                                 "WHERE IsActive = true ORDER BY SortOrder")
        where = "" if include_closed else " WHERE IsClosed = false"
        agg = await self.soql("SELECT StageName, COUNT(Id) deals, SUM(Amount) amount, SUM(ExpectedRevenue) weighted "
                              f"FROM Opportunity{where} GROUP BY StageName")
        by_stage = {r.get("StageName"): r for r in agg}
        rows, total, weighted_total, count = [], 0.0, 0.0, 0
        order = [s.get("MasterLabel") for s in stages] + [k for k in by_stage if k not in {s.get("MasterLabel") for s in stages}]
        closed = {s.get("MasterLabel"): bool(s.get("IsClosed")) for s in stages}
        for name in order:
            r = by_stage.get(name)
            if r is None or (closed.get(name) and not include_closed):
                continue
            amount, weighted, deals = _num(r.get("amount")) or 0.0, _num(r.get("weighted")) or 0.0, int(r.get("deals") or 0)
            rows.append(H.compact({"stage": name, "deals": deals, "amount": round(amount, 2), "weighted": round(weighted, 2),
                                   "closed": closed.get(name) or None}))
            total += amount
            weighted_total += weighted
            count += deals
        return {"pipeline": "Opportunities", "stages": rows,
                "totals": {"deals": count, "amount": round(total, 2), "weighted": round(weighted_total, 2)}}


ADAPTERS: dict[str, type[CRMAdapter]] = {"hubspot": HubSpotAdapter, "salesforce": SalesforceAdapter}


async def crm_adapter(ctx: ToolContext, account: str | None = None) -> Any:
    kind, cfg = await H.pick_connector(ctx, CRM_KINDS, account, what="CRM")
    return ADAPTERS[kind](ctx, cfg)


# =========================================================================== tools
group = ToolGroup(
    "crm", "CRM",
    "Look up and update customers, deals and tasks in HubSpot or Salesforce, and summarize the sales pipeline.",
    prefix="crm", icon="briefcase", category="business", connector_kinds=CRM_KINDS,
)


def _lim(n: int, hi: int = 100) -> int:
    return max(1, min(int(n or 20), hi))


def _record_preview(title: str):
    def render(a: dict) -> str:
        skip = {"account"}
        rows = [(k.replace("_", " ").capitalize(), v) for k, v in a.items() if k not in skip and v not in (None, "", [], {})]
        return H.preview(title, [("CRM", a.get("account") or "default")] + rows)
    return render


@group.tool(risk=Risk.LOW, scope="read")
async def search_customer(ctx: ToolContext, query: str, kind: CustomerKind = "any", limit: int = 10,
                          account: str | None = None) -> dict:
    """Find customers by name, email, company or domain.

    Args:
        query: Search text.
        kind: contact, company, lead (Salesforce) or any.
        limit: Maximum results per record type.
        account: CRM connector name or kind (hubspot, salesforce).
    """
    ad = await crm_adapter(ctx, account)
    rows = await ad.search_customer(query, kind, _lim(limit, 50))
    return {"crm": ad.label, "count": len(rows), "customers": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def get_customer(ctx: ToolContext, customer_id: str, kind: CustomerKind = "any",
                       account: str | None = None) -> dict:
    """Get a customer record (with associated companies/deals where available).

    Args:
        customer_id: Record id.
        kind: contact, company, lead or any (Salesforce infers it from the id).
        account: CRM connector name or kind.
    """
    ad = await crm_adapter(ctx, account)
    return {"crm": ad.label, **await ad.get_customer(customer_id, kind)}


@group.tool(risk=Risk.MEDIUM, scope="write", preview=_record_preview("Create CRM customer"))
async def create_customer(ctx: ToolContext, kind: Literal["contact", "company", "lead"] = "contact",
                          name: str | None = None, email: str | None = None, phone: str | None = None,
                          company: str | None = None, title: str | None = None, domain: str | None = None,
                          properties: dict[str, Any] | None = None, account: str | None = None) -> dict:
    """Create a contact, company or lead.

    Args:
        kind: contact, company or lead.
        name: Person's full name (or company name for kind=company).
        email: Email address.
        phone: Phone number.
        company: Company name (contacts/leads; Salesforce links an existing account by name).
        title: Job title.
        domain: Company website domain.
        properties: Extra CRM fields by API name, e.g. {"industry": "SaaS"}.
        account: CRM connector name or kind.
    """
    ad = await crm_adapter(ctx, account)
    return {"crm": ad.label, **await ad.create_customer(kind, {"name": name, "email": email, "phone": phone,
                                                                 "company": company, "title": title, "domain": domain,
                                                                 "properties": properties})}


@group.tool(risk=Risk.MEDIUM, scope="write", preview=_record_preview("Update CRM customer"))
async def update_customer(ctx: ToolContext, customer_id: str, kind: Literal["contact", "company", "lead", "any"] = "any",
                          name: str | None = None, email: str | None = None, phone: str | None = None,
                          company: str | None = None, title: str | None = None, stage: str | None = None,
                          properties: dict[str, Any] | None = None, account: str | None = None) -> dict:
    """Update a customer's fields.

    Args:
        customer_id: Record id.
        kind: contact, company, lead or any (HubSpot defaults to contact).
        name: New name.
        email: New email.
        phone: New phone.
        company: New company name (HubSpot contacts).
        title: New job title.
        stage: Lifecycle stage (HubSpot) / lead status (Salesforce leads).
        properties: Other fields by API name.
        account: CRM connector name or kind.
    """
    ad = await crm_adapter(ctx, account)
    k = "contact" if kind == "any" and ad.kind == "hubspot" else kind
    return {"crm": ad.label, **await ad.update_customer(customer_id, k, {
        "name": name, "email": email, "phone": phone, "company": company, "title": title, "stage": stage,
        "properties": properties})}


@group.tool(risk=Risk.LOW, scope="read")
async def list_deals(ctx: ToolContext, stage: str | None = None, owner: str | None = None, query: str | None = None,
                     include_closed: bool = False, limit: int = 25, account: str | None = None) -> dict:
    """List deals/opportunities (open ones by default).

    Args:
        stage: Only this stage (label, e.g. "Negotiation").
        owner: Owner id (HubSpot) or name/email (Salesforce).
        query: Words in the deal name.
        include_closed: Include closed won/lost deals.
        limit: Maximum deals.
        account: CRM connector name or kind.
    """
    ad = await crm_adapter(ctx, account)
    rows = await ad.list_deals(stage, owner, _lim(limit), query, include_closed)
    return {"crm": ad.label, "count": len(rows), "deals": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def get_deal(ctx: ToolContext, deal_id: str, account: str | None = None) -> dict:
    """Get a deal/opportunity with its details and associations.

    Args:
        deal_id: Deal id.
        account: CRM connector name or kind.
    """
    ad = await crm_adapter(ctx, account)
    return {"crm": ad.label, **await ad.get_deal(deal_id)}


@group.tool(risk=Risk.MEDIUM, scope="write", preview=_record_preview("Create deal"))
async def create_deal(ctx: ToolContext, name: str, amount: float | None = None, stage: str | None = None,
                      close_date: str | None = None, pipeline: str | None = None, contact_id: str | None = None,
                      company_id: str | None = None, properties: dict[str, Any] | None = None,
                      account: str | None = None) -> dict:
    """Create a deal/opportunity (optionally linked to a contact and company).

    Args:
        name: Deal name.
        amount: Deal value.
        stage: Stage label (default: first stage of the pipeline / "Prospecting").
        close_date: Expected close date YYYY-MM-DD (Salesforce default: +30 days).
        pipeline: HubSpot pipeline label/id.
        contact_id: Contact to associate.
        company_id: Company/account id (Salesforce also accepts an account name).
        properties: Extra fields by API name.
        account: CRM connector name or kind.
    """
    ad = await crm_adapter(ctx, account)
    return {"crm": ad.label, **await ad.create_deal({
        "name": name, "amount": amount, "stage": stage, "close_date": _date(close_date), "pipeline": pipeline,
        "contact_id": contact_id, "company_id": company_id, "properties": properties})}


@group.tool(risk=Risk.MEDIUM, scope="write", preview=_record_preview("Update deal"))
async def update_deal(ctx: ToolContext, deal_id: str, name: str | None = None, amount: float | None = None,
                      stage: str | None = None, close_date: str | None = None, pipeline: str | None = None,
                      properties: dict[str, Any] | None = None, account: str | None = None) -> dict:
    """Update a deal: stage, amount, close date, name or other fields.

    Args:
        deal_id: Deal id.
        name: New name.
        amount: New value.
        stage: New stage label.
        close_date: New close date YYYY-MM-DD.
        pipeline: HubSpot pipeline (when moving between pipelines).
        properties: Other fields by API name.
        account: CRM connector name or kind.
    """
    ad = await crm_adapter(ctx, account)
    return {"crm": ad.label, **await ad.update_deal(deal_id, {
        "name": name, "amount": amount, "stage": stage, "close_date": _date(close_date), "pipeline": pipeline,
        "properties": properties})}


@group.tool(risk=Risk.LOW, scope="read")
async def list_tasks(ctx: ToolContext, status: str | None = None, limit: int = 25, account: str | None = None) -> dict:
    """List CRM tasks (open ones by default, soonest due first).

    Args:
        status: Filter by status (HubSpot: NOT_STARTED, IN_PROGRESS, WAITING, COMPLETED; Salesforce: e.g. "Not Started").
        limit: Maximum tasks.
        account: CRM connector name or kind.
    """
    ad = await crm_adapter(ctx, account)
    rows = await ad.list_tasks(status, _lim(limit))
    return {"crm": ad.label, "count": len(rows), "tasks": rows}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("subject", "body"), preview=_record_preview("Create CRM task"))
async def create_task(ctx: ToolContext, subject: str, due: str | None = None, body: str | None = None,
                      priority: str | None = None, contact_id: str | None = None, deal_id: str | None = None,
                      company_id: str | None = None, account: str | None = None) -> dict:
    """Create a follow-up task (optionally linked to a contact, deal or company).

    Args:
        subject: Task subject.
        due: Due date YYYY-MM-DD (HubSpot default: tomorrow).
        body: Notes.
        priority: high, medium or low.
        contact_id: Related contact.
        deal_id: Related deal.
        company_id: Related company/account.
        account: CRM connector name or kind.
    """
    ad = await crm_adapter(ctx, account)
    return {"crm": ad.label, **await ad.create_task({"subject": subject, "due": _date(due), "body": body,
                                                     "priority": priority, "contact_id": contact_id, "deal_id": deal_id,
                                                     "company_id": company_id})}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("subject", "body"), preview=_record_preview("Update CRM task"))
async def update_task(ctx: ToolContext, task_id: str, subject: str | None = None, status: str | None = None,
                      due: str | None = None, priority: str | None = None, body: str | None = None,
                      account: str | None = None) -> dict:
    """Update a CRM task (e.g. mark it completed or move the due date).

    Args:
        task_id: Task id.
        subject: New subject.
        status: New status ("completed" marks it done).
        due: New due date YYYY-MM-DD.
        priority: high, medium or low.
        body: New notes.
        account: CRM connector name or kind.
    """
    ad = await crm_adapter(ctx, account)
    return {"crm": ad.label, **await ad.update_task(task_id, {"subject": subject, "status": status, "due": _date(due),
                                                              "priority": priority, "body": body})}


@group.tool(risk=Risk.LOW, scope="read")
async def pipeline_summary(ctx: ToolContext, pipeline: str | None = None, include_closed: bool = False,
                           account: str | None = None) -> dict:
    """Summarize the sales pipeline: number of deals and total / weighted value per stage.

    Args:
        pipeline: HubSpot pipeline label/id (default: the first pipeline).
        include_closed: Include closed won/lost stages.
        account: CRM connector name or kind.
    """
    ad = await crm_adapter(ctx, account)
    return {"crm": ad.label, **await ad.pipeline_summary(pipeline, include_closed)}


# =========================================================================== connector kinds
@H.connector_test
async def _test_hubspot(cfg: dict) -> tuple[bool, str]:
    ad = HubSpotAdapter(ToolContext(org_id="_test", user_id=None), cfg)
    try:
        info = await ad.api.get("/account-info/v3/details") or {}
        return True, f"Connected to HubSpot portal {info.get('portalId', '?')} ({info.get('uiDomain', 'app.hubspot.com')})."
    except H.ApiError as e:
        if e.status != 403:
            raise
        await ad.api.get("/crm/v3/objects/contacts", params={"limit": 1})
        return True, "Connected to HubSpot (contacts readable)."


@H.connector_test
async def _test_salesforce(cfg: dict) -> tuple[bool, str]:
    ad = SalesforceAdapter(ToolContext(org_id="_test", user_id=None), cfg)
    api = await ad.api()
    limits = await api.get("/limits") or {}
    remaining = (limits.get("DailyApiRequests") or {}).get("Remaining")
    return True, f"Connected to Salesforce ({ad.instance})" + (f"; {remaining} API calls left today." if remaining else ".")


register_connector_kind(ConnectorKindSpec(
    kind="hubspot", name="HubSpot", description="HubSpot CRM contacts, companies, deals and tasks.",
    icon="briefcase", category="business", tool_groups=["crm"],
    fields=[
        ConnectorField("token", "Private app access token", type="password", required=True, secret=True,
                       placeholder="pat-na1-…",
                       help="Settings → Integrations → Private apps. Scopes: crm.objects.contacts/companies/deals "
                            "read+write, crm.schemas.deals.read, crm.objects.owners.read, tickets (optional)."),
    ],
    docs_url="https://developers.hubspot.com/docs/api/private-apps", test=_test_hubspot,
))

register_connector_kind(ConnectorKindSpec(
    kind="salesforce", name="Salesforce", description="Salesforce accounts, contacts, leads, opportunities and tasks.",
    icon="briefcase", category="business", tool_groups=["crm"],
    fields=[
        ConnectorField("instance_url", "Instance URL", type="url", required=True,
                       placeholder="https://yourcompany.my.salesforce.com"),
        ConnectorField("client_id", "Connected app consumer key"),
        ConnectorField("client_secret", "Consumer secret", type="password", secret=True),
        ConnectorField("refresh_token", "Refresh token", type="password", secret=True,
                       help="From the OAuth web-server flow (scopes: api refresh_token). Or leave empty to use the "
                            "client-credentials flow (enable it on the connected app with a run-as user)."),
        ConnectorField("access_token", "Access token (alternative)", type="password", secret=True,
                       help="A session token for quick tests — expires."),
        ConnectorField("login_url", "Login URL (optional)", type="url", placeholder="https://login.salesforce.com",
                       help="Use https://test.salesforce.com for sandboxes."),
    ],
    docs_url="https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_flows.htm", test=_test_salesforce,
))
