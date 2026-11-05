# Changelog

## 14.0.1.0.0 (2026-11-05)

First version.

### Added
- Projects can be linked to a unique code; linking creates the SharePoint folder `Projects/<year>/<code>/`.
- Files tab on the project: add a file by upload, or by pasting a link to a file in the user's own upload folder (for large files). Files can be named after a document number; a new revision replaces the current file, which is kept as superseded.
- One view-only, specific-people link per file, granted to everyone who can open the project in Odoo.
- File access sync every 5 minutes, **Sync now** for administrators, and a nightly check that deletes links Odoo did not make and recreates a lost one.
- Files of a cancelled document number are deleted from SharePoint (recycle bin) by the next sync.
- **Remove** a file (administrators): its links are deleted and it is renamed so its name is free again.
- Settings > Document Management (connection, **Test connection**); Microsoft 365 address on the user form.
- Kanban card: unique code and a Files box. Help > Project Files Guide (Bahasa Indonesia) with What's New.

### Security
- Files are visible in Odoo to project members and Document Management administrators only; link, location and state fields can only be changed by the module itself (checked with `env.su`, never a context flag).
- The client secret is read from `odoo.conf` only. Logs hold HTTP status and Graph error codes only.

### Deploy
1. Set up the SharePoint site and the app registration (README, Configuration).
2. Add `dms_graph_client_secret` to `odoo.conf`.
3. Copy the module into the addons path, install it (`-i document_management_system`), restart Odoo.
4. Fill in Settings > Document Management and click **Test connection**.
