#!/usr/bin/env python3
"""
sprinkle : the cloud clustered backup utility
"""
__author__ = "Michael Montuori [michael.montuori@gmail.com]"
__copyright__ = "Copyright 2017 Michael Montuori. All rights reserved."
__credits__ = ["Warren Crigger"]
__license__ = "GPLv3"
__version__ = "1.1"
__revision__ = "0"
__docformat__ = "reStructuredText"

from libsprinkle import clsync
from libsprinkle import rclone
from libsprinkle import config
from libsprinkle import common
from libsprinkle import service_accounts
from libsprinkle import smtp_email
from libsprinkle import sprinkle_daemon
import logging
import getopt
import sys
import traceback
import os
import tempfile
try:
    from filelock import Timeout, FileLock
except:
    print('FileLock library not found. run: "pip3 install filelock"')
    quit()


lock = FileLock("sprinkle.lock", timeout=1)

__drive_id = None
__rclone_env_file = None
__rclone_verbose = None

DEFAULT_RCLONE_ENV_VALUES = (
    ("RCLONE_DRIVE_CHUNK_SIZE", "256M"),
    ("RCLONE_SIZE_ONLY", "1"),
    ("RCLONE_NO_UPDATE_MODTIME", "1"),
)

def default_config_path():
    return os.path.join(os.path.expanduser("~"), ".sprinkle", "sprinkle.conf")


def default_rclone_env_path():
    return os.path.join(os.path.expanduser("~"), ".sprinkle", "rclone.env")


def default_rclone_env_text():
    comments = {
        "RCLONE_DRIVE_CHUNK_SIZE": "Google Drive upload chunk size.",
        "RCLONE_SIZE_ONLY": "Compare by size only.",
        "RCLONE_NO_UPDATE_MODTIME": "Do not update remote modtime metadata after upload.",
    }
    lines = [
        "# Sprinkle rclone environment overrides.",
        "# Lines whose first non-space character is # are ignored.",
        "# Edit this file to tune rclone without changing Sprinkle commands.",
        "",
    ]
    for key, value in DEFAULT_RCLONE_ENV_VALUES:
        lines.append("# " + comments[key])
        lines.append("{}={}".format(key, value))
    lines.append("")
    return "\n".join(lines)


def ensure_rclone_env_file(path):
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.exists(path):
        return path
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fp:
        fp.write(default_rclone_env_text())
    return path


def apply_rclone_env_file(path):
    if path in (None, ""):
        return {}
    path = ensure_rclone_env_file(path)
    loaded = {}
    with open(path, "r") as fp:
        for line in fp:
            line = line.strip()
            if line == "" or line.startswith("#"):
                continue
            if "=" not in line:
                logging.debug("ignoring invalid rclone env line in " + path)
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key == "":
                logging.debug("ignoring empty rclone env key in " + path)
                continue
            value = value.strip()
            os.environ[key] = value
            loaded[key] = value
    logging.debug("loaded " + str(len(loaded)) + " rclone environment variables from " + path)
    return loaded


def warranty():
    """
WARRANTY:
    THIS SOFTWARE IS PROVIDED ``AS IS'' AND ANY EXPRESSED OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
    THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT
    SHALL THE APACHE SOFTWARE FOUNDATION OR ITS CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
    SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
    OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
    LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY
    WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
    """


def authors():
    """
AUTHOR:
    Michael Montuori, [michael.montuori@gmail.com]
    """


def copyrights():
    """
COPYRIGHT:
    (C)2018 Michael Montuori, [michael.montuori@gmail.com]. All rights reserved.
    """
    return


def credits():
    """
CREDITS:
    Warren Crigger for development and testing support
    """


def usage_options():
    """
OPTIONS:
    -c, --conf {config file}     configuration file
    -d, --debug                  debug output (default:true)
    -h, --help                   help
    -v, --verbose                set RCLONE_VERBOSE=1 for rclone
    --version                    print version
    --check-prereq               chech prerequisites
    --comp-method {size|md5}     compare method [size|md5] (default:size)
    --daemon-interval            interval for the daemon to execute in minutes (default:60)
    --daemon-mode                start sprinkle in daemon mode
    --daemon-pidfile             daemon pidfile (default:/var/run/sprinkle.pid or /tmp/sprinkle.pid)
    --daemon-type                type of daemon [interval|ondemand] (default:interval)
    --delete-files               do not delete files on remote end (default:false)
    --display-unit {G|M|K|B}     display unit (G)igabytes, (M)egabytes, (K)ilobytes, or (B)ites
    --dist-type {mas}            distribution type (default:mas)
    --dry-run                    perform a dry run without actually backing up
    --exclude-file {file}        file containing the backup exclude paths
    --exclude-regex {regex}      regular expression to match for file backup exclusion
    --log-file {file}            logs output to the specified file
    --no-cache                   turn off caching
    --rclone-conf {config file}  rclone configuration (default:None)
    --rclone-env-file {file}     file with environment variables for rclone
    --rclone-sa-dir {dir}        build rclone config from service accounts
    --rclone-sa-count {num}      limit number of service accounts used
    --drive-id {id}              Google Drive folder ID for rclone config
    --sa-db {file}               service account registry database
    --sa-store {dir}             managed service account store
    --sa-cache-ttl-hours {num}   hours before cached SA quota is stale (default:72)
    --sa-refresh {mode}          SA quota refresh [missing|stale|all|none] (default:stale)
    --sa-clean-invalid {mode}    invalid SA cleanup [none|quarantine|delete] (default:quarantine)
    --sa-group-size {num}        preferred SA grouping size for generated operator configs
    --rclone-exe {rclone_exe}    rclone executable (default:rclone)
    --rclone-move                use 'rclone move' instead of 'rclone copy' (default:false)
    --restore-duplicates         restore files if duplicates are found (default:false)
    --retries {num_retries}      number of retries (default:1)
    --progress                   show progress
    --single-instance            make sure only 1 concurrent instance of sprinkle is running (default:False)
    --ls-stop-first              stop listing after first remote with files (default:true)
    """
    return


def usage_commands():
    """
COMMANDS:
    backup                       backup files to clustered drives
    config                       create ~/.sprinkle/sprinkle.conf
    help                         displays the help fot the specific command
    ls                           list files
    lsmd5                        list md5 of files
    stats                        display volume statistics
    sa-import                    import Google Drive service account files
    sa-stats                     display imported service account statistics
    restore                      restore files from clustered drives
    removedups                   removes duplicate files
    """
    return

def usage():
    """
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
    """
    print(usage.__doc__)
    version()
    print(usage_commands.__doc__)
    print(usage_options.__doc__)
    print(authors.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)
    print(warranty.__doc__)


def usage_config():
    """
NAME:
    sprinkle config - create a Sprinkle configuration file

SYNOPSIS:
    sprinkle.py [options] config

DESCRIPTION:
    Interactively writes ~/.sprinkle/sprinkle.conf with the common Sprinkle defaults,
    including rclone_move, delete_files, and Google Drive service account cache defaults.
    Use -c/--conf with this command to write a different config path.

EXAMPLES:
    sprinkle.py config
    sprinkle.py -c /tmp/sprinkle.conf config
    """
    print(usage_config.__doc__)
    print(usage_options.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)


def usage_ls():
    """
NAME:
    sprinkle ls - List files on remote volumes

SYNOPSIS:
    sprinkle.py [options] ls {path}

DESCRIPTION:
    List files on the remote drive. The output generated by the command looks like:

    --- NAME                                                                  SIZE MOD TIME            REMOTE
    --- ---------------------------------------------------------------- --------- ------------------- ---------------
    -d- /backup/directory                                                       -1 2018-10-21:00:18:53 volume1:
    --- /backup/directory/file.txt                                            8580 2018-10-21:00:17:28 volume1:

    -d- indicates that the file is a directory and --- indicates a regular file

ARGUMENTS:
    path
        the remote path to list files

EXAMPLES:
    sprinkle.py ls /backup
    sprinkle.py ls /
    """
    print(usage_ls.__doc__)
    print(usage_options.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)


