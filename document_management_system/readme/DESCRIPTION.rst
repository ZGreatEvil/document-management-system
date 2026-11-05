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
