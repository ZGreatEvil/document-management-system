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