def usage_lsmd5():
    """
NAME:
    sprinkle lsmd5 - List file MD5 hash on remote volumes

SYNOPSIS:
    sprinkle.py [options] lsmd5 {path}

DESCRIPTION:
    List files on the remote drive with the respective MD5 hash. The output generated by the command looks like:

    NAME                                                             MD5
    ---------------------------------------------------------------- --------------------------------
    /backup/directory/file1.txt                                      92de4cde16da896dcc6289b92df42976
    /backup/directory/file2.txt                                      86efff36b7b0df257f1779d974c8101b

ARGUMENTS:
    path
        the remote path to list files

EXAMPLES:
    sprinkle.py lsmd5 /backup
    sprinkle.py lsmd5 /
    """
    print(usage_lsmd5.__doc__)
    print(usage_options.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)


def usage_backup():
    """
NAME:
    sprinkle backup - backs up the local directory to remote volumes

SYNOPSIS:
    sprinkle.py [options] backup {local dir}

DESCRIPTION:
    Backs up the local directory to the remote drives configured in rclone.
    Hint: backup requires --drive-id <folder-id>. You can also pass
    --rclone-sa-dir <path>; if omitted, Sprinkle uses the default managed
    service-account store.

ARGUMENTS:
    local dir
        the local directory to backup

EXAMPLES:
    sprinkle.py --drive-id XXXXX backup /backup
    sprinkle.py --drive-id XXXXX --rclone-sa-dir /etc/rclone/sa backup /backup
    """
    print(usage_backup.__doc__)
    print(usage_options.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)


def usage_restore():
    """
NAME:
    sprinkle restores - restore files from a previously backed up directory

SYNOPSIS:
    sprinkle.py [options] restore {remote dir} {local dir}

DESCRIPTION:
    Restores the remote directories from the rclone drives to the local directory specified.

ARGUMENTS:
    remote dir
        the remote directory to restore

    local dir
        the local directory to use

EXAMPLES:
    sprinkle.py restore /backup c:/backup
    """
    print(usage_restore.__doc__)
    print(usage_options.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)


def usage_help():
    """
NAME:
    sprinkle help - display help for specific commands

SYNOPSIS:
    sprinkle.py [options] help {command}

DESCRIPTION:
    displays the general help about sprinkle

ARGUMENTS:
    command
        the command to display help for

EXAMPLES:
    sprinkle.py help
    """
    print(usage_help.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)
    print(warranty.__doc__)


def usage_stats():
    """
NAME:
    sprinkle stats - display volume statistics

SYNOPSIS:
    sprinkle.py [options] stats

DESCRIPTION:
    display the statistics about all the remote volumes. The output should look like:

    REMOTE                          SIZE                 FREE      %FREE
    =============== ==================== ==================== ==========
    volume1:                         15G                   0G          1
    volume2:                         15G                   1G          7
    volume3:                         15G                   0G          3
    --------------- -------------------- -------------------- ----------
    total:                           45G                   1G          3

EXAMPLES:
    sprinkle.py stats
    sprinkle.py --display-unit=K stats
    """
    print(usage_stats.__doc__)
    print(usage_options.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)


def usage_sa_import():
    """
NAME:
    sprinkle sa-import - import Google Drive service account files

SYNOPSIS:
    sprinkle.py [options] sa-import {path...}

DESCRIPTION:
    Recursively imports valid service account JSON files into Sprinkle's managed store.
    Duplicates are recorded but not copied again. New accounts are validated with
    rclone about --json during import. Invalid accounts and unknown quota results are
    quarantined by default.

EXAMPLES:
    sprinkle.py --drive-id XXXXX sa-import /Users/user/workspace/svcacc
    """
    print(usage_sa_import.__doc__)
    print(usage_options.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)


def usage_sa_stats():
    """
NAME:
    sprinkle sa-stats - display imported service account statistics

SYNOPSIS:
    sprinkle.py [options] sa-stats

DESCRIPTION:
    Shows imported service accounts and cached quota data. By default this command refreshes
    stale cache entries with rclone about --json.

EXAMPLES:
    sprinkle.py --drive-id XXXXX sa-stats
    sprinkle.py --sa-refresh=none sa-stats
    sprinkle.py --sa-cache-ttl-hours=72 --sa-refresh=stale sa-stats
    """
    print(usage_sa_stats.__doc__)
    print(usage_options.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)


def usage_removedups():
    """
NAME:
    sprinkle removedups - remove duplicate files from remote volumes

SYNOPSIS:
    sprinkle.py [options] removedups {path}

DESCRIPTION:
    Removes duplicate files from remote volumes. Remote file can accumulate over time due to a variety of
    conditions. Run this utility often to minimize chances of having corrupt data.

ARGUMENTS:
    path
        the remote path to list files

EXAMPLES:
    sprinkle.py removedups /backup
    """
    print(usage_removedups.__doc__)
    print(usage_options.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)


def usage_find():
    """
    NAME:
        sprinkle find - search for files on the remote volumes

    SYNOPSIS:
        sprinkle.py [options] find {regexp}

    DESCRIPTION:
        Finds files on all configured remote volumes specified with the regular expression.

    ARGUMENTS:
        regexp
            the regular expression used to filter files

    EXAMPLES:
        sprinkle.py find "/backup/....sh"
        """
    print(usage_find.__doc__)
    print(usage_options.__doc__)
    print(copyrights.__doc__)
    print(credits.__doc__)


def version():
    print("VERSION:\n    " + __version__ + '.' + __revision__ +
          ", module version: " + clsync.__version__ + '.' + clsync.__revision__ +
          ", rclone module version: " + rclone.__version__ + '.' + rclone.__revision__)


