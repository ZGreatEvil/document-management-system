==========================
Document Management System
==========================

.. |badge| image:: https://img.shields.io/badge/licence-LGPL--3-blue.png
    :target: http://www.gnu.org/licenses/lgpl-3.0-standalone.html
    :alt: License: LGPL-3

|badge|

Project files in SharePoint, managed from Odoo's Project app.

* A project linked to a unique code (from ``document_numbering``) gets its own SharePoint folder,
  ``Projects/<year>/<unique code>/``.
* Members add files from the project's **Files** tab: uploaded from their computer, or moved from their
  personal upload folder in SharePoint by pasting a link (for large files).
* A file that belongs to a document number is named after it, e.g. ``QTN-001_SDX001_XI_26.pdf``; a number has one
  current file, and a new revision replaces it (the old one is kept as superseded).
* Every file gets one view-only "specific people" link, granted only to the project's members: everyone who can
  open the project in Odoo. Anyone else who opens the link, even a forwarded one, is refused by SharePoint.
* When the people who can open a project change, the access to all its files follows within minutes. Files of a
  cancelled number are deleted from SharePoint (they stay in the site's recycle bin).
* File contents are never stored in Odoo.

.. contents::
   :local:

Configuration
=============

**1. SharePoint site (a Microsoft 365 administrator, once)**

* Create one site, e.g. "Project Documents", **without** a Microsoft 365 group. One or two owners; the Members and
  Visitors groups stay empty.
* External sharing: "Only people in your organization". Only site owners may share.
* In its document library, create two top folders: ``Intake`` and ``Projects``. They must be in the same library:
  files are moved from one to the other, and Microsoft Graph cannot move between libraries.

**2. App registration (Microsoft Entra ID)**

* Register an app and give it the Microsoft Graph **application** permission ``Sites.Selected``; grant admin
  consent.
* Give the app ``fullcontrol`` on the one site: ``POST /sites/{site-id}/permissions`` with
  ``{"roles": ["fullcontrol"], "grantedToIdentities": [{"application": {"id": "<app id>"}}]}``. This single call
  needs ``Sites.FullControl.All`` and is done once by an administrator.
* Create a client secret.

**3. Odoo server**

* Put the secret in ``odoo.conf``, in the ``[options]`` section, and restart Odoo::

    dms_graph_client_secret = <secret>

  It is never stored in the database and never shown in Odoo.
* If a reverse proxy sits in front of Odoo, its upload size limit must be at least Odoo's (128 MB by default,
  system parameter ``web.max_file_upload_size``).
* For phones, install OCA ``web_responsive``: Odoo 14 Community otherwise draws its screens on phones as a shrunken
  desktop page.

**4. Odoo settings** (Settings > Document Management)

* Tenant ID, Client ID, Site ID, Library ID, and the item IDs of the ``Projects`` and ``Intake`` folders.
* Click **Test connection**.
* On each user, tab **Document Management**: the Microsoft 365 sign-in address, when it differs from the login.
* Give the Document Management **Administrator** role (Settings > Users) to the people who link projects to
  unique codes and remove files.

Usage
=====

**Link a project** (Document Management administrators): open the project, tab **Files**, fill in
**Unique Code**. Its SharePoint folder is created at once.

**Add a file** (members): tab **Files** > **Add file**.

* *Upload from this computer*: choose the file (up to the upload limit).
* *Paste a SharePoint link*, for large files: click **My upload folder**, put the file there (not in a sub-folder),
  use **Copy link** on it in SharePoint and paste the link. The file is moved into the project folder and the
  copied link is deleted.
* Optionally choose the **Document Number**: the file is then named after it. Only active numbers of the project's
  unique code and of your own division can be chosen. When the number already has a file, tick
  **Replace the current file**; for a file with the same name, add a revision to the number first.

**Open a file**: **Open** in the file list. SharePoint refuses anyone who cannot open the project in Odoo.

**Who can open the files**: everyone who can open the project in Odoo (project tab **Settings**, *Visibility* and
invited users). The list is shown on the **Files** tab and followed in SharePoint every 5 minutes; administrators
can click **Sync now**.

**Remove a file** (administrators): open the file > **Remove**. Its links are deleted and it is renamed
"... (removed <date>)" so its name is free again; the file itself stays in SharePoint.

A guide in Bahasa Indonesia is under Document Library > Help > **Project Files Guide**.

Credits
=======

Contributors
~~~~~~~~~~~~

* ZGreatEvil (https://github.com/ZGreatEvil)
