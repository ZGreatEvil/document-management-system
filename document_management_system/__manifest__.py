# -*- coding: utf-8 -*-
{
    'name': 'Document Management System',
    'version': '14.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Project files in SharePoint: one folder per unique code, one members-only link per file',
    'author': 'ZGreatEvil',
    'website': 'https://github.com/ZGreatEvil/document-management-system',
    'license': 'LGPL-3',
    'depends': ['project', 'document_library', 'document_numbering'],
    'data': [
        'security/dms_security.xml',
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
}