def read_args(argv):
    global __configfile
    global __dirtosync
    global __args
    global __cmd_debug
    global __dist_type
    global __comp_method
    global __rclone_exe
    global __rclone_conf
    global __rclone_env_file
    global __rclone_verbose
    global __drive_id
    global __display_unit
    global __rclone_retries
    global __show_progress
    global __delete_files
    global __rclone_move
    global __restore_duplicates
    global __dry_run
    global __smtp_enable
    global __smtp_from
    global __smtp_to
    global __smtp_server
    global __smtp_port
    global __smtp_user
    global __smtp_password
    global __no_cache
    global __cl_sync
    global __exclude_file
    global __exclude_regex
    global __log_file
    global __single_instance
    global __check_prereq
    global __daemon_type
    global __daemon_mode
    global __daemon_interval
    global __daemon_pidfile
    global __ls_stop_first
    global __sa_db
    global __sa_store
    global __sa_cache_ttl_hours
    global __sa_refresh
    global __sa_clean_invalid
    global __sa_group_size
    global __rclone_sa_dir
    global __rclone_sa_count

    __configfile = None
    __cmd_debug = None
    __dist_type = None
    __comp_method = None
    __rclone_exe = None
    __rclone_conf = None
    __rclone_env_file = None
    __rclone_verbose = None
    __drive_id = None
    __display_unit = None
    __rclone_retries = None
    __show_progress = None
    __delete_files = None
    __rclone_move = None
    __restore_duplicates = False
    __dry_run = None
    __smtp_enable = None
    __smtp_from = None
    __smtp_to = None
    __smtp_server = None
    __smtp_port = None
    __smtp_user = None
    __smtp_password = None
    __no_cache = None
    __cl_sync = None
    __exclude_file = None
    __exclude_regex = None
    __log_file = None
    __single_instance = None
    __check_prereq = None
    __daemon_type = None
    __daemon_mode = False
    __daemon_interval = None
    __daemon_pidfile = None
    __ls_stop_first = None
    __sa_db = None
    __sa_store = None
    __sa_cache_ttl_hours = None
    __sa_refresh = None
    __sa_clean_invalid = None
    __sa_group_size = None
    __rclone_sa_dir = None
    __rclone_sa_count = None

    try:
        opts, args = getopt.getopt(argv, "dvhc:s:",
                                   ["help",
                                    "conf=",
                                    "debug",
                                    "verbose",
                                    "version",
                                    "dist-type=",
                                    "comp-method=",
                                    "rclone-exe=",
                                    "rclone-conf=",
                                    "rclone-env-file=",
                                    "rclone-sa-dir=",
                                    "rclone-sa-count=",
                                    "drive-id=",
                                    "sa-db=",
                                    "sa-store=",
                                    "sa-cache-ttl-hours=",
                                    "sa-refresh=",
                                    "sa-clean-invalid=",
                                    "sa-group-size=",
                                    "stats=",
                                    "display-unit=",
                                    "rclone-retries=",
                                    "rclone-move",
                                    "show-progress",
                                    "progress",
                                    "dry-run",
                                    "delete-files",
                                    "restore-duplicates",
                                    "smtp-enable",
                                    "smtp-from=",
                                    "smtp-to=",
                                    "smtp-server=",
                                    "smtp-port=",
                                    "smtp-user=",
                                    "smtp-password=",
                                    "no-cache",
                                    "exclude-file=",
                                    "exclude-regex=",
                                    "log-file=",
                                    "single-instance",
                                    "ls-stop-first",
                                    "check-prereq",
                                    "daemon-type=",
                                    "daemon-mode",
                                    "daemon-interval=",
                                    "daemon-pidfile="
                                    ])
    except getopt.GetoptError:
        usage()
        sys.exit(2)

    for opt, arg in opts:
        if opt in ('-h', '--help'):
            usage()
            sys.exit(0)
        elif opt == '--version':
            version()
            sys.exit(0)
        elif opt in ("-v", "--verbose"):
            __rclone_verbose = True
        elif opt in ("-c", "--conf"):
            __configfile = arg
        elif opt in ("-d", "--debug"):
            __cmd_debug = True
        elif opt in ("--dist-type"):
            __dist_type = arg
        elif opt in ("--comp-method"):
            __comp_method = arg
        elif opt in ("--rclone-exe"):
            __rclone_exe = arg
        elif opt in ("--rclone-conf"):
            __rclone_conf = arg
        elif opt in ("--rclone-env-file"):
            __rclone_env_file = arg
        elif opt in ("--rclone-sa-dir"):
            __rclone_sa_dir = arg
        elif opt in ("--rclone-sa-count"):
            __rclone_sa_count = int(arg)
        elif opt in ("--drive-id"):
            __drive_id = arg
            __ls_stop_first = True
        elif opt in ("--sa-db"):
            __sa_db = arg
        elif opt in ("--sa-store"):
            __sa_store = arg
        elif opt in ("--sa-cache-ttl-hours"):
            __sa_cache_ttl_hours = int(arg)
        elif opt in ("--sa-refresh"):
            if arg not in ("missing", "stale", "all", "none"):
                raise Exception("--sa-refresh must be one of missing, stale, all, none")
            __sa_refresh = arg
        elif opt in ("--sa-clean-invalid"):
            if arg not in ("none", "quarantine", "delete"):
                raise Exception("--sa-clean-invalid must be one of none, quarantine, delete")
            __sa_clean_invalid = arg
        elif opt in ("--sa-group-size"):
            __sa_group_size = int(arg)
        elif opt in ("--display-unit"):
            if arg != 'G' and arg != 'M' and arg != 'K' and arg != 'B':
                logging.error('invalid UNIT ' + arg + ', only [G|M|K|B] accepted')
            __display_unit = arg
        elif opt in ("--rclone-retries"):
            __rclone_retries = int(arg)
        elif opt in ("--show-progress", "--progress"):
            __show_progress = True
        elif opt in ("--delete-files"):
            __delete_files = True
        elif opt in ("--rclone-move"):
            __rclone_move = True
        elif opt in ("--restore-duplicates"):
            __restore_duplicates = True
        elif opt in ("--dry-run"):
            __dry_run = True
        elif opt in ("--smtp-enable"):
            __smtp_enable = True
        elif opt in ("--smtp-from"):
            __smtp_from = arg
        elif opt in ("--smtp-to"):
            __smtp_to = arg
        elif opt in ("--smtp-server"):
            __smtp_server = arg
        elif opt in ("--smtp-port"):
            __smtp_port = arg
        elif opt in ("--smtp-user"):
            __smtp_user = arg
        elif opt in ("--smtp-password"):
            __smtp_password = arg
        elif opt in ("--no-cache"):
            __no_cache = True
        elif opt in ("--exclude-file"):
            __exclude_file = arg
        elif opt in ("--exclude-regex"):
            __exclude_regex = arg
        elif opt in ("--log-file"):
            __log_file = arg
        elif opt in ("--single-instance"):
            __single_instance = True
        elif opt in ("--ls-stop-first"):
            __ls_stop_first = True
        elif opt in ("--check-prereq"):
            __check_prereq = True
        elif opt in ("--daemon-type"):
            print("here1")
            __daemon_type = arg
        elif opt in ("--daemon-mode"):
            __daemon_mode = True
        elif opt in ("--daemon-interval"):
            __daemon_interval = int(arg)
        elif opt in ("--daemon-pidfile"):
            __daemon_pidfile = arg

    if __configfile is None:
        candidate_config = default_config_path()
        if os.path.isfile(candidate_config):
            __configfile = candidate_config

    if len(args) < 1 and __check_prereq is None:
        usage()
        sys.exit()

    __args = args


