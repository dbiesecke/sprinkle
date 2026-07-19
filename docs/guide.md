# A Guide to Sprinkle

This document contains a basic starting guide to use Sprinkle. It will guide you in setting up Sprinkle
volumes and how to backup/recover data.

## Install
This section describes the installations steps required to install Sprinkle for Windows and Linux.
Keep in mind that the actual steps might change based on your environment.

### Install RClone
The first thing to do is to install RClone. The best way to install RClone is by following the official
guide [here](https://rclone.org/install/).  
After the installation is complete, the following 2 commands,
for Windows and Linux should report the respective installed version:
##### for Windows:
```
C:\>rclone.exe --version
rclone v1.43.1
- os/arch: windows/amd64
- go version: go1.11
```
##### for Linux:
```
$ rclone --version
rclone v1.43
- os/arch: linux/amd64
- go version: go1.11
```
You may need to modify the PATH environment variable to make the command available to the system.
Consult the related operating system documentation on the different way to modify the PATH environment
variable if needed.  
Now that RClone is installed, let's proceed...

### Install Python 3
Sprinkle is developed in Python 3. This chapter describes how to setup Python for Windows and Linux. The
best way to install Python is by following the official documentation [here](https://www.python.org/doc/).
After the installation is completed, the following two commands will return the respective version.  
Make sure the installed version is at least 3.6 or later.
##### for Windows:
```
C:\>python --version
Python 3.6.5
```
##### for Linux:
```
$  python3 --version
Python 3.6.2
```

On Linux the actual command might be called **python3** due to compatibility and co-existemce with
version 2.

### Install Sprinkle and Dependencies
Now, we can install Sprinkle and the necessary dependencies by following the officiel guide
[here](https://mmontuori.github.io/sprinkle/).  
After the installation is complete, executing the following two commands will report Sprinkle's version
for the respective operating system:
##### for Windows:
```
C:\>python sprinkle.py --version
VERSION:
    1.1.0, module version: 1.1.0, rclone module version: 1.1.0
```
##### for Linux:
```
$  sprinkle.py --version
VERSION:
    1.1.0, module version: 1.1.0, rclone module version: 1.1.0
```
At this point, you can check prerequisites with the following command:
```
sprinkle.py --check-prereq
checking prerequisites, examine the error messages below...
**** PASSED! ****
```
If anything other than PASSES! is displayed, resolve the problem until the PASSED! message is displayed.

## Configure
Now, that everything is installed and functional. Let's proceed with the configuration of RClone and
Sprinkle.
### Configure RClone
Configuring RClone is probably the most complex item to perform.
##### for Windows:
```
C:\>rclone config
Current remotes:

Name                 Type
====                 ====
nasbackup            drive
nasbackup2           drive
nasbackup3           drive

e) Edit existing remote
n) New remote
d) Delete remote
r) Rename remote
c) Copy remote
s) Set configuration password
q) Quit config
e/n/d/r/c/s/q>
```
##### for Linux:
```
$ rclone config
Current remotes:

Name                 Type
====                 ====
nasbackup            drive
nasbackup2           drive
nasbackup3           drive

e) Edit existing remote
n) New remote
d) Delete remote
r) Rename remote
c) Copy remote
s) Set configuration password
q) Quit config
e/n/d/r/c/s/q>
```

### Configure Sprinkle
As long as RClone is configured correctly with volumes, Sprinkle works out of the box, however,
Sprinkle can be configured by editing the file sprinkle.conf. The file that ships with sprinkle
has all default values assigned which work for most installations, however, it's possible that
values have to be tweaked for specific installations. All values in sprinkle.conf are commented.
The recommended way to create a local configuration is the interactive config command. By default it
writes to `~/.sprinkle/sprinkle.conf`.

Sprinkle uses the same path precedence for reading and writing configuration:

1. `-c/--conf`
2. a non-empty `SPRINKLE_CONFIG`
3. `~/.sprinkle/sprinkle.conf`

For normal commands an explicitly selected CLI or environment file must exist. The Home file is
loaded automatically when present and otherwise remains optional. The Docker image deliberately sets
`SPRINKLE_CONFIG=/config/sprinkle.conf` so `/config` can be mounted as persistent configuration.

##### interactive configuration:
```
$ python3 sprinkle.py config
rclone_move: move files instead of copying them [Y/n]: Y
delete_files: delete files after 1-way sync [y/N]:
debug output (-d) [Y/n]:
rclone_sa_count [5]:
drive_id [XXXXX]: GDRIVE_FOLDER_ID
rclone_sa_dir [/etc/rclone/sa]:
sa_cache_ttl_hours [72]:
sa_refresh {missing|stale|all|none} [stale]:
sa_clean_invalid {none|quarantine|delete} [quarantine]:
```

The generated config can also store defaults equivalent to:

```
--rclone-sa-count 5 --drive-id GDRIVE_FOLDER_ID -d --rclone-sa-dir /etc/rclone/sa
```

Sprinkle never passes an inherited `RCLONE_CONFIG` to rclone. The key is also ignored when it appears
in `rclone.env`. Select a classic rclone file with `--rclone-conf` or `rclone_config`; service-account
operations generate a temporary configuration and pass it explicitly with `--config`.

For example, the **debug** value
##### sprinkle.conf debug value:
```
# run sprinkle in debug mode. Expect a lot of output
# value: true|false
# debug=false
```
##### in order to change the value:
```
# run sprinkle in debug mode. Expect a lot of output
# value: true|false
debug=true
```
This modification will set the debug value to true and output additional information that can be
used for development or troubleshooting.

### Configure Google Drive Service Accounts
Sprinkle can import Google Drive service-account JSON files into a managed local store, dedupe them,
validate them with `rclone about --json`, and cache quota data in SQLite. Service-account JSON files
contain secrets, so do not commit or paste raw `private_key` values.

##### import service accounts:
```
$ python3 sprinkle.py sa-import /path/to/service-accounts
service account import: found 46 json files
service account import [1/46] imported: sa-77757096d8ee3b33cf290d6a.json
service account import [2/46] imported: sa-774a8b4465f01f42f1e45016.json
...
service account import complete
scanned:      46
validated:    46
imported:     46
duplicates:   0
invalid:      0
validation errors: 0
quarantined:  0
deleted:      0
store:        /home/user/.sprinkle/service-accounts
database:     /home/user/.sprinkle/sa-cache.sqlite3
```

Invalid JSON files, rejected service accounts, missing projects/users, and unknown quota results are
recorded as invalid. With the default `sa_clean_invalid=quarantine`, the affected JSON file is copied
to the managed quarantine directory instead of being deleted from the source path.

##### inspect imported accounts and cached quota:
```
$ python3 sprinkle.py --drive-id GDRIVE_FOLDER_ID sa-stats
SERVICE ACCOUNT SUMMARY
active:      46
duplicates:  0
invalid:     0
refreshed:   0
errors:      0
stale:       0
unknown:     0
```

Most Google Drive service accounts are effectively small 10-15 GB quotas. Sprinkle therefore uses the
cached quota data for placement and keeps extra headroom for large files so uploads are not sent to an
account that only barely fits the file.

## Verify
### Verify Sprinkle->RClone->Volumes Connections
To verify proper Sprinkle operation, use any commands, like the stats command:
##### for Windows:
```
C:\>python sprinkle.py stats
calculating total and free space...
REMOTE                          SIZE                 FREE      %FREE
=============== ==================== ==================== ==========
nasbackup:                       15G                   0G          1
nasbackup2:                      15G                   1G          7
nasbackup3:                      15G                   0G          3
--------------- -------------------- -------------------- ----------
total:                           45G                   1G          3
```
##### for Linux:
```
sprinkle.py stats
calculating total and free space...
REMOTE                          SIZE                 FREE      %FREE
=============== ==================== ==================== ==========
nasbackup:                       15G                   0G          1
nasbackup2:                      15G                   1G          7
nasbackup3:                      15G                   0G          3
--------------- -------------------- -------------------- ----------
total:                           45G                   1G          3
```

## Backup
### Backup a Directory via Sprinkle
To backup (sprinkle) a local directory over to clustered volumes, use:
##### for Windows:
```
C:\>rclone backup C:\dir_to_backup
```
##### for Linux:
```
$ rclone backup /dir_to_backup
```

## Restore
### Restore a Previously Backed up Directory via Sprinkle
To restore a previously backed up directory, use:
##### for Windows:
```
C:\>rclone restore C:/dir_to_backup c:/restore_here
```
##### for Linux:
```
$ rclone restore /dir_to_backup /restore_here
```

## List
### List Directory Content via Sprinkle
In order to list files located on a clustered volume use the following commands:
##### for Windows:
```
C:\>python sprinkle.py ls c:/dir_to_list
retrieving file list from: nasbackup:/dir_to_list/router1...
retrieving file list from: nasbackup2:/dir_to_list/router1...
retrieving file list from: nasbackup3:/dir_to_list/router1...
--- NAME                              SIZE MOD TIME            REMOTE
--- ---------------------------- --------- ------------------- ---------------
--- /dir_to_list/test.txt               15 2018-11-09:02:05:34 nasbackup2:
-d- /dir_to_list/router1                -1 2018-11-10:00:56:29 nasbackup2:
```
##### for Linux:
```
$ sprinkle-py ls /dir_to_list
retrieving file list from: nasbackup:/dir_to_list/router1...
retrieving file list from: nasbackup2:/dir_to_list/router1...
retrieving file list from: nasbackup3:/dir_to_list/router1...
--- NAME                              SIZE MOD TIME            REMOTE
--- ---------------------------- --------- ------------------- ---------------
--- /dir_to_list/test.txt               15 2018-11-09:02:05:34 nasbackup2:
-d- /dir_to_list/router1                -1 2018-11-10:00:56:29 nasbackup2:
```

## Help
### Built-in Help
Sprinkle comes with a very extensive built-in help. Any option can be used from as a command line
argument or set into the sprinkle.conf file. To invoke the built-in help use:
##### for Windows:
```
C:\>python sprinkle.py --help
NAME:
    sprinkle - the cloud clustered backup utility

SYNOPSIS:
    sprinkle.py [options} {command} {arg...arg}

DESCRIPTION:
    Sprinkle is a volume clustering utility. It presents all the RClone available volumes as a single clustered volume.
    It supports 1-way sync mainly for backup and recovery.
    Sprinkle uses the excellent [RClone](https://rclone.org) software for cloud volume access.

EXAMPLES:
    sprinkle.py config
    sprinkle.py ls /backup
    sprinkle.py backup /dir_to_backup
    sprinkle.py restore /backup /opt/restore_dir
    sprinkle.py stats
    sprinkle.py -c /home/sprinkle/sprinkle.conf ls /backup
    sprinkle.py sa-import /path/to/service-accounts
    sprinkle.py --drive-id GDRIVE_FOLDER_ID sa-stats
...
...
```
##### for Linux:
```
$ sprinkle-py --help
NAME:
    sprinkle - the cloud clustered backup utility

SYNOPSIS:
    sprinkle.py [options} {command} {arg...arg}

DESCRIPTION:
    Sprinkle is a volume clustering utility. It presents all the RClone available volumes as a single clustered volume.
    It supports 1-way sync mainly for backup and recovery.
    Sprinkle uses the excellent [RClone](https://rclone.org) software for cloud volume access.

EXAMPLES:
    sprinkle.py config
    sprinkle.py ls /backup
    sprinkle.py backup /dir_to_backup
    sprinkle.py restore /backup /opt/restore_dir
    sprinkle.py stats
    sprinkle.py -c /home/sprinkle/sprinkle.conf ls /backup
    sprinkle.py sa-import /path/to/service-accounts
    sprinkle.py --drive-id GDRIVE_FOLDER_ID sa-stats
...
...
```

## Contributing
Please read [CONTRIBUTE.md](https://mmontuori.github.io/sprinkle/CONTRIBUTE) for details on our code of conduct, and the process for submitting pull requests to us.

## Versioning
We use [SemVer](http://semver.org/) for versioning. For the versions available, see the [tags on this repository](https://github.com/your/project/tags). 

## Authors
* **Michael Montuori** - *Initial work* - [mmontuori](https://github.com/mmontuori)

## License
This project is licensed under the GPLv3 License - see the [LICENSE.md](https://github.com/mmontuori/sprinkle/LICENSE) file for details
