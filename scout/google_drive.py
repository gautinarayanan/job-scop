"""Google Drive + Sheets: one archive file per scrape, one master tracker.

    Job Scout/
      Job Scout - Master          Sheet. "Jobs" tab: every job ever found, with a
                                  Status dropdown (default "Not applied") and a
                                  link to the scrape file it came from.
                                  Hidden "Seen" tab: dedupe memory.
      Scrapes/
        2026-09-28_jobslist.csv   one per scrape, never modified afterwards
        2026-09-30_jobslist.csv

Auth is OAuth as *you*, with the narrow `drive.file` scope, not a service
account:

* A service account has no Drive storage of its own, so it cannot create files
  in a personal Gmail Drive, even inside a folder shared with it.
* `drive.file` only reaches files this app created; it cannot see the rest of
  your Drive. It is a non-sensitive scope, so the OAuth app can be set to
  "In production" without Google review, and the login does not expire after
  7 days the way Testing-mode logins do.

Everything is found or created by name, so there are no file IDs to configure.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from datetime import date, datetime
from typing import Dict, List, Optional, Set

log = logging.getLogger("scout.google")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER = "application/vnd.google-apps.folder"
SHEET = "application/vnd.google-apps.spreadsheet"

MASTER_SUFFIX = " - Master"
JOBS_TAB = "Jobs"
SEEN_TAB = "Seen"
MASTER_HEADERS = ["Status", "Applied On", "Title", "Company", "Location", "Salary",
                  "Site", "Link", "Found", "CSV File", "Notes"]
STATUSES = ["Not applied", "Applied", "Interviewing", "Offer", "Rejected",
            "Ghosted", "Withdrawn", "Not interested"]

RETRIES = 5  # googleapiclient retries 429/5xx with exponential backoff


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    missing = [k for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN") if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing Google secrets: {', '.join(missing)}. Run authorize.py first.")
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())  # fail fast with a clear error if the token is bad
    return creds


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _text(value: str) -> Dict:
    # stringValue is stored verbatim: scraped text can never become a formula.
    return {"userEnteredValue": {"stringValue": value or ""}}


def _link(url: str, label: str) -> Dict:
    if not url.startswith(("https://", "http://")) or '"' in url:
        return _text(url)
    return {"userEnteredValue": {"formulaValue": f'=HYPERLINK("{url}", "{label.replace(chr(34), chr(34) * 2)}")'}}


class GoogleStore:
    def __init__(self, folder_name: str):
        from googleapiclient.discovery import build

        creds = _creds()
        self.drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.calls = 0

        self.root_id = self._folder(folder_name)
        self.scrapes_id = self._folder("Scrapes", parent=self.root_id)
        self.master_id, self.master_url = self._master(folder_name + MASTER_SUFFIX)
        self.jobs_tab = self._sheet_ids(self.master_id)[JOBS_TAB]

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _run(self, request):
        self.calls += 1
        return request.execute(num_retries=RETRIES)

    def _list(self, q: str, order: str = "createdTime", size: int = 10) -> List[Dict]:
        return self._run(self.drive.files().list(
            q=q, spaces="drive", pageSize=size, orderBy=order,
            fields="files(id, name, createdTime, webViewLink)")).get("files", [])

    def _folder(self, name: str, parent: Optional[str] = None) -> str:
        q = f"name = '{_q(name)}' and mimeType = '{FOLDER}' and trashed = false"
        if parent:
            q += f" and '{parent}' in parents"
        found = self._list(q)
        if found:
            return found[0]["id"]
        body = {"name": name, "mimeType": FOLDER}
        if parent:
            body["parents"] = [parent]
        log.info(f"Creating Drive folder '{name}'")
        return self._run(self.drive.files().create(body=body, fields="id"))["id"]

    def _sheet_ids(self, spreadsheet_id: str) -> Dict[str, int]:
        info = self._run(self.sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties(sheetId,title)"))
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in info["sheets"]}

    def _batch(self, requests: List[Dict]) -> Dict:
        return self._run(self.sheets.spreadsheets().batchUpdate(
            spreadsheetId=self.master_id, body={"requests": requests}))

    # ── master sheet ─────────────────────────────────────────────────────────
    def _master(self, name: str) -> tuple:
        found = self._list(f"name = '{_q(name)}' and mimeType = '{SHEET}' "
                           f"and '{self.root_id}' in parents and trashed = false")
        if found:
            return found[0]["id"], found[0]["webViewLink"]

        log.info(f"Creating master sheet '{name}'")
        meta = self._run(self.drive.files().create(
            body={"name": name, "mimeType": SHEET, "parents": [self.root_id]},
            fields="id, webViewLink"))
        self.master_id = meta["id"]
        first = next(iter(self._sheet_ids(self.master_id).values()))
        self._batch([
            {"updateSheetProperties": {
                "properties": {"sheetId": first, "title": JOBS_TAB,
                               "gridProperties": {"frozenRowCount": 1}},
                "fields": "title,gridProperties.frozenRowCount"}},
            {"addSheet": {"properties": {"title": SEEN_TAB, "hidden": True}}},
            {"updateCells": {
                "range": {"sheetId": first, "startRowIndex": 0, "endRowIndex": 1},
                "rows": [{"values": [{"userEnteredValue": {"stringValue": h},
                                      "userEnteredFormat": {"textFormat": {"bold": True}}}
                                     for h in MASTER_HEADERS]}],
                "fields": "userEnteredValue,userEnteredFormat.textFormat.bold"}},
        ])
        self._run(self.sheets.spreadsheets().values().update(
            spreadsheetId=self.master_id, range=f"{SEEN_TAB}!A1",
            valueInputOption="RAW", body={"values": [["key", "first_seen"]]}))
        return self.master_id, meta["webViewLink"]

    def add_jobs(self, jobs: List[Dict], file_name: str, file_url: str) -> None:
        """Append new jobs to the master. Never edits, sorts or clears existing
        rows, so your Status, Applied On and Notes are never touched."""
        if not jobs:
            return
        rows = [{"values": [
            _text("Not applied"), _text(""),
            _text(j["Title"]), _text(j["Company"]), _text(j["Location"]), _text(j["Salary"]),
            _text(j["Site"]), _link(j["Link"], "Company site" if j.get("_direct") else j["Site"]), _text(j["Found"]),
            _link(file_url, file_name), _text(""),
        ]} for j in jobs]
        status_col = MASTER_HEADERS.index("Status")
        self._batch([
            {"appendCells": {"sheetId": self.jobs_tab, "rows": rows, "fields": "userEnteredValue"}},
            # Open-ended range: covers every row, including ones added later.
            {"setDataValidation": {
                "range": {"sheetId": self.jobs_tab, "startRowIndex": 1,
                          "startColumnIndex": status_col, "endColumnIndex": status_col + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": s} for s in STATUSES]},
                         "showCustomUi": True, "strict": True}}},
        ])

    # ── seen-jobs memory, so a job is only ever added once ───────────────────
    def load_seen(self) -> Set[str]:
        res = self._run(self.sheets.spreadsheets().values().get(
            spreadsheetId=self.master_id, range=f"{SEEN_TAB}!A2:A"))
        return {row[0] for row in res.get("values", []) if row}

    def add_seen(self, keys: List[str], today: str) -> None:
        if keys:
            self._run(self.sheets.spreadsheets().values().append(
                spreadsheetId=self.master_id, range=f"{SEEN_TAB}!A1",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": [[k, today] for k in keys]}))

    # ── scrape files ─────────────────────────────────────────────────────────
    def last_scrape_date(self, tz: str = "UTC") -> Optional[date]:
        """Date of the newest scrape file, used to honour scrape_every_days."""
        files = self._list(f"'{self.scrapes_id}' in parents and trashed = false",
                           order="createdTime desc", size=1)
        if not files:
            return None
        from zoneinfo import ZoneInfo
        created = datetime.fromisoformat(files[0]["createdTime"].replace("Z", "+00:00"))
        return created.astimezone(ZoneInfo(tz)).date()

    def upload_scrape(self, jobs: List[Dict], columns: List[str], today: str, fmt: str) -> tuple:
        """Upload this scrape as <date>_jobslist.csv (or .json). Returns (name, url)."""
        from googleapiclient.http import MediaIoBaseUpload

        if fmt == "json":
            data = json.dumps([{c: j[c] for c in columns} for j in jobs], indent=2).encode()
            mime = "application/json"
        else:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(jobs)
            data, mime = buf.getvalue().encode("utf-8"), "text/csv"

        # A forced second run on the same day gets _2, _3, ... rather than a
        # second file with an identical name.
        base, n = f"{today}_jobslist", 1
        name = f"{base}.{fmt}"
        while self._list(f"name = '{_q(name)}' and '{self.scrapes_id}' in parents and trashed = false"):
            n += 1
            name = f"{base}_{n}.{fmt}"

        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
        meta = self._run(self.drive.files().create(
            body={"name": name, "parents": [self.scrapes_id]},
            media_body=media, fields="id, webViewLink"))
        return name, meta["webViewLink"]