def configure(config_file):
    global __config
    global __drive_id
    global __rclone_move

    _default_values = {
        "debug": True,
        "dry_run": False,
        "show_progress": False,
        "delete_files": False,
        "rclone_move": True,
        "restore_duplicates": False,
        "smtp_enable": False,
        "no_cache": False,
        "distribution_type": "mas",
        "compare_method": "size",
        "display_unit": "G",
        "rclone_retries": '1',
        "drive_id": None,
        "rclone_env_file": default_rclone_env_path(),
        "rclone_sa_dir": None,
        "rclone_sa_count": None,
        "log_file": None,
        "single_instance": False,
        "ls_stop_first": True,
        "check_prereq": False,
        "daemon_type": 'interval',
        "daemon_mode": False,
        "daemon_interval": 60,
        "daemon_pidfile": '/var/run/sprinkle.pid',
        "sa_db": service_accounts.DEFAULT_DB_PATH,
        "sa_store": service_accounts.DEFAULT_STORE_DIR,
        "sa_cache_ttl_hours": service_accounts.DEFAULT_CACHE_TTL_HOURS,
        "sa_refresh": service_accounts.DEFAULT_REFRESH_MODE,
        "sa_clean_invalid": service_accounts.DEFAULT_CLEAN_INVALID,
        "sa_group_size": 50,
        "large_file_threshold_bytes": clsync.DEFAULT_LARGE_FILE_THRESHOLD_BYTES,
        "large_file_min_free_bytes": clsync.DEFAULT_LARGE_FILE_MIN_FREE_BYTES,
        "large_file_min_free_percent": clsync.DEFAULT_LARGE_FILE_MIN_FREE_PERCENT
    }

    if config_file is not None:
        conf = config.Config(config_file)
        __config = conf.get_config()
    else:
        __config = {}

    for field in _default_values:
        if field not in __config:
            __config[field] = _default_values[field]

    normalize_config_types(__config)

    if __cmd_debug is True:
        __config['debug'] = True
        init_logging(True, __daemon_mode)
    elif 'debug' in __config:
        init_logging(__config['debug'], __daemon_mode)

    if __dist_type is not None:
        __config['distribution_type'] = __dist_type

    if __comp_method is not None:
        __config['compare_method'] = __comp_method

    if __rclone_exe is not None:
        __config['rclone_exe'] = __rclone_exe

    if __rclone_conf is not None:
        __config['rclone_config'] = __rclone_conf

    if __rclone_env_file is not None:
        __config['rclone_env_file'] = __rclone_env_file

    if __drive_id is not None:
        __config['drive_id'] = __drive_id

    if __rclone_sa_dir is not None:
        __config['rclone_sa_dir'] = __rclone_sa_dir

    if __rclone_sa_count is not None:
        __config['rclone_sa_count'] = __rclone_sa_count

    if __rclone_retries is not None:
        __config['rclone_retries'] = str(__rclone_retries)

    if __show_progress is not None:
        __config['show_progress'] = __show_progress

    if __delete_files is not None:
        __config['delete_files'] = __delete_files

    if __rclone_move is not None:
        __config['rclone_move'] = __rclone_move

    if __dry_run is not None:
        __config['dry_run'] = __dry_run

    if __smtp_enable is not None:
        __config['smtp_enable'] = __smtp_enable

    if __display_unit is not None:
        __config['display_unit'] = __display_unit

    if __smtp_from is not None:
        __config['smtp_from'] = __smtp_from

    if __smtp_to is not None:
        __config['smtp_to'] = __smtp_to

    if __smtp_server is not None:
        __config['smtp_server'] = __smtp_server

    if __smtp_port is not None:
        __config['smtp_port'] = __smtp_port

    if __smtp_user is not None:
        __config['smtp_user'] = __smtp_user

    if __smtp_password is not None:
        __config['smtp_password'] = __smtp_password

    if __no_cache is not None:
        __config['no_cache'] = __no_cache

    if __exclude_file is not None:
        __config['exclude_file'] = __exclude_file

    if __exclude_regex is not None:
        __config['exclude_regex'] = __exclude_regex

    if __log_file is not None:
        __config['log_file'] = __log_file

    if __single_instance is not None:
        __config['single_instance'] = __single_instance

    if __ls_stop_first is not None:
        __config['ls_stop_first'] = __ls_stop_first

    if __check_prereq is not None:
        __config['check_prereq'] = __check_prereq

    if __daemon_type is not None:
        __config['daemon_type'] = __daemon_type

    if __daemon_mode is not None:
        __config['daemon_mode'] = __daemon_mode
        __config['no_cache'] = True

    if __daemon_interval is not None:
        __config['daemon_interval'] = __daemon_interval

    if __daemon_pidfile is not None:
        __config['daemon_pidfile'] = __daemon_pidfile

    if __sa_db is not None:
        __config['sa_db'] = __sa_db

    if __sa_store is not None:
        __config['sa_store'] = __sa_store

    if __sa_cache_ttl_hours is not None:
        __config['sa_cache_ttl_hours'] = __sa_cache_ttl_hours

    if __sa_refresh is not None:
        __config['sa_refresh'] = __sa_refresh

    if __sa_clean_invalid is not None:
        __config['sa_clean_invalid'] = __sa_clean_invalid

    if __sa_group_size is not None:
        __config['sa_group_size'] = __sa_group_size

    apply_rclone_env_file(__config.get('rclone_env_file'))
    if __rclone_verbose is True:
        os.environ["RCLONE_VERBOSE"] = "1"


def normalize_config_types(config_values):
    bool_fields = (
        'debug',
        'dry_run',
        'show_progress',
        'delete_files',
        'rclone_move',
        'restore_duplicates',
        'smtp_enable',
        'no_cache',
        'single_instance',
        'ls_stop_first',
        'check_prereq',
        'daemon_mode',
    )
    int_fields = (
        'daemon_interval',
        'sa_cache_ttl_hours',
        'sa_group_size',
        'rclone_sa_count',
        'large_file_threshold_bytes',
        'large_file_min_free_bytes',
        'large_file_min_free_percent',
    )
    for field in bool_fields:
        if field in config_values:
            config_values[field] = _parse_bool(config_values[field])
    for field in int_fields:
        if field in config_values and config_values[field] not in (None, ''):
            config_values[field] = int(config_values[field])


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 'yes', 'y', 'ja', 'j')


def verify_configuration():
    logging.debug('verifying configuration ' + str(__config))
    if __config['smtp_enable'] is True:
        if 'smtp_from' not in __config:
            raise Exception('smtp_from value is None')
        if 'smtp_to' not in __config:
            raise Exception('smtp_to value is None')
        if 'smtp_server' not in __config:
            raise Exception('smtp_server value is None')
        if 'smtp_port' not in __config:
            raise Exception('smtp_port value is None')

    if 'exclude_file' in __config and __config['exclude_file'] is not None:
        if not os.path.isfile(__config['exclude_file']):
            raise Exception('exclude_file ' + __config['exclude_file'] + ' not found!')
        global __exclusion_list
        __exclusion_list = load_exclusion_file(__config['exclude_file'])
        logging.debug('exclusion list: ' + str(__exclusion_list))
        __config['__exclusion_list'] = __exclusion_list

    if __daemon_mode is True and os.access(os.path.dirname(__config['daemon_pidfile']), os.W_OK) is not True:
        logging.warning('cannot write to pidfile "' + __config['daemon_pidfile'] + '" switching to /tmp/sprinkle.pid')
        __config['daemon_pidfile'] = '/tmp/sprinkle.pid'


def prepare_rclone_sa_config():
    global __rclone_conf
    rclone_sa_dir = __config.get('rclone_sa_dir')
    if rclone_sa_dir in (None, ''):
        if len(__args) > 0 and __args[0] == 'backup':
            rclone_sa_dir = service_accounts.DEFAULT_STORE_DIR
            __config['rclone_sa_dir'] = rclone_sa_dir
            common.print_line(
                "backup hint: set --drive-id <folder-id>; --rclone-sa-dir is optional and defaults to "
                + rclone_sa_dir
            )
        else:
            return
    if __rclone_conf is not None and globals().get('__rclone_sa_dir') is None:
        return
    drive_id = __config.get('drive_id')
    if drive_id in (None, ''):
        if len(__args) > 0 and __args[0] == 'backup':
            raise Exception(
                "backup requires --drive-id <folder-id>; optionally pass --rclone-sa-dir <path> "
                "to override the default service-account store"
            )
        raise Exception("--drive-id option or drive_id config value is required when using rclone_sa_dir")
    fd, tmp_conf = tempfile.mkstemp(prefix="rclone-", suffix=".conf")
    os.close(fd)
    registry = service_accounts.ServiceAccountRegistry(
        __config.get('sa_db'),
        __config.get('sa_store'),
        __config.get('sa_cache_ttl_hours', service_accounts.DEFAULT_CACHE_TTL_HOURS),
    )
    source_dir = os.path.abspath(os.path.expanduser(rclone_sa_dir))
    managed_store_dir = os.path.abspath(os.path.expanduser(__config.get('sa_store')))
    if source_dir == managed_store_dir:
        selected_files = [
            account['managed_path']
            for account in registry.active_accounts()
            if account['managed_path']
        ]
    else:
        import_result = registry.import_paths(
            [rclone_sa_dir],
            __config.get('sa_clean_invalid', service_accounts.DEFAULT_CLEAN_INVALID),
        )
        selected_files = import_result.selected_files
    if len(selected_files) == 0:
        raise Exception("no valid service accounts found in " + rclone_sa_dir)
    config_text, entries = rclone.generate_rclone_config_from_files(
        selected_files,
        tmp_conf,
        drive_id,
        max_accounts=_optional_int(__config.get('rclone_sa_count')),
        return_entries=True,
        shuffle=False,
    )
    registry.assign_remote_names(entries)
    __rclone_conf = tmp_conf
    __config['rclone_config'] = tmp_conf


