"""One-time Google sign-in. Run locally, never in CI.

    python authorize.py path/to/client_secret.json
    python authorize.py path/to/client_secret.json --save ~/.job-scout/google.env

Opens a browser, asks you to allow access to files this app creates in your
Drive (scope: drive.file, nothing else), then prints the three values to save
as GitHub repository secrets. With --save they are written to a private file
(mode 600) instead of the screen, e.g. for `gh secret set -f <file>`.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from scout.google_drive import SCOPES


def main() -> None:
    ap = argparse.ArgumentParser(description="One-time Google sign-in for Job Scout.")
    ap.add_argument("client_secret", help="the Desktop-app OAuth client JSON from Google Cloud")
    ap.add_argument("--save", metavar="FILE", help="write the secrets to FILE instead of printing them")
    args = ap.parse_args()
    secret_file = Path(args.client_secret).expanduser()
    client = json.loads(secret_file.read_text())
    if "installed" not in client:
        sys.exit("That file is not a 'Desktop app' OAuth client. Create one of type Desktop app.")

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        sys.exit("Google returned no refresh token. Remove the app at "
                 "https://myaccount.google.com/permissions and run this again.")

    values = {
        "GOOGLE_CLIENT_ID": creds.client_id,
        "GOOGLE_CLIENT_SECRET": creds.client_secret,
        "GOOGLE_REFRESH_TOKEN": creds.refresh_token,
    }
    if args.save:
        out = Path(args.save).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines(f"{k}={v}\n" for k, v in values.items())
        print(f"\nSaved to {out} (readable only by you):")
        for k, v in values.items():
            print(f"  {k:22} {v[:6]}…{v[-4:]}")
        print(f"\nUpload to GitHub with:  gh secret set -f {out}")
    else:
        print("\nSave these as GitHub repository secrets "
              "(Settings -> Secrets and variables -> Actions -> New repository secret):\n")
        for k, v in values.items():
            print(f"{k:22} = {v}")
    print("\nTreat the refresh token like a password. Keep client_secret.json outside this repo.")


if __name__ == "__main__":
    main()
