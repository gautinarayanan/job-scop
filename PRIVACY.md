# Privacy policy

Job Scout is a personal, self-hosted tool. Each person runs their own copy on
their own GitHub account and connects it to their own Google account.

**What it accesses.** Only the Google Drive files that Job Scout itself creates
(the `drive.file` permission): a "Job Scout" folder, the scrape files in it, and
one tracking spreadsheet. It cannot see, read or change any other file in your
Drive, and it requests no access to Gmail, contacts, or your profile.

**What it stores.** Public job listings (title, company, location, salary,
link) scraped from job boards, written to your own Google Drive. Nothing is
stored anywhere else. There is no Job Scout server and no database.

**Who can see it.** Only you, through your Google Drive. The Google login token
is stored as an encrypted secret in your own GitHub repository and is never
shared with the author of Job Scout or anyone else.

**Email.** If you set up email notifications, summaries are sent from your own
email account to an address you choose.

**Removing access.** Revoke it at any time at
<https://myaccount.google.com/permissions>. Delete the "Job Scout" folder to
remove all data.

Questions: open an issue on this repository.