def command_needs_rclone_config():
    if __check_prereq is not None and __check_prereq is True:
        return True
    if len(__args) < 1:
        return False
    return __args[0] in ('ls', 'lsmd5', 'backup', 'restore', 'stats', 'removedups', 'find')


def _optional_int(value):
    if value in (None, ''):
        return None
    return int(value)


def load_exclusion_file(exclude_file):
    logging.debug('loading exclusion file ' + exclude_file)
    lines = []
    with open(exclude_file, 'r') as f:
        for line in f:
            line = line.strip()
            line = line.replace('\\', '/')
            lines.append(line)
        f.close()
    return lines


def init_logging(debug, daemon_mode=False):
    if debug is True:
        logging.basicConfig(format='%(asctime)s %(message)s',
                            datefmt='%m/%d/%Y %I:%M:%S %p',
                            level=logging.DEBUG,
                            filename=__log_file)
        logging.getLogger('sprinkle').setLevel(logging.DEBUG)
    else:
        if daemon_mode is False:
            logging.basicConfig(format='%(message)s',
                                level=logging.INFO,
                                filename=__log_file)
        else:
            logging.basicConfig(format='%(asctime)s %(message)s',
                                datefmt='%m/%d/%Y %I:%M:%S %p',
                                level=logging.INFO,
                                filename=__log_file)
        logging.getLogger('sprinkle').setLevel(logging.INFO)


def config_command(prompt_func=input, output_path=None):
    target = output_path or default_config_path()
    common.print_line('creating Sprinkle configuration at ' + target)
    if os.path.exists(target):
        overwrite = _prompt_bool(prompt_func, 'Overwrite existing config', False)
        if not overwrite:
            common.print_line('configuration not changed')
            return target

    rclone_move = _prompt_bool(prompt_func, 'rclone_move: move files instead of copying them', True)
    delete_files = _prompt_bool(prompt_func, 'delete_files: delete files after 1-way sync', False)
    debug = _prompt_bool(prompt_func, 'debug output (-d)', True)
    rclone_sa_count = _prompt_text(prompt_func, 'rclone_sa_count', '5')
    drive_id = _prompt_text(prompt_func, 'drive_id', 'XXXXX')
    rclone_sa_dir = _prompt_text(prompt_func, 'rclone_sa_dir', '/etc/rclone/sa')
    sa_cache_ttl_hours = _prompt_int(
        prompt_func,
        'sa_cache_ttl_hours',
        service_accounts.DEFAULT_CACHE_TTL_HOURS,
    )
    sa_refresh = _prompt_choice(
        prompt_func,
        'sa_refresh',
        service_accounts.DEFAULT_REFRESH_MODE,
        ('missing', 'stale', 'all', 'none'),
    )
    sa_clean_invalid = _prompt_choice(
        prompt_func,
        'sa_clean_invalid',
        service_accounts.DEFAULT_CLEAN_INVALID,
        ('none', 'quarantine', 'delete'),
    )

    content = _build_config_text(
        rclone_move,
        delete_files,
        debug,
        rclone_sa_count,
        drive_id,
        rclone_sa_dir,
        sa_cache_ttl_hours,
        sa_refresh,
        sa_clean_invalid,
    )
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, 'w') as fp:
        fp.write(content)
    if os.path.abspath(os.path.expanduser(target)) == os.path.abspath(default_config_path()):
        ensure_rclone_env_file(default_rclone_env_path())
    common.print_line('wrote ' + target)
    return target


def _prompt_bool(prompt_func, label, default):
    default_text = 'Y/n' if default else 'y/N'
    answer = prompt_func(label + ' [' + default_text + ']: ').strip().lower()
    if answer == '':
        return default
    return answer in ('y', 'yes', 'true', '1', 'j', 'ja')


def _prompt_text(prompt_func, label, default):
    answer = prompt_func(label + ' [' + str(default) + ']: ').strip()
    if answer == '':
        return str(default)
    return answer


def _prompt_int(prompt_func, label, default):
    while True:
        answer = _prompt_text(prompt_func, label, default)
        try:
            return str(int(answer))
        except ValueError:
            common.print_line(label + ' must be an integer')


def _prompt_choice(prompt_func, label, default, choices):
    while True:
        answer = _prompt_text(prompt_func, label + ' {' + '|'.join(choices) + '}', default)
        if answer in choices:
            return answer
        common.print_line(label + ' must be one of ' + ', '.join(choices))


def _bool_text(value):
    return 'true' if value else 'false'


def _build_config_text(
        rclone_move,
        delete_files,
        debug,
        rclone_sa_count,
        drive_id,
        rclone_sa_dir,
        sa_cache_ttl_hours,
        sa_refresh,
        sa_clean_invalid):
    return """## SPRINKLE CONFIGURATION
## Generated by: sprinkle.py config

# run sprinkle in debug mode. Equivalent to command line -d
debug={debug}

# rclone_move: move files instead of copying them
# rclone_move=false (copy files and keep sources) (default)
rclone_move={rclone_move}

# rclone_env_file: environment variables exported before invoking rclone.
# Lines starting with # are ignored. The default file is created on first use.
rclone_env_file=~/.sprinkle/rclone.env

# delete_files: delete files after 1-way sync
# delete_files=false (leave files not locally present on remote drives)
# delete_files=true (delete files not locally present from remote drives) (default)
delete_files={delete_files}

# Google Drive service account defaults.
# Equivalent command line:
# --rclone-sa-count {rclone_sa_count} --drive-id {drive_id} -d --rclone-sa-dir {rclone_sa_dir}
rclone_sa_count={rclone_sa_count}
drive_id={drive_id}
rclone_sa_dir={rclone_sa_dir}

# service account registry database and managed JSON store
sa_db=~/.sprinkle/sa-cache.sqlite3
sa_store=~/.sprinkle/service-accounts
sa_cache_ttl_hours={sa_cache_ttl_hours}
sa_refresh={sa_refresh}
sa_clean_invalid={sa_clean_invalid}
sa_group_size=50
ls_stop_first=true

# Large-file upload selection keeps enough headroom on small Google Drive accounts.
# Defaults require an extra 512 MiB or 5%, whichever is larger, for files >= 1 GiB.
large_file_threshold_bytes={large_file_threshold_bytes}
large_file_min_free_bytes={large_file_min_free_bytes}
large_file_min_free_percent={large_file_min_free_percent}

display_unit=G
distribution_type=mas
compare_method=size
rclone_retries=1
""".format(
        debug=_bool_text(debug),
        rclone_move=_bool_text(rclone_move),
        delete_files=_bool_text(delete_files),
        rclone_sa_count=rclone_sa_count,
        drive_id=drive_id,
        rclone_sa_dir=rclone_sa_dir,
        sa_cache_ttl_hours=sa_cache_ttl_hours,
        sa_refresh=sa_refresh,
        sa_clean_invalid=sa_clean_invalid,
        large_file_threshold_bytes=clsync.DEFAULT_LARGE_FILE_THRESHOLD_BYTES,
        large_file_min_free_bytes=clsync.DEFAULT_LARGE_FILE_MIN_FREE_BYTES,
        large_file_min_free_percent=clsync.DEFAULT_LARGE_FILE_MIN_FREE_PERCENT,
    )


