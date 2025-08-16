# Sprinkle (Volume Clustering)


Sprinkle is a volume clustering utility. It presents all the RClone available volumes as a single clustered volume. It supports 1-way sync mainly for
backup and recovery. Sprinkle uses the excellent [RClone](https://rclone.org) software for cloud volume access.

## Special Features

* Docker Image [dbiesecke/sprinkle](https://hub.docker.com/r/dbiesecke/sprinkle)
  
* load sa accounts from directory, random selection, gdrive_id option & with limiter

```bash
# will load 5 sa accounts from /etc/rclone/sa & root_folder it is set to "XXXXX" ( your public gdrive dir)

$ docker run -i -v /etc/rclone:/etc/rclone:ro dbiesecke/sprinkle --rclone-sa-count 5 --drive-id XXXXX -d --rclone-sa-dir /etc/rclone/sa stats
```



Features:
* Consolidate multiple cloud drives into a single virtual drive
* Sprinkle your backup across multiple cloud drives
* Minimize cost by stacking multiple free cloud drives into single one
* Run as Unix daemon with custom schedules for seamless backups of important files
* Developed in Python for extreme multi-platform flexibility

## Getting Started

The easiest way to install Sprinkle and all prerequisites is via PyPI with:
```
pip3 install sprinkle-py
```

Or by cloning the repository to your running machine, but make sure prerequisites are met:
```
git clone https://gitlab.com/mmontuori/sprinkle.git
cd sprinkle
./sprinkle.py -c sprinkle.conf ls /
```
A more comprehensive guide can be found [here](https://mmontuori.github.io/sprinkle/docs/guide)

## Prerequisites

* Python 3 installed
* FileLock Python library [https://pypi.org/project/filelock](https://pypi.org/project/filelock)
* Progress Python library [https://pypi.org/project/progress](https://pypi.org/project/progress)
* RClone installed and available in the PATH or configured in sprinkle.conf file. RClone documentation
is available [here](https://rclone.org) for reference
* Few storage drives available from the supported RClone drives

## Installing

Following are the installation steps:

* Install Sprinkle with a supported method
* Download and install RCLone from [https://rclone.org](https://rclone.org)
* Run **RClone** config to configure and authorize your cloud or local storage
  (you might want to run the program on a machione for which http://localhost can be reached
  ideally, from your local workstation)
* Verify access to the storage by issuing the command "rclone ls {alias name}:"
* Copy rclone.conf on the machine which will execute Sprinkle
* Make sure all the prerequisites are satisfied
* Add **RClone** executable to the system PATH variable, or configure location in sprinkle.conf file
* From Sprinkle installation directory run **"./sprinkle.py [-c path to sprinkle.conf] ls /"**

From this point, backups and restore can be executed on the clustered storage.

```
./sprinkle.py -c {path to sprinkle.conf} backup {directory to backup}
```

Use the builtin --help utility to get additional commands and information.

```
./sprinkle.py --help
```

and the command specific help.

```
    -c, --conf {config file}     configuration file
    -d, --debug                  debug output
    -h, --help                   help
    -v, --version                print version
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
    --rclone-sa-dir {dir}        build rclone config from service accounts
    --rclone-sa-count {num}      limit number of service accounts used
    --drive-id {id}              Google Drive folder ID for rclone config
    --rclone-exe {rclone_exe}    rclone executable (default:rclone)
    --restore-duplicates         restore files if duplicates are found (default:false)
    --retries {num_retries}      number of retries (default:1)
    --show-progress              show progress
    --single-instance            make sure only 1 concurrent instance of sprinkle is running (default:False)
    --ls-stop-first             stop listing after first remote with files
    
```

## Authors

* **Michael Montuori** - *Head developer* - [mmontuori](https://gitlab.com/mmontuori)
* **Daniel** - *Fork* [dbiesecke](https://gitlab.com/dbiesecke)

## License

This project is licensed under the GPLv3 License - see the
[LICENSE](https://www.gnu.org/licenses/gpl-3.0.en.html) file for details

## Acknowledgments

* Warren Crigger for development support
