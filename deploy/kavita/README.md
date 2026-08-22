# Kavita member EPUB library

This deployment is isolated from the stock-selection application:

- Hostname: `books.longchangwudao.cn`
- Runtime user: `kavita` (system account, no login shell)
- Program: `/opt/kavita/releases/v0.9.0.2`, selected by `/opt/kavita/current`
- Mutable configuration: `/var/lib/kavita/config`
- EPUB library root: `/var/lib/kavita/books`
- Listener: `127.0.0.1:5000` only
- Public entry point: Nginx HTTPS reverse proxy

The installed release is the official `kavita-linux-x64.tar.gz` asset for
Kavita `v0.9.0.2`. Its verified SHA-256 is:

```text
7bec1747802a03daf3b5d42fabfa288c491b1d0dc5d47c4cce1dc32b6391b323
```

## First-run setup

1. Open `https://books.longchangwudao.cn` and create the administrator.
2. In **Settings → Themes**, select `longchang-wudao`.
3. Create an EPUB library rooted at `/var/lib/kavita/books`.
4. Create member accounts without administrator privileges.
5. Upload EPUB files as `root` into `/var/lib/kavita/books`, then set ownership
   to `root:kavita` and directories/files to `0750`/`0640` respectively.
6. Trigger a library scan in Kavita.

The deployed administrator locale is Simplified Chinese. Kavita currently
reports about 94% Chinese translation coverage, so a few upstream labels may
remain in English.

EPUB files must be below a subdirectory of the library root; files placed
directly in `/var/lib/kavita/books` are ignored. Use this layout:

```text
/var/lib/kavita/books/
└── Author name/
    └── Book title.epub
```

The library's advanced file filter enables only `Epub`; archives, PDF and image
scanning are disabled.

Kavita can read the EPUB directory but cannot modify it. It also cannot read
`/var/lib/masterstock`.

## Service operations

```sh
systemctl status kavita
systemctl restart kavita
journalctl -u kavita --since today
```

Kavita rewrites `wwwroot/index.html` at startup. The installed release therefore
links that one file to `/var/lib/kavita/config/wwwroot-index.html`; the remainder
of `/opt/kavita` stays read-only under systemd hardening.

Network binding is controlled by `/var/lib/kavita/config/appsettings.json` and
must remain `127.0.0.1:5000`.

## Backup and restore

`kavita-backup.timer` creates a stopped-service snapshot of
`/var/lib/kavita/config` every day at about 03:15 Asia/Shanghai. Archives are
stored in `/var/backups/kavita` with 14-day retention. The short stop gives a
consistent copy of the SQLite database and its WAL files.

These archives protect accounts, progress, annotations, settings, covers and
themes. The authoritative EPUB files under `/var/lib/kavita/books` are not part
of this archive and need an ECS snapshot or separate off-host copy.

## Member invitations

For each ordinary member, grant access to `会员书库` and enable `Login`,
`Change Password`, and `Bookmark`. Leave `Admin`, `Download`,
`Change Restriction`, `Read Only`, and `Promote` disabled. `Read Only` is not
used because members need to save progress, bookmarks and annotations.

Annotation sharing and viewing shared annotations are disabled by default, so
notes remain private to each account.

Before a restore, stop Kavita and preserve the current config directory. Extract
the selected archive into an empty staging directory, inspect it, then replace
only `/var/lib/kavita/config`, restore ownership to `kavita:kavita`, and start
the service. Never restore anything into `/var/lib/masterstock`.

## Upgrade and rollback

Install each new release in a new `/opt/kavita/releases/<version>` directory.
Verify its checksum and release security notes before changing the `current`
symlink. Stop Kavita, take a configuration backup, switch the symlink, recreate
the mutable `wwwroot/index.html` link if required, and start the service.

Rollback consists of stopping Kavita, pointing `/opt/kavita/current` to the
previous release, restoring the matching pre-upgrade configuration archive when
the database schema changed, and starting Kavita again.