def ls():
    global __cl_sync
    if __cl_sync is None:
        __cl_sync = clsync.ClSync(__config)
    if len(__args) == 1:
        logging.error('invalid ls command')
        usage_ls()
        sys.exit(-1)
    files = __cl_sync.ls(
        common.remove_ending_slash(__args[1]),
        stop_after_first=__config.get('ls_stop_first', False),
    )
    largest_length = 25
    keys = common.sort_dict_keys(files)
    for tmp_file in keys:
        filename_length = len(files[tmp_file].path)
        if not files[tmp_file].is_dir and filename_length > largest_length:
            largest_length = filename_length
    common.print_line('---' + " " +
                      'NAME'.ljust(largest_length) + " " +
                      'SIZE'.rjust(9) + " " +
                      'MOD TIME'.ljust(19) + " " +
                      'REMOTE'
                      )
    common.print_line('---' + " " +
                      ''.join('-' for i in range(largest_length)) + " " +
                      ''.join('-' for i in range(9)) + " " +
                      ''.join('-' for i in range(19)) + " " +
                      ''.join('-' for i in range(15))
                      )
    for tmp_file in keys:
        if files[tmp_file].is_dir is True:
            first_chars = '-d-'
        else:
            first_chars = '---'
        file_name = files[tmp_file].path
        if file_name.startswith('//'):
            file_name = file_name[1:len(file_name)]
        common.print_line(first_chars + " " +
                          file_name.ljust(largest_length) + " " +
                          str(files[tmp_file].size).rjust(9) + " " +
                          common.get_printable_datetime(files[tmp_file].mod_time).ljust(19) + " " +
                          files[tmp_file].remote
                          )

def lsmd5():
    global __cl_sync
    if __cl_sync is None:
        __cl_sync = clsync.ClSync(__config)
    if len(__args) == 1:
        logging.error('invalid lsmd5 command')
        usage_lsmd5()
        sys.exit(-1)
    files = __cl_sync.lsmd5(
        common.remove_ending_slash(__args[1]),
        stop_after_first=__config.get('ls_stop_first', False),
    )
    largest_length = 25
    keys = common.sort_dict_keys(files)
    for tmp_file in keys:
        filename_length = len(tmp_file)
        if filename_length > largest_length:
            largest_length = filename_length
    common.print_line('NAME'.ljust(largest_length) + " " +
                      'MD5'.ljust(32)
                      )
    common.print_line(''.join('-' for i in range(largest_length)) + " " +
                      ''.join('-' for i in range(32))
                      )

    for tmp_file in keys:
        file_name = tmp_file
        common.print_line(file_name.ljust(largest_length) + " " +
                          files[tmp_file]
                          )

def backup():
    global __cl_sync
    if __cl_sync is None:
        __cl_sync = clsync.ClSync(__config)
    if len(__args) == 1:
        logging.error('invalid backup command')
        usage_backup()
        sys.exit(-1)
    local_dir = common.remove_ending_slash(__args[1])
    common.print_line('backing up ' + local_dir + '...')
    __cl_sync.backup(local_dir, __config['delete_files'], __config['dry_run'])


def restore():
    global __cl_sync
    if __cl_sync is None:
        __cl_sync = clsync.ClSync(__config)
    if len(__args) < 3:
        logging.error('invalid remote command')
        usage_restore()
        sys.exit(-1)
    remote_path = __args[2]
    local_dir = common.remove_ending_slash(__args[1])
    if __config['restore_duplicates'] is False:
        common.print_line('checking if duplicates are present before restoring...')
        duplicates = __cl_sync.remove_duplicates(local_dir, True)
        if len(duplicates) > 0:
            common.print_line('DUPLICATE FILES FOUND:')
            for duplicate in duplicates:
                common.print_line("\t" + duplicate)
            common.print_line('restore cannot proceed! Use remove duplicates function before continuing')
            return
    common.print_line('restoring ' + remote_path + ' from ' + local_dir)
    __cl_sync.restore(local_dir, remote_path, __config['dry_run'])


def stats():
    global __cl_sync
    logging.debug('display stats about the volumes')
    common.print_line('calculating total and free space...')
    if __cl_sync is None:
        __cl_sync = clsync.ClSync(__config)
    common.print_line('REMOTE'.ljust(15) + " " +
                      'SIZE'.rjust(20) + " " +
                      'FREE'.rjust(20) + " " +
                      '%FREE'.rjust(10)
                      )
    common.print_line(''.join('=' for i in range(15)) + " " +
                      ''.join('=' for i in range(20)) + " " +
                      ''.join('=' for i in range(20)) + " " +
                      ''.join('=' for i in range(10))
                      )
    sizes = __cl_sync.get_sizes()
    frees = __cl_sync.get_frees()
    display_unit = __config['display_unit']
    for remote in sizes:
        percent_use = _format_percent(frees[remote], sizes[remote])
        common.print_line(remote.ljust(15) + " " +
                          _format_amount(sizes[remote], display_unit).rjust(20) + " " +
                          _format_amount(frees[remote], display_unit).rjust(20) + " " +
                          percent_use.rjust(10)
                          )

    size = __cl_sync.get_size()
    free = __cl_sync.get_free()
    logging.debug('size: ' + "{:,}".format(size))
    logging.debug('free: ' + "{:,}".format(free))
    percent_use = _format_percent(free, size)
    common.print_line(''.join('-' for i in range(15)) + " " +
                      ''.join('-' for i in range(20)) + " " +
                      ''.join('-' for i in range(20)) + " " +
                      ''.join('-' for i in range(10))
                      )
    common.print_line("total:".ljust(15) + " " +
                      _format_amount(size, display_unit).rjust(20) + " " +
                      _format_amount(free, display_unit).rjust(20) + " " +
                      percent_use.rjust(10)
                      )


def _service_account_registry():
    return service_accounts.ServiceAccountRegistry(
        __config['sa_db'],
        __config['sa_store'],
        __config['sa_cache_ttl_hours'],
    )


def _format_amount(amount, display_unit):
    if amount is None:
        return 'UNKNOWN'
    return "{:,}{}".format(common.convert_unit(amount, display_unit), display_unit)


def _format_percent(free, total):
    if free is None or total is None or total == 0:
        return 'UNKNOWN'
    return "{:,}".format(int(free * 100 / total))


def _sa_import_progress(event):
    event_type = event.get("event")
    if event_type == "start":
        common.print_line("service account import: found " + str(event.get("total", 0)) + " json files")
        return
    if event_type != "status":
        return
    status = event.get("status")
    path = event.get("path") or ""
    basename = os.path.basename(path)
    prefix = "service account import [{}/{}] ".format(event.get("index"), event.get("total"))
    message = prefix + status + ": " + basename
    reason = event.get("reason")
    if reason:
        message += " - " + reason
    common.print_line(message)


def _service_account_live_validator(path, payload):
    fd, tmp_conf = tempfile.mkstemp(prefix="rclone-sa-import-", suffix=".conf")
    os.close(fd)
    try:
        rclone.generate_rclone_config_from_files(
            [path],
            tmp_conf,
            None,
            prefix="sa_import",
            start_index=1,
            shuffle=False,
        )
        rclone_exe = __config.get('rclone_exe', 'rclone')
        rclone_retries = __config.get('rclone_retries', '1')
        rc = rclone.RClone(tmp_conf, rclone_exe, rclone_retries)
        quota, error = rc.get_about_json_with_error("sa_import1:")
        if error is not None:
            return None, _friendly_rclone_error(error, payload)
        unknown_reason = _quota_unknown_reason(quota)
        if unknown_reason is not None:
            return None, unknown_reason
        return quota, None
    finally:
        try:
            os.unlink(tmp_conf)
        except Exception:
            pass


