# Document Management System

Odoo 14 Community module (in progress) that stores project files in SharePoint: one folder per project code, and one view-only, specific-people link per file, shared only with the project's members.

## Status

Phase 0, proof of concept. The Odoo module itself is not written yet.

## Phase 0: Microsoft Graph proof of concept

`scripts/poc_graph.py` runs every Microsoft Graph call the module needs against a **test** SharePoint site. It checks whether an app registration with only the `Sites.Selected` application permission (granted `fullcontrol` on that one site) is enough.

Steps: token and permission check, site and library, folders, small upload, large upload session (and cancel), `createLink` (view, specific people), `grant`, permission list, `revokeGrants`, `/shares` resolve, move and rename, delete a link, `invite` on a folder.

Everything happens inside a new `PoC-<timestamp>` folder at the root of the library. The script prints HTTP status codes and Graph error codes only, never tokens, secrets or response bodies.

### Requirements

- Python 3.7 or later
- `requests`

### Settings

Settings come from environment variables only. Never put them in a file in this repository.

| Variable | Required | Meaning |
| --- | --- | --- |
| `DMS_TENANT_ID` | yes | Entra ID tenant id |
| `DMS_CLIENT_ID` | yes | App registration (client) id |
| `DMS_CLIENT_SECRET` | yes | Client secret |
| `DMS_SITE_ID` | yes | Test site id |
| `DMS_TEST_MEMBER_EMAIL` | yes | Test user who receives the file link |
| `DMS_TEST_INTAKE_EMAIL` | no | Test user who receives the intake folder (default: the member) |
| `DMS_DRIVE_ID` | no | Document library id (default: the site's default library) |
| `DMS_TEST_PASTED_LINK` | no | A link made with "Copy link" in SharePoint, to resolve through `/shares` |

### Run

```powershell
python scripts\poc_graph.py
```

The script ends with a manual check: the test member opens the printed link and notes whether the folder path is visible anywhere, and a different user confirms that the same link is refused.

Remove the test folder afterwards:

```powershell
python scripts\poc_graph.py --cleanup PoC-<timestamp>
```

Only a folder whose name starts with `PoC-` at the library root can be removed this way. It goes to the site recycle bin.
