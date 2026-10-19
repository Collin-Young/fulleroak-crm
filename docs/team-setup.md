# Team setup

This private repository contains the CRM application, campaign tools, tests, and aerial renderer. Live leads, offer databases, photos, recordings, transcripts, OAuth credentials, API keys, and saved Vumber sessions stay outside Git.

## Install on Windows

Use Python 3.12 or 3.13 and Git. Windows is currently required for the encrypted Flask signing key and Vumber token storage.

```powershell
git clone https://github.com/Collin-Young/fulleroak-crm.git
cd fulleroak-crm
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in your own service settings in `.env`. The CRM creates a fresh `data/leads.db` on its first run. Access to this repository does not copy the existing customer database or automatically connect Gmail or Vumber.

For the DigitalOcean deployment, `deploy/fulleroak-gmail-sync.service` and
`deploy/fulleroak-gmail-sync.timer` poll the authorized Gmail account every two
minutes. OAuth files belong under `/var/lib/fulleroak-crm/gmail`, outside the
repository, and remain readable only by the `fulleroak` service account. The
lead dashboard reloads every 30 seconds while visible and pauses while a form
field is focused.

## Use one shared team instance

Choose one Windows computer to host the CRM and its database. Run this command from the repository directory:

```powershell
.\.venv\Scripts\python.exe serve_crm.py --host 0.0.0.0 --port 5000
```

This entry point always requires login and uses Waitress. Visit `http://localhost:5000` on that computer to create the initial owner account before opening access to teammates. Under **Team & audit**, the owner can add manager, agent, or viewer accounts. Teammates on the same private network can use `http://HOST-COMPUTER:5000` with their own accounts. Keep access on a trusted private network or VPN; use an HTTPS reverse proxy for access across an untrusted network. This setup does not publish or configure hosting automatically.

All teammates must use that same hosted instance to share leads, messages, and campaign state. Separate clones start with separate databases. Do not put SQLite files in a Git repository or synchronize a live SQLite database through a shared folder. Run one CRM server process for a shared campaign queue; the current workers and login limiter are in process.

The desktop command `python crm_app.py` is for local use and disables login. Use `serve_crm.py` for a team instance.

## Existing records and integrations

To move existing records to a different host, stop the source CRM and workflow worker, make a SQLite backup, and transfer it through your approved private file channel to `data/leads.db`. Transfer required offer data and media separately. Keep a backup before migration. A new Windows user must reconnect Vumber and establish its own Gmail OAuth session because its DPAPI-encrypted session cannot be moved. Linux hosts such as Render must set a stable `CRM_DATA_ENCRYPTION_KEY` and reconnect Vumber once. Do not run the old and new hosts against the same campaign audience simultaneously.

## Render

The repository includes `render.yaml` for a paid Render web service with a 5 GB persistent disk. Connect the private GitHub repository as a Blueprint and deploy. Render generates the CRM cookie and data-encryption secrets. Copy `leads.db`, `all_fop_offers.db`, and referenced media into `/opt/render/project/src/data` with Render's SSH/SCP transfer after the service is created. Reconnect Vumber from the cloud CRM; the Windows-encrypted local Vumber session must not be copied. Add `OPENROUTER_API_KEY` in Render later if AI transcription and drafting are needed.

Keep a single Render instance. SQLite and the in-process campaign scheduler are not safe for horizontal scaling. The service uses HTTPS-only session cookies on Render.

## DigitalOcean

The `deploy` directory includes the systemd and Nginx files used by the shared DigitalOcean installation. The application runs as the unprivileged `fulleroak` user on `127.0.0.1:5050`; Nginx provides public HTTPS. Set `CRM_DATA_DIR=/var/lib/fulleroak-crm`, `CRM_SECURE_COOKIES=true`, and `CRM_PROXY_HEADERS=true` in the root-owned environment file. Keep the database and customer media in `/var/lib/fulleroak-crm`, with `/opt/fulleroak-crm/data` linked to that directory.

The certificate timer retries the canonical `crm.geoleads.land` certificate while newly created DNS records propagate, then disables itself after success. The `sslip.io` hostname keeps the CRM available over HTTPS during propagation.

Opening **Vumber connection** lets an owner or manager connect the shared instance. For Gmail sync, supply your own Google Desktop OAuth client file and follow the README. Configure model keys separately on the host if transcription or AI features are needed.

## Property aerials and campaigns

`property_map_generator.py` is bundled from the Property Report Card project, so aerial generation no longer requires a second project folder. The pipeline joins the full LandInsights export by parcel link, checks APNs, reads its `KML Geometry`, renders Esri imagery, and produces MMS-sized aerials.

The Wild Horse import and inspection helpers refer to local campaign files that are intentionally excluded from Git. Provide those inputs separately before running them. Importing a campaign creates a draft; review it in **SMS Campaigns** before starting it. New CSV campaigns pause on server restart and require an explicit restart from the campaign page.

## Verify

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## Repository access

The GitHub owner can invite teammates under **Settings → Collaborators**. Repository access and CRM user accounts are separate: collaborators can download code, while CRM accounts control access to the shared running instance.