def _quota_unknown_reason(quota):
    if not isinstance(quota, dict):
        return "rclone about returned unknown quota"
    missing = []
    for field in ("total", "free"):
        if field not in quota or quota.get(field) is None:
            missing.append(field)
    if missing:
        return "rclone about returned unknown quota: missing " + ",".join(missing)
    return None


def _friendly_rclone_error(error, identity=None):
    text = " ".join(str(error).split())
    if len(text) > 320:
        text = text[:317] + "..."
    lower = text.lower()
    account = ""
    if identity is not None:
        email = _identity_value(identity, "client_email")
        project = _identity_value(identity, "project_id")
        if email or project:
            account = " for"
            if email:
                account += " " + email
            if project:
                account += " project " + project
    if "executable not found" in lower or "no such file" in lower:
        return "rclone executable not found" + account
    if "invalid_grant" in lower or "jwt" in lower or "invalid_client" in lower:
        return "service account credentials rejected" + account + ": " + text
    if "project" in lower and ("not found" in lower or "deleted" in lower or "disabled" in lower):
        return "service account project not available" + account + ": " + text
    if "service_disabled" in lower or "accessnotconfigured" in lower or "api has not been used" in lower:
        return "Google Drive API is not available for project" + account + ": " + text
    if "notfound" in lower or "not found" in lower:
        return "service account user, project, or Drive target was not found" + account + ": " + text
    if text == "":
        return "rclone about failed with no error output" + account
    return "rclone about failed" + account + ": " + text


def _identity_value(identity, key):
    if hasattr(identity, "get"):
        return identity.get(key)
    try:
        return identity[key]
    except Exception:
        return None


def sa_import():
    if len(__args) < 2:
        logging.error('invalid sa-import command')
        usage_sa_import()
        sys.exit(-1)
    registry = _service_account_registry()
    result = registry.import_paths(
        __args[1:],
        __config['sa_clean_invalid'],
        validator=_service_account_live_validator,
        progress=_sa_import_progress,
    )
    common.print_line('service account import complete')
    common.print_line('scanned:      ' + str(result.scanned))
    common.print_line('validated:    ' + str(result.validated))
    common.print_line('imported:     ' + str(result.imported))
    common.print_line('duplicates:   ' + str(result.duplicates))
    common.print_line('invalid:      ' + str(result.invalid))
    common.print_line('validation errors: ' + str(result.validation_errors))
    common.print_line('quarantined:  ' + str(result.quarantined))
    common.print_line('deleted:      ' + str(result.deleted))
    common.print_line('store:        ' + os.path.abspath(os.path.expanduser(__config['sa_store'])))
    common.print_line('database:     ' + os.path.abspath(os.path.expanduser(__config['sa_db'])))


def sa_stats():
    registry = _service_account_registry()
    refresh_mode = __config['sa_refresh']
    if globals().get('__sa_refresh') is None and refresh_mode == service_accounts.DEFAULT_REFRESH_MODE:
        refresh_mode = 'stale'
    refreshed = 0
    files_cached = 0
    file_cache_errors = 0
    for account in registry.active_accounts():
        quota_row = registry.quota_by_account_id(account['id'])
        if registry.should_refresh(quota_row, refresh_mode):
            quota, error = _refresh_service_account_quota(account)
            registry.update_quota(account['id'], quota, error)
            refreshed += 1
        ls_cache_row = registry.ls_cache_by_account_id(account['id'], '/')
        if registry.should_refresh_ls_cache(ls_cache_row, refresh_mode):
            json_text, error = _refresh_service_account_file_cache(account)
            registry.update_ls_cache(account['id'], '/', json_text, error)
            if error is None:
                files_cached += 1
            else:
                file_cache_errors += 1

    counts = registry.summary_counts()
    ls_summary = registry.ls_cache_summary()
    rows = registry.all_account_stats()
    active_rows = [row for row in rows if row['status'] == 'active']
    stale_count = 0
    error_count = 0
    unknown_count = 0
    total = 0
    used = 0
    free = 0
    for row in active_rows:
        if row['last_about_at'] is None:
            unknown_count += 1
        elif registry.is_stale(row['last_about_at']):
            stale_count += 1
        if row['last_error'] is not None:
            error_count += 1
        if row['total'] is not None:
            total += row['total']
        if row['used'] is not None:
            used += row['used']
        if row['free'] is not None:
            free += row['free']

    common.print_line('SERVICE ACCOUNT SUMMARY')
    common.print_line('active:      ' + str(counts.get('active', 0)))
    common.print_line('duplicates:  ' + str(counts.get('duplicate', 0)))
    common.print_line('invalid:     ' + str(counts.get('invalid', 0)))
    common.print_line('refreshed:   ' + str(refreshed))
    common.print_line('file caches: ' + str(files_cached))
    common.print_line('file cache errors: ' + str(file_cache_errors))
    common.print_line('cached paths:' + str(ls_summary['cached_paths']))
    common.print_line('cached files:' + str(ls_summary['files']))
    common.print_line('errors:      ' + str(error_count))
    common.print_line('stale:       ' + str(stale_count))
    common.print_line('unknown:     ' + str(unknown_count))
    common.print_line('')
    common.print_line('ACCOUNT'.ljust(44) + " " +
                      'SIZE'.rjust(12) + " " +
                      'USED'.rjust(12) + " " +
                      'FREE'.rjust(12) + " " +
                      '%FREE'.rjust(8) + " " +
                      'UPDATED'.ljust(20) + " " +
                      'ERROR')
    common.print_line(''.join('=' for i in range(44)) + " " +
                      ''.join('=' for i in range(12)) + " " +
                      ''.join('=' for i in range(12)) + " " +
                      ''.join('=' for i in range(12)) + " " +
                      ''.join('=' for i in range(8)) + " " +
                      ''.join('=' for i in range(20)) + " " +
                      ''.join('=' for i in range(20)))
    display_unit = __config['display_unit']
    for row in active_rows:
        account = row['client_email'] or row['account_key'] or str(row['id'])
        if len(account) > 44:
            account = account[:41] + '...'
        common.print_line(account.ljust(44) + " " +
                          _format_amount(row['total'], display_unit).rjust(12) + " " +
                          _format_amount(row['used'], display_unit).rjust(12) + " " +
                          _format_amount(row['free'], display_unit).rjust(12) + " " +
                          _format_percent(row['free'], row['total']).rjust(8) + " " +
                          str(row['last_about_at'] or 'UNKNOWN').ljust(20) + " " +
                          str(row['last_error'] or ''))
    common.print_line(''.join('-' for i in range(44)) + " " +
                      ''.join('-' for i in range(12)) + " " +
                      ''.join('-' for i in range(12)) + " " +
                      ''.join('-' for i in range(12)) + " " +
                      ''.join('-' for i in range(8)) + " " +
                      ''.join('-' for i in range(20)) + " " +
                      ''.join('-' for i in range(20)))
    common.print_line('total:'.ljust(44) + " " +
                      _format_amount(total, display_unit).rjust(12) + " " +
                      _format_amount(used, display_unit).rjust(12) + " " +
                      _format_amount(free, display_unit).rjust(12) + " " +
                      _format_percent(free, total).rjust(8))


