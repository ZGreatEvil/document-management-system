Reviewed for 14.0.1.0.0

# Handover: document_management_system

Odoo 14 Community module. Project files live in SharePoint, one folder per unique code, one members-only link per file. It extends `document_library` and `document_numbering` and never edits them.

## Layout
- `models/graph_client.py`: the only way to call Microsoft Graph. Token cached per worker, retries 429/5xx honouring `Retry-After` (max 60 s), renews the token once on a 401, raises `DmsGraphError(status, code)`. Logs status and Graph error codes only.
- `models/sharepoint.py`: plain functions over the client: SharePoint name rules, folders, upload (one PUT up to 10 MiB, else an upload session in 5 MiB chunks), link + grant, move, link and permission clean-up.
- `models/project_project.py`: link a code (folder), the member rule (`_dms_compute_member_users`), the member sync and its crons, the nightly drift check.
- `models/dms_file.py`: `dms.file` (never deleted or copied; one shared file per number) and `dms.file.grant` (what SharePoint was last told).
- `models/member_sources.py`: what makes projects re-check their members: users, groups, project access, cancelled numbers.
- `wizard/dms_file_add_wizard.py`: Add file (upload or pasted link) and its checks.
- `static/src/js/dms_help.js`: the help page; add a What's New entry on every version bump.

## Business rules
- Members of a project = the active internal users who can open the project in Odoo (Odoo's own project access rules decide), with a Microsoft 365 address. Agreed on 2026-11-05.
- A file of a cancelled number is deleted from SharePoint by the next sync; Odoo keeps its record as removed. Agreed on 2026-11-05.
- Only Document Management administrators link a unique code; one project per code; the code cannot change once files exist.
- Numbered files: active numbers of the project's code, of the user's own division (numbering administrators excepted). One shared file per number; replacing needs confirmation. When two people add one at the same moment, the unique index stops the second inside a savepoint: they get a clear message and their upload is removed (or their pasted file moved back).
- The file count on the kanban card and the project's Files button counts current (shared) files, the same files the list behind it shows; superseded and removed ones stay on the Files tab as history.
- Archived unique codes cannot be linked.
- Pasted links: a file (not a folder), directly in the user's own upload folder, in the configured library, not added before.
- Protected fields change only through the module's own methods running with `sudo()`, checked with `env.su`.
- Never store file contents in Odoo; never show SharePoint's `webUrl` of a project file; never create anonymous, organisation-wide or edit links; never give access to a folder except a user's own upload folder.

## Local setup and tests
- Needs Odoo 14 with `project`, `document_library` and `document_numbering` in the addons path, Python 3.7.
- Tests mock Graph (`tests/common.py`, `FakeGraph`) and never call Microsoft:
  `odoo-bin -c <conf> -d <new database> -i document_management_system --test-enable --test-tags /document_management_system --stop-after-init`
- They create their own companies, codes and users, so they also pass on a database holding real data.

## Deploying
Replace the module folder on the server, run `-u document_management_system` on every database that has it, restart Odoo (assets are cached until a restart).

Uninstalling removes everything the module added to Odoo (checked: no tables, columns, views, menus, crons or groups left; the connection settings stay, as Odoo keeps parameters). It never touches SharePoint: folders, files and their links stay there.

## External services
- Microsoft Graph, application permission `Sites.Selected` with `fullcontrol` on one site. The client secret is the `dms_graph_client_secret` key in `odoo.conf`; tenant, client, site, library and folder IDs are system parameters (Settings > Document Management).
- Not yet verified against a live tenant: run `scripts/poc_graph.py` (repository root) against the test site first.

## Known traps
- Odoo hides archived users when reading a many2many: compare stored members with `active_test=False`, or deactivated users keep their access.
- Member upkeep must stay one access query per user (`_dms_visible_to`), never one per user and project: the first version took 25 seconds per user change with 150 projects and 80 users. A test guards it.
- Computed fields that depend on the user need `@api.depends_context('uid')`.
- SharePoint does not say which permissions are inherited, so only links (and the uploader's own direct permission after a move) are ever deleted.
- Odoo 14 draws its screens on phones as a shrunken desktop page; install OCA `web_responsive` for phones (not yet tested with this module).
- Text the module adds uses `.o_dms_note` ($gray-600, 4.69:1) rather than `text-muted` (2.07:1 on white) to meet WCAG AA. Odoo's own purple buttons and links (3.97:1) are left as Odoo styles them, like in every other app.
