odoo.define('document_management_system.Help', function (require) {
"use strict";

/**
 * Help > Project Files: the guide to project files in plain Bahasa Indonesia, with this module's What's New
 * at the end. Plain-string DOM, like the Document Library help pages.
 *
 * MAINTENANCE: on every version bump add an entry at the TOP of RELEASES. Write what users notice and what they
 * need to do, not what code changed.
 */

var AbstractAction = require('web.AbstractAction');
var core = require('web.core');

var RELEASES = [
    {
        version: '14.0.1.0.0', date: '4 November 2026',
        title: 'File proyek di SharePoint',
        items: [
            'Proyek dengan kode unik kini punya tab <strong>Files</strong> untuk menyimpan file proyek di SharePoint.',
            'Setiap file punya satu tautan yang hanya bisa dibuka anggota proyek.',
            'File besar bisa ditambahkan lewat <strong>folder unggah</strong> pribadi Anda.',
            'Akses ke file mengikuti anggota proyek secara otomatis.'
        ],
        todo: 'Tidak ada. Bila menu atau tombol baru belum tampil, tekan <strong>Ctrl + F5</strong>.'
    }
];

var SECTIONS = [
    {
        id: 'apa', title: 'Untuk apa',
        body: '<p>File proyek (penawaran, BAST, foto, laporan) disimpan di <strong>SharePoint</strong>, satu folder untuk ' +
            'setiap kode unik proyek. Anda mengelolanya dari Odoo, di proyek yang bersangkutan.</p>' +
            '<p>Setiap file punya <strong>satu tautan</strong>. Tautan itu hanya bisa dibuka oleh anggota proyek. ' +
            'Orang lain yang membukanya, termasuk bila tautan diteruskan, akan ditolak.</p>'
    },
    {
        id: 'siapa', title: 'Siapa yang bisa membuka file',
        body: '<p>Semua orang yang <strong>bisa membuka proyek itu di Odoo</strong>:</p><ul>' +
            '<li>bila proyek hanya untuk orang yang diundang: orang yang diundang;</li>' +
            '<li>bila proyek untuk semua karyawan: semua karyawan;</li>' +
            '<li>admin proyek (Project Administrator) selalu bisa.</li></ul>' +
            '<p>Pengaturannya ada di tab <strong>Settings</strong> proyek (Visibility). Daftarnya terlihat di tab ' +
            '<strong>Files</strong>, bagian <em>Who can open the files</em>. Bila akses proyek berubah, akses ke ' +
            'semua file ikut berubah dalam beberapa menit.</p>'
    },
    {
        id: 'tambah', title: 'Menambah file',
        body: '<ol><li>Buka proyek, lalu tab <strong>Files</strong>.</li>' +
            '<li>Klik <strong>Add file</strong>.</li>' +
            '<li>Pilih <strong>Upload from this computer</strong>, lalu pilih filenya.</li>' +
            '<li>Bila file ini untuk sebuah nomor dokumen, pilih nomornya di <strong>Document Number</strong>. ' +
            'Nama file akan mengikuti nomor itu, misalnya <code>QTN-001_SDX001_XI_26.pdf</code>. ' +
            'Bila tidak, biarkan kosong.</li>' +
            '<li>Klik <strong>Add file</strong>.</li></ol>' +
            '<p>Batas unggah 128 MB. Untuk file yang lebih besar, pakai folder unggah (di bawah).</p>'
    },
    {
        id: 'folder', title: 'File besar: folder unggah saya',
        body: '<ol><li>Di tab <strong>Files</strong>, klik <strong>My upload folder</strong>. Folder pribadi Anda ' +
            'di SharePoint terbuka. Hanya Anda yang bisa memakainya.</li>' +
            '<li>Unggah file ke folder itu (langsung di folder itu, jangan di dalam subfolder).</li>' +
            '<li>Di SharePoint, klik kanan file, pilih <strong>Copy link</strong>.</li>' +
            '<li>Di Odoo, klik <strong>Add file</strong>, pilih <strong>Paste a SharePoint link</strong>, ' +
            'tempel tautannya, lalu klik <strong>Add file</strong>.</li></ol>' +
            '<p>File dipindahkan ke folder proyek. Tautan lama dari <em>Copy link</em> dihapus.</p>'
    },
    {
        id: 'nomor', title: 'Nomor dokumen dan revisi',
        body: '<ul><li>Satu nomor hanya punya <strong>satu file aktif</strong>.</li>' +
            '<li>Untuk versi baru, tambahkan dulu revisi pada nomornya (di aplikasi Numbering), lalu tambahkan file ' +
            'dan centang <strong>Replace the current file</strong>. File lama tetap tersimpan dengan status ' +
            '<em>Superseded</em>.</li>' +
            '<li>Anda hanya bisa memakai nomor dari divisi Anda sendiri, dan hanya nomor yang aktif.</li>' +
            '<li>Bila sebuah nomor <strong>dibatalkan</strong>, file-filenya dihapus dari SharePoint dalam beberapa ' +
            'menit. Pemilik situs masih bisa memulihkannya dari Recycle Bin.</li></ul>'
    },
    {
        id: 'masalah', title: 'Kendala umum',
        body: '<dl>' +
            '<dt>"Only members of this project can add files"</dt><dd>Anda belum punya akses ke proyek ini. ' +
            'Minta Project Manager mengundang Anda ke proyek.</dd>' +
            '<dt>"... is not in your upload folder"</dt><dd>Pindahkan file ke folder unggah Anda sendiri ' +
            '(bukan subfolder, bukan folder orang lain), lalu salin tautannya lagi.</dd>' +
            '<dt>"... is a folder"</dt><dd>Salin tautan file-nya, bukan tautan folder.</dd>' +
            '<dt>"... has already been added"</dt><dd>File itu sudah ada di sebuah proyek.</dd>' +
            '<dt>"... is already in the project folder"</dt><dd>Untuk versi baru, tambahkan revisi pada nomornya dulu.</dd>' +
            '<dt>"Someone else added a file for ... at the same moment"</dt><dd>Orang lain menambahkan file untuk ' +
            'nomor yang sama tepat pada saat yang sama. Muat ulang proyek untuk melihatnya. Bila file Anda yang ' +
            'benar, tambahkan lagi dan centang <strong>Replace the current file</strong>.</dd>' +
            '<dt>SharePoint menolak saat membuka file</dt><dd>Anda tidak (lagi) menjadi anggota proyek, atau ' +
            'perubahan anggota belum selesai diproses. Tunggu beberapa menit dan coba lagi.</dd>' +
            '<dt>Tidak ada tab Files atau tombol Add file</dt><dd>Proyek belum dihubungkan ke kode unik. Minta admin ' +
            'Document Management menghubungkannya.</dd></dl>'
    },
    {
        id: 'admin', title: 'Untuk admin Document Management',
        body: '<ul><li><strong>Menghubungkan proyek:</strong> di tab <strong>Files</strong> proyek, isi ' +
            '<strong>Unique Code</strong>. Folder di SharePoint dibuat otomatis.</li>' +
            '<li><strong>Sync now:</strong> memperbarui akses semua file proyek sekarang juga, tanpa menunggu ' +
            'pembaruan otomatis (setiap 5 menit).</li>' +
            '<li><strong>Remove:</strong> di form file, menghapus tautannya sehingga tidak ada yang bisa membukanya ' +
            'lewat Odoo. File tetap ada di SharePoint dengan nama "... (removed tanggal)", sehingga file pengganti ' +
            'untuk nomor yang sama bisa ditambahkan.</li>' +
            '<li><strong>Alamat Microsoft 365:</strong> di form user, tab <em>Document Management</em>. ' +
            'Bila kosong, login user yang dipakai.</li>' +
            '<li><strong>Koneksi:</strong> Settings &gt; Document Management, lalu <strong>Test connection</strong>.</li></ul>'
    }
];

var DmsHelp = AbstractAction.extend({
    hasControlPanel: false,

    start: function () {
        this.$el.addClass('o_dms_help');
        this.$el.html(this._render());
        return this._super.apply(this, arguments);
    },

    _render: function () {
        var toc = SECTIONS.map(function (s) {
            return '<li><a href="#dms_help_' + s.id + '">' + s.title + '</a></li>';
        }).join('') + '<li><a href="#dms_help_baru">Apa yang baru</a></li>';
        var sections = SECTIONS.map(function (s) {
            return '<section class="o_dms_help_section" id="dms_help_' + s.id + '"><h2>' + s.title + '</h2>' +
                s.body + '</section>';
        }).join('');
        var releases = RELEASES.map(function (r) {
            return '<div class="o_dms_help_release"><h3>' + r.title + '</h3>' +
                '<p class="o_dms_help_muted">Versi ' + r.version + ' &middot; ' + r.date + '</p>' +
                '<ul>' + r.items.map(function (i) { return '<li>' + i + '</li>'; }).join('') + '</ul>' +
                '<p><strong>Yang perlu Anda lakukan:</strong> ' + r.todo + '</p></div>';
        }).join('');
        return '<div class="o_dms_help_page">' +
            '<h1>File Proyek</h1>' +
            '<p class="o_dms_help_lead">Panduan menyimpan dan membuka file proyek di SharePoint.</p>' +
            '<nav class="o_dms_help_toc" aria-label="Daftar isi"><ul>' + toc + '</ul></nav>' +
            sections +
            '<section class="o_dms_help_section" id="dms_help_baru"><h2>Apa yang baru</h2>' + releases + '</section>' +
            '</div>';
    }
});

core.action_registry.add('dms_help', DmsHelp);

return DmsHelp;
});