def _refresh_service_account_quota(account):
    if account['managed_path'] is None:
        return None, 'missing managed service account file'
    fd, tmp_conf = tempfile.mkstemp(prefix="rclone-sa-", suffix=".conf")
    os.close(fd)
    try:
        rclone.generate_rclone_config_from_files(
            [account['managed_path']],
            tmp_conf,
            __config.get('drive_id'),
            prefix="sa",
            start_index=1,
        )
        rclone_exe = __config.get('rclone_exe', 'rclone')
        rclone_retries = __config.get('rclone_retries', '1')
        rc = rclone.RClone(tmp_conf, rclone_exe, rclone_retries)
        quota, error = rc.get_about_json_with_error("sa1:")
        if error is not None:
            return None, _friendly_rclone_error(error, account)
        unknown_reason = _quota_unknown_reason(quota)
        if unknown_reason is not None:
            return None, unknown_reason
        return quota, None
    except Exception as e:
        return None, str(e)
    finally:
        try:
            os.unlink(tmp_conf)
        except Exception:
            pass


def _refresh_service_account_file_cache(account):
    if account['managed_path'] is None:
        return None, 'missing managed service account file'
    fd, tmp_conf = tempfile.mkstemp(prefix="rclone-sa-files-", suffix=".conf")
    os.close(fd)
    try:
        rclone.generate_rclone_config_from_files(
            [account['managed_path']],
            tmp_conf,
            __config.get('drive_id'),
            prefix="sa_files",
            start_index=1,
        )
        rclone_exe = __config.get('rclone_exe', 'rclone')
        rclone_retries = __config.get('rclone_retries', '1')
        rc = rclone.RClone(tmp_conf, rclone_exe, rclone_retries)
        return rc.lsjson("sa_files1:", "/", ['--recursive', '--fast-list'], True), None
    except Exception as e:
        return None, _friendly_rclone_error(e, account)
    finally:
        try:
            os.unlink(tmp_conf)
        except Exception:
            pass


def remove_duplicates():
    global __cl_sync
    common.print_line('removing duplicates')
    if __cl_sync is None:
        __cl_sync = clsync.ClSync(__config)
    if len(__args) == 1:
        logging.error('invalid removedups command')
        usage_removedups()
        sys.exit(-1)
    __cl_sync.remove_duplicates(common.remove_ending_slash(__args[1]))


def find():
    global __cl_sync
    if __cl_sync is None:
        __cl_sync = clsync.ClSync(__config)
    if len(__args) == 1:
        logging.error('invalid find command')
        usage_find()
        sys.exit(-1)
    files = __cl_sync.find(common.remove_ending_slash(__args[1]))
    largest_length = 25
    keys = common.sort_dict_keys(files)
    for tmp_file in keys:
        filename_length = len(files[tmp_file].path)
        if not files[tmp_file].is_dir and filename_length > largest_length:
            largest_length = filename_length
    common.print_line('---' + " " +
                      'NAME'.ljust(largest_length) + " " +
                      'SIZE'.rjust(9) + " " +
                      'MOD TIME'.ljust(19) + " " +
                      'REMOTE'
                      )
    common.print_line('---' + " " +
                      ''.join('-' for i in range(largest_length)) + " " +
                      ''.join('-' for i in range(9)) + " " +
                      ''.join('-' for i in range(19)) + " " +
                      ''.join('-' for i in range(15))
                      )
    for tmp_file in keys:
        if files[tmp_file].is_dir is True:
            first_chars = '-d-'
        else:
            first_chars = '---'
        file_name = files[tmp_file].path
        if file_name.startswith('//'):
            file_name = file_name[1:len(file_name)]
        common.print_line(first_chars + " " +
                          file_name.ljust(largest_length) + " " +
                          str(files[tmp_file].size).rjust(9) + " " +
                          common.get_printable_datetime(files[tmp_file].mod_time).ljust(19) + " " +
                          files[tmp_file].remote
                          )


def check_single_instance():
    if __single_instance is not None and __single_instance is True:
        try:
            lock.acquire(timeout=1)
        except Timeout:
            logging.error('sprinkle is running in another instance!')
            sys.exit(-1)


def check_prerequisites():
    logging.info('checking prerequisites, examine the error messages below...')
    check_reault = True
    try:
        __cl_sync = clsync.ClSync(__config)
        __cl_sync.get_remotes()
    except:
        check_reault = False
    if check_reault is True:
        logging.info('**** PASSED! ****')
    else:
        logging.info('**** FAILED! *****')


def main(argv):
    read_args(argv)
    if len(__args) > 0 and __args[0] == 'config':
        config_command(output_path=__configfile)
        sys.exit(0)
    configure(__configfile)
    if command_needs_rclone_config():
        prepare_rclone_sa_config()
    verify_configuration()
    if __check_prereq is not None and __check_prereq is True:
        check_prerequisites()
        sys.exit(0)
    check_single_instance()
    logging.debug('config: ' + str(__config))

    if __log_file is not None:
        print('sprinkle is logging to file ' + __log_file + '...')
    try:
        if __args[0] == 'ls':
            ls()
        elif __args[0] == 'lsmd5':
            lsmd5()
        elif __args[0] == 'backup':
            if __daemon_mode is True:
                try:
                    sd = sprinkle_daemon.SprinkleDaemon(__config, __args[1])
                    sd.start()
                except Exception as e:
                    logging.error('error starting daemon')
                    logging.error('error message: ' + str(e))
                    if __cmd_debug is True:
                        traceback.print_exc(file=sys.stderr)
            else:
                try:
                    backup()
                except Exception as e:
                    if __config['smtp_enable'] is True:
                        logging.info('sending email')
                        email = smtp_email.EMail()
                        email.set_from(__config['smtp_from'])
                        email.set_to(__config['smtp_to'])
                        email.set_smtp_server(__config['smtp_server'])
                        email.set_smtp_port(__config['smtp_port'])
                        if 'smtp_user' in __config:
                            email.set_smtp_user(__config['smtp_user'])
                        if 'smtp_password' in __config:
                            email.set_smtp_password(__config['smtp_password'])
                        email.set_subject('Sprinkle Failure Notification')
                        email.set_message('Sprinkle has experienced the following error:\n\n' + str(e) +
                                          '\n\nExamine logs for additional information')
                        email.send()
                    raise e
        elif __args[0] == 'restore':
            restore()
        elif __args[0] == 'stats':
            stats()
        elif __args[0] == 'sa-import':
            sa_import()
        elif __args[0] == 'sa-stats':
            sa_stats()
        elif __args[0] == 'removedups':
            remove_duplicates()
        elif __args[0] == 'find':
            find()
        elif __args[0] == 'help':
            if len(__args) < 2:
                usage_help()
            else:
                if __args[1] == 'ls':
                    usage_ls()
                elif __args[1] == 'lsmd5':
                    usage_lsmd5()
                elif __args[1] == 'backup':
                    usage_backup()
                elif __args[1] == 'restore':
                    usage_restore()
                elif __args[1] == 'stats':
                    usage_stats()
                elif __args[1] == 'sa-import':
                    usage_sa_import()
                elif __args[1] == 'sa-stats':
                    usage_sa_stats()
                elif __args[1] == 'removedups':
                    usage_removedups()
                elif __args[1] == 'config':
                    usage_config()
                elif __args[1] == 'find':
                    usage_find()
                else:
                    print('')
                    print('invalid command. Use help [command]')
                    sys.exit(-1)

            quit()
        else:
            print('')
            print('invalid command. Use help [command]')
            sys.exit(-1)
    except Exception as e:
        if __cmd_debug is True:
            traceback.print_exc(file=sys.stderr)

if __name__ == "__main__":
    # execute only if run as a script
    main(sys.argv[1:])
