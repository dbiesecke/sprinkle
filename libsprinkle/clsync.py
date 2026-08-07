#!/usr/bin/env python3
"""
clsync module
"""
__author__ = "Michael Montuori [michael.montuori@gmail.com]"
__copyright__ = "Copyright 2017 Michael Montuori. All rights reserved."
__credits__ = ["Warren Crigger"]
__license__ = "GPLv3"
__version__ = "1.2"
__revision__ = "0"

import logging
from libsprinkle import rclone
from libsprinkle import common
from libsprinkle import clfile
from libsprinkle import exceptions
from libsprinkle import operation
from libsprinkle import service_accounts
try:
    from progress.bar import Bar
except:
    print("Progress library not found. run command 'pip3 install progress'")
    quit()
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

DEFAULT_LARGE_FILE_THRESHOLD_BYTES = 1024 * 1024 * 1024
DEFAULT_LARGE_FILE_MIN_FREE_BYTES = 512 * 1024 * 1024
DEFAULT_LARGE_FILE_MIN_FREE_PERCENT = 5
BACKUP_TRANSFER_WORKERS = 2
BACKUP_DIRECTORY_BATCH_MAX_FILES = 64

class ClSync:

    duplicate_suffix = ".sprinkle_duplicate_file"

    def __init__(self, config):
        logging.debug('constructing ClSync')
        if config is None:
            logging.error("configuration is None. Cannot continue!")
            raise Exception("None value for configuration")
        if 'rclone_workdir' in config and config['rclone_workdir'] == None and not common.is_dir(config['rclone_workdir']):
            logging.error("working directory " + str(config['rclone_workdir']) + " not found. Cannot continue!")
            raise Exception("Working directory " + config['rclone_workdir'] + " not found")
        self._config = config
        if 'rclone_config' in self._config:
            rclone_config = self._config['rclone_config']
        else:
            rclone_config = None
        if 'distribution_type' in config:
            self._distribution_type = config['distribution_type']
        else:
            self._distribution_type = 'mas'
        if self._distribution_type == 'mas':
            self._cached_free = {}
        self._run_unavailable_remotes = {}
        self._run_quota_exhausted_remotes = set()
        self._run_quota_error_remotes = {}
        self._sa_registry = None
        self._sa_refresh = config.get('sa_refresh', service_accounts.DEFAULT_REFRESH_MODE)
        self._large_file_threshold_bytes = int(config.get(
            'large_file_threshold_bytes',
            DEFAULT_LARGE_FILE_THRESHOLD_BYTES,
        ))
        self._large_file_min_free_bytes = int(config.get(
            'large_file_min_free_bytes',
            DEFAULT_LARGE_FILE_MIN_FREE_BYTES,
        ))
        self._large_file_min_free_percent = int(config.get(
            'large_file_min_free_percent',
            DEFAULT_LARGE_FILE_MIN_FREE_PERCENT,
        ))
        if config.get('sa_db') is not None:
            self._sa_registry = service_accounts.ServiceAccountRegistry(
                config.get('sa_db'),
                config.get('sa_store'),
                config.get('sa_cache_ttl_hours', service_accounts.DEFAULT_CACHE_TTL_HOURS),
            )
        self._rc_slots = [str(remote).rstrip(':') + ':' for remote in str(
            config.get('rclone_rc_remotes') or ''
        ).split(',') if str(remote).strip().rstrip(':')]
        self._rc_slot_mode = bool(config.get('rclone_rc_url') and self._rc_slots and self._sa_registry)
        if getattr(self, '_rc_slot_mode', False):
            self._sa_registry.ensure_rc_slots(self._rc_slots)
        if 'compare_method' in config:
            self._compare_method = config['compare_method']
        else:
            self._compare_method = 'size'
        if 'rclone_retries' not in config:
            self._rclone_retries = '1'
        else:
            self._rclone_retries = config['rclone_retries']
        self._remotes = None
        self._remote_calls = 0
        self._sizes = None
        self._frees = None
        self._show_progress = config['show_progress']
        # Keep only two active upload streams.  More streams provide little
        # benefit for the small service-account quotas and make full-account
        # recovery harder to reason about.
        self._backup_transfer_workers = BACKUP_TRANSFER_WORKERS
        self._backup_transfer_lock = threading.Lock()
        self._reserved_free = {}

        if '__exclusion_list' in config:
            self.__exclusion_list = config['__exclusion_list']
        else:
            self.__exclusion_list = None

        if 'exclude_regex' in config:
            self.__exclude_regex = config['exclude_regex']
        else:
            self.__exclude_regex = None

        self._cache = {}
        self._cache_counter = {}
        self._cache_invalidation_max = 1440 / (config['daemon_interval'] * 2)
        if self._cache_invalidation_max < 1:
            self._cache_invalidation_max = 1

        if 'rclone_exe' not in self._config:
            self._rclone = rclone.RClone(
                rclone_config,
                rc_url=self._config.get('rclone_rc_url'),
                rc_user=self._config.get('rclone_rc_user'),
                rc_password=self._config.get('rclone_rc_password'),
                rc_timeout_seconds=self._config.get('rclone_rc_timeout_seconds', 30),
                rc_drive_id=self._config.get('drive_id'),
                rc_drive_remotes=self._config.get('cluster_remotes'),
                transfers=self._backup_transfer_workers,
            )
        else:
            self._rclone = rclone.RClone(
                rclone_config, self._config['rclone_exe'], self._rclone_retries,
                self._config.get('rclone_rc_url'),
                self._config.get('rclone_rc_user'),
                self._config.get('rclone_rc_password'),
                self._config.get('rclone_rc_timeout_seconds', 30),
                self._config.get('drive_id'),
                self._config.get('cluster_remotes'),
                self._backup_transfer_workers,
            )

        if 'rclone_move' in config:
            self._rclone_move = config['rclone_move']
        else:
            self._rclone_move = False

    def get_remotes(self):
        logging.debug('getting rclone remotes')
        if self._config.get('cluster_remotes') not in (None, ''):
            return self._config.get('cluster_remotes')
        if self._remotes is None or self._remote_calls > 100:
            self._remotes = self._rclone.get_remotes()
            self._remote_calls = 0
        self._remote_calls += 1
        return self._remotes

    def mkdir(self, directory):
        logging.debug('makind directory ' + directory)
        for remote in self.get_remotes():
            logging.debug('creating directory ' + remote + directory)
            self._rclone.mkdir(remote, directory)

    def ls(self, file, with_dups=False, regex=None, stop_after_first=None, remotes=None, normalize_path=True):
        return self._ls(
            file,
            with_dups,
            regex,
            stop_after_first,
            recursive=True,
            remotes=remotes,
            normalize_path=normalize_path,
        )

    def ls_shallow(self, file, with_dups=False, regex=None, stop_after_first=None, remotes=None, normalize_path=True):
        return self._ls(
            file,
            with_dups,
            regex,
            stop_after_first,
            recursive=False,
            remotes=remotes,
            normalize_path=normalize_path,
        )

    def _ls(
            self,
            file,
            with_dups=False,
            regex=None,
            stop_after_first=None,
            recursive=True,
            remotes=None,
            normalize_path=True):
        logging.debug('lsjson of file: ' + file)
        if stop_after_first is None:
            stop_after_first=self._config['ls_stop_first']
        if normalize_path and not file.startswith('/'):
            logging.debug('adding / ' + file)
            file = '/' + file
        if remotes is None:
            remotes = self.get_remotes()
        memory_cache_key = (file, with_dups, regex, stop_after_first, recursive, tuple(remotes), normalize_path)
        if self._config['no_cache'] is False and memory_cache_key in self._cache:
            logging.debug('serving cached version of file list...')
            self._cache_counter[memory_cache_key] += 1
            if self._cache_counter[memory_cache_key] <= self._cache_invalidation_max:
                return self._cache[memory_cache_key]
            else:
                self._cache_counter[memory_cache_key] = 0
        if regex is not None:
            regexp = re.compile(regex)
        else:
            regexp = None
        files = {}
        md5s = None
        if self._compare_method == 'md5':
            md5s = self.lsmd5(file, stop_after_first, remotes, normalize_path)
        for remote in remotes:
            common.print_line('retrieving file list from: ' + remote + file + '...')
            logging.debug('getting lsjson from ' + remote + file)
            json_out = self._cached_lsjson(remote, file, recursive)
            logging.debug('loading json')
            tmp_json = json.loads(json_out)
            logging.debug('json size: ' + str(len(tmp_json)))
            logging.debug('json loaded')
            for tmp_json_file in tmp_json:
                tmp_file = clfile.ClFile()
                tmp_file.remote = remote
                tmp_file.path = file + '/' + tmp_json_file['Path']
                tmp_file.name = tmp_json_file['Name']
                tmp_file.size = tmp_json_file['Size']
                tmp_file.mime_type = tmp_json_file['MimeType']
                tmp_file.mod_time = tmp_json_file['ModTime']
                tmp_file.is_dir = tmp_json_file['IsDir']
                tmp_file.id = tmp_json_file.get('ID')
                key = file + '/' + tmp_json_file['Path']
                if regexp is not None and regexp.search(key) is None:
                    logging.debug('skipping ' + key + '...')
                    continue
                if self._compare_method == 'md5' and not tmp_file.is_dir:
                    tmp_file.md5 = md5s[key]
                if with_dups and tmp_file.is_dir is False and key in files:
                    key = key + ClSync.duplicate_suffix
                files[key] = tmp_file
                if stop_after_first and len(files) > 0:
                    self._store_memory_ls_cache(memory_cache_key, files)
                    return files
            if stop_after_first and self._stop_after_first_success():
                self._store_memory_ls_cache(memory_cache_key, files)
                return files
            logging.debug('end of clsync.ls()')
        self._store_memory_ls_cache(memory_cache_key, files)
        return files

    def _cached_lsjson(self, remote, path, recursive=True):
        if self._config['no_cache'] is False and self._sa_registry is not None:
            cached = self._sa_registry.ls_cache_by_remote(remote, path)
            if cached is not None and not self._sa_registry.should_refresh_ls_cache(cached, self._sa_refresh):
                logging.debug('serving cached lsjson for ' + remote + path)
                return self._json_for_listing_depth(cached["json_text"], recursive)
            if cached is not None and self._sa_refresh == 'none':
                logging.debug('serving cached lsjson for ' + remote + path)
                return self._json_for_listing_depth(cached["json_text"], recursive)
            parent_json = self._json_from_cached_parent(remote, path, recursive)
            if parent_json is not None:
                logging.debug('serving parent cached lsjson for ' + remote + path)
                return parent_json
        extra_args = ['--fast-list']
        if recursive:
            extra_args.insert(0, '--recursive')
        try:
            json_out = self._rclone.lsjson(remote, path, extra_args, True)
        except exceptions.FileNotFoundException as e:
            json_out = '[]'
        except Exception as e:
            if self._config['no_cache'] is False and self._sa_registry is not None:
                self._sa_registry.update_ls_cache_for_remote(remote, path, None, str(e))
            raise
        if recursive and self._config['no_cache'] is False and self._sa_registry is not None:
            self._sa_registry.update_ls_cache_for_remote(remote, path, json_out, None)
        return json_out

    def _json_from_cached_parent(self, remote, path, recursive=True):
        if path == '/':
            return None
        root_cache = self._sa_registry.ls_cache_by_remote(remote, '/')
        if root_cache is None:
            return None
        if self._sa_registry.should_refresh_ls_cache(root_cache, self._sa_refresh):
            return None
        prefix = path.strip('/')
        try:
            rows = json.loads(root_cache["json_text"])
        except Exception:
            return None
        filtered = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_path = row.get("Path", "")
            if row_path == prefix:
                continue
            if not row_path.startswith(prefix + '/'):
                continue
            copied = dict(row)
            copied["Path"] = row_path[len(prefix) + 1:]
            if not recursive and '/' in copied["Path"]:
                continue
            filtered.append(copied)
        return json.dumps(filtered)

    def _json_for_listing_depth(self, json_text, recursive=True):
        if recursive:
            return json_text
        try:
            rows = json.loads(json_text)
        except Exception:
            return json_text
        if not isinstance(rows, list):
            return json_text
        filtered = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if '/' in row.get("Path", ""):
                continue
            filtered.append(row)
        return json.dumps(filtered)

    def _stop_after_first_success(self):
        return self._config.get('drive_id') not in (None, '')

    def _store_memory_ls_cache(self, cache_key, files):
        if self._config['no_cache'] is False:
            self._cache[cache_key] = files
            self._cache_counter[cache_key] = 0

    def lsmd5(self, file, stop_after_first=False, remotes=None, normalize_path=True):
        logging.debug('lsjson of file: ' + file)
        if normalize_path and not file.startswith('/'):
            file = '/' + file
        files = {}
        if remotes is None:
            remotes = self.get_remotes()
        for remote in remotes:
            common.print_line('retrieving file list from: ' + remote + file + '...')
            logging.debug('getting lsjson from ' + remote + file)
            try:
                out = self._rclone.md5sum(remote, file, ['--fast-list'], True)
            except exceptions.FileNotFoundException as e:
                out = ''
            #logging.debug('out: ' + str(out.split('\n')))
            md5s = out.split('\n')
            for line in md5s:
                if line == '':
                    continue
                md5 = line.split('  ')[0]
                filename = line.split('  ')[1]
                files[file + '/' + filename] = md5
            if stop_after_first and len(files) > 0:
                break
        return files

    def get_sizes(self):
        logging.debug('getting sizes')
        if self._sizes is None:
            self._sizes = {}
            for remote in self.get_remotes():
                quota = self._get_remote_quota(remote)
                size = self._quota_value(quota, 'total')
                logging.debug('size of ' + remote + ' is ' + str(size))
                self._sizes[remote] = size
        return self._sizes

    def get_size(self):
        logging.debug('getting sizes')
        total_size = 0
        for remote in self.get_remotes():
            if self._sizes is None:
                quota = self._get_remote_quota(remote)
                size = self._quota_value(quota, 'total')
            else:
                size = self._sizes[remote]
            logging.debug('size of ' + remote + ' is ' + str(size))
            if size is not None:
                total_size += size
        return total_size

    def get_frees(self):
        logging.debug('getting free sizes')
        if self._frees is None:
            self._frees = {}
            for remote in self.get_remotes():
                quota = self._get_remote_quota(remote)
                size = self._quota_value(quota, 'free')
                logging.debug('free of ' + remote + ' is ' + str(size))
                self._frees[remote] = size
        return self._frees

    def get_free(self):
        logging.debug('getting total free size')
        total_size = 0
        for remote in self.get_remotes():
            if self._frees is None:
                quota = self._get_remote_quota(remote)
                size = self._quota_value(quota, 'free')
            else:
                size = self._frees[remote]
            logging.debug('free of ' + remote + ' is ' + str(size))
            if size is not None:
                total_size += size
        return total_size

    def get_max_file_size(self):
        logging.debug('getting total maximum file size')
        total_size = 0
        for remote in self.get_remotes():
            quota = self._get_remote_quota(remote)
            size = self._quota_value(quota, 'free')
            logging.debug('free of ' + remote + ' is ' + str(size))
            if size is not None and size > total_size:
                total_size = size
        return total_size

    def get_best_remote(self, requested_size=1):
        remotes = self.get_eligible_remotes(requested_size)
        if not remotes:
            required_size = self._required_free_for_upload(requested_size)
            raise Exception(
                'no remote has enough known free space for requested size ' +
                str(requested_size) + ' with required free ' + str(required_size)
            )
        return remotes[0]

    def get_eligible_remotes(self, requested_size=1):
        if self._distribution_type != 'mas':
            logging.error('distribution mode ' + self._distribution_type + ' not supported.')
            raise Exception('unsupported distribution mode ' + self._distribution_type)
        required_size = self._required_free_for_upload(requested_size)
        if getattr(self, '_rc_slot_mode', False):
            self._bind_empty_rc_slots(required_size)
        logging.debug(
            'selecting remotes with enough available space to store size: ' +
            str(requested_size) + ', required free: ' + str(required_size)
        )
        candidates = []
        for remote in self.get_remotes():
            if remote in getattr(self, '_run_unavailable_remotes', {}):
                continue
            if remote in getattr(self, '_run_quota_exhausted_remotes', set()):
                continue
            if remote in getattr(self, '_run_quota_error_remotes', {}):
                continue
            size = self._known_free_for_remote(remote)
            logging.debug('free of ' + remote + ' is ' + str(size))
            if size is not None and required_size <= size:
                candidates.append((size, remote))
        candidates.sort(reverse=True)
        if candidates:
            return [remote for _size, remote in candidates]
        if getattr(self, '_rc_slot_mode', False) and self._rotate_rc_slot(required_size):
            # The new binding has a fresh cached quota; repeat normal filtering.
            return self.get_eligible_remotes(requested_size)
        return []

    def _bind_empty_rc_slots(self, required_size):
        for name in self._sa_registry.empty_rc_slots(self._rc_slots):
            account = self._sa_registry.eligible_unbound_account(required_size)
            if account is None:
                return
            try:
                self._configure_rc_slot(name, account)
            except Exception as exc:
                self.mark_remote_unavailable_for_run(name + ':', exc)
                return

    def _rotate_rc_slot(self, required_size):
        """Replace a full configured slot only when the next file cannot fit."""
        account = self._sa_registry.eligible_unbound_account(required_size)
        if account is None:
            return False
        choices = []
        for remote in self._rc_slots:
            if remote in self._run_unavailable_remotes:
                continue
            bound = self._sa_registry.rc_slot_account(remote)
            free = None if bound is None else bound['free']
            if not service_accounts.has_usable_quota(bound, required_size):
                choices.append((0 if free is None else int(free), remote.rstrip(':')))
        if not choices:
            return False
        _free, name = sorted(choices)[0]
        try:
            self._configure_rc_slot(name, account)
            return True
        except Exception as exc:
            self.mark_remote_unavailable_for_run(name + ':', exc)
            return False

    def _configure_rc_slot(self, name, account):
        """Push credentials to RC and bind only after RC reports known quota."""
        managed_path = account['managed_path']
        if not managed_path:
            raise Exception('service account has no managed credential file')
        try:
            with open(managed_path, 'r') as fp:
                credentials = json.load(fp)
        except Exception as exc:
            raise Exception('unable to read managed service account credential: {}'.format(exc.__class__.__name__))
        self._rclone.configure_rc_drive_service_account(name, credentials)
        quota, error = self._rclone.get_about_json_with_error(name + ':')
        if error is not None:
            raise Exception('RC slot {} configured but quota check failed: {}'.format(name, str(error)[:300]))
        if not service_accounts.has_usable_quota(quota):
            reason = 'RC slot {} configured but returned unknown quota (missing total/free)'.format(name)
            self._sa_registry.update_quota(account['id'], None, reason)
            raise Exception(reason)
        free = self._quota_value(quota, 'free')
        self._sa_registry.bind_rc_slot(name, account['id'])
        self._sa_registry.update_quota(account['id'], quota, None)
        remote = name + ':'
        self._cached_free[remote] = free
        if self._frees is not None:
            self._frees[remote] = free
        logging.info('configured RC slot %s for service account %s', name, account['client_email'])

    def ensure_remote_has_enough_space(self, remote, requested_size):
        required_size = self._required_free_for_upload(requested_size)
        free_size = self._known_free_for_remote(remote)
        if free_size is None or free_size < required_size:
            raise Exception(
                'remote ' + remote + ' does not have enough known free space for requested size ' +
                str(requested_size) + ' with required free ' + str(required_size)
            )
        return remote

    def _known_free_for_remote(self, remote):
        if remote not in self._cached_free:
            quota = self._get_remote_quota(remote)
            free_size = self._quota_value(quota, 'free')
            # Unknown quota must be checked again for later files, not cached as capacity.
            if free_size is not None:
                self._cached_free[remote] = free_size
        return self._cached_free.get(remote)

    def _required_free_for_upload(self, requested_size):
        requested_size = int(requested_size)
        if requested_size < self._large_file_threshold_bytes:
            return requested_size
        percent_margin = int(requested_size * self._large_file_min_free_percent / 100)
        margin = max(self._large_file_min_free_bytes, percent_margin)
        return requested_size + margin

    def mark_remote_used(self, remote, size):
        if self._distribution_type == 'mas':
            if remote in self._cached_free and self._cached_free[remote] is not None:
                self._cached_free[remote] = max(0, self._cached_free[remote] - int(size))
            if self._frees is not None and remote in self._frees and self._frees[remote] is not None:
                self._frees[remote] = max(0, self._frees[remote] - int(size))
        if getattr(self, '_sa_registry', None) is not None:
            self._sa_registry.adjust_quota_for_remote(remote, int(size))
            self._sa_registry.invalidate_ls_cache_for_remote(remote)
        self._clear_memory_ls_cache()

    def _confirmed_target_file_size(self, remote, directory, name):
        try:
            rows = self._rclone.lsjson(remote, directory, ['--fast-list'], True)
            if isinstance(rows, str):
                rows = json.loads(rows)
        except Exception as exc:
            raise Exception('quota cache verification failed: {}'.format(exc))
        if not isinstance(rows, list):
            return None
        for row in rows:
            if row.get('Name') == name and not row.get('IsDir'):
                return row.get('Size')
        return None

    def _record_confirmed_transfer(self, remote, directory, name, previous_size, expected_size):
        if getattr(self, '_sa_registry', None) is None:
            self.mark_remote_used(remote, int(expected_size) - int(previous_size))
            return
        confirmed_size = self._confirmed_target_file_size(remote, directory, name)
        if confirmed_size is None:
            raise Exception('quota cache verification failed: target file not found after transfer')
        self.mark_remote_used(remote, int(confirmed_size) - int(previous_size))

    def _ensure_target_directory(self, remote, directory):
        """Create the destination before a transfer, retaining it after a failed copy."""
        mkdir = getattr(getattr(self, '_rclone', None), 'mkdir', None)
        if mkdir is not None:
            mkdir(remote, directory)

    def mark_remote_quota_exhausted(self, remote):
        """Exclude a remote after Google confirms that its quota is exhausted."""
        exhausted = getattr(self, '_run_quota_exhausted_remotes', None)
        if exhausted is None:
            exhausted = set()
            self._run_quota_exhausted_remotes = exhausted
        exhausted.add(remote)
        self._cached_free[remote] = 0
        if getattr(self, '_frees', None) is not None and remote in self._frees:
            self._frees[remote] = 0
        if self._sa_registry is not None:
            self._sa_registry.mark_remote_quota_exhausted(remote)

    def mark_remote_unavailable_for_run(self, remote, error):
        """Avoid retrying a failed remote for every later ADD in this backup run."""
        unavailable = getattr(self, '_run_unavailable_remotes', None)
        if unavailable is None:
            unavailable = {}
            self._run_unavailable_remotes = unavailable
        if remote not in unavailable:
            unavailable[remote] = str(error)[:300]
            logging.warning(
                'excluding %s for the remainder of this backup run after transfer failure: %s',
                remote,
                unavailable[remote],
            )

    def mark_remote_quota_error_for_run(self, remote, error):
        """Avoid repeated failing quota requests without inventing a quota value."""
        unavailable = getattr(self, '_run_quota_error_remotes', None)
        if unavailable is None:
            unavailable = {}
            self._run_quota_error_remotes = unavailable
        if remote not in unavailable:
            unavailable[remote] = str(error)[:300]
            logging.warning(
                'excluding %s for the remainder of this backup run after quota query failure: %s',
                remote,
                unavailable[remote],
            )

    def _is_storage_quota_exceeded(self, error):
        text = str(error).lower()
        return (
            'storagequotaexceeded' in text or
            'drive storage quota has been exceeded' in text
        )

    def _get_remote_quota(self, remote):
        cached = None
        if self._sa_registry is not None:
            cached = self._sa_registry.quota_by_remote(remote)
            if cached is not None and not service_accounts.has_usable_quota(cached):
                reason = cached['last_error'] or 'rclone about returned unknown quota: missing total,free'
                if self._sa_refresh == 'none' or not self._sa_registry.should_refresh(cached, self._sa_refresh):
                    self.mark_remote_quota_error_for_run(remote, reason)
                    return None
            elif cached is not None and not self._sa_registry.should_refresh(cached, self._sa_refresh):
                return self._quota_from_row(cached)
            if cached is not None and self._sa_refresh == 'none':
                return self._quota_from_row(cached)
        quota_error = None
        try:
            get_with_error = getattr(self._rclone, 'get_about_json_with_error', None)
            if get_with_error is not None:
                quota, quota_error = get_with_error(remote)
            else:
                quota = self._rclone.get_about_json(remote, True)
        except Exception as e:
            quota = None
            quota_error = str(e)
        if quota_error is None and not service_accounts.has_usable_quota(quota):
            quota_error = 'rclone about returned unknown quota: missing total,free'
            quota = None
        if quota_error is not None:
            logging.debug('error refreshing quota for ' + remote + ': ' + str(quota_error))
            self.mark_remote_quota_error_for_run(remote, quota_error)
        if self._sa_registry is not None and cached is not None:
            if quota is None:
                self._sa_registry.update_quota_for_remote(remote, None, quota_error)
                return None
            self._sa_registry.update_quota_for_remote(remote, quota, None)
        return quota

    def _quota_from_row(self, row):
        if not service_accounts.has_usable_quota(row):
            return None
        quota = {}
        for key in ('total', 'used', 'free', 'trashed', 'other', 'objects'):
            quota[key] = row[key]
        if all(quota[key] is None for key in quota):
            return None
        return quota

    def _quota_value(self, quota, key):
        if quota is None:
            return None
        return quota.get(key)

    def index_local_dir(self, local_dir, exclusion_list=None):
        common.print_line('indexing local directory: ' + local_dir + '...')
        if self.__exclude_regex is not None:
            regexp = re.compile(self.__exclude_regex)
        else:
            regexp = None
        clfiles = {}
        for root, dirs, files in os.walk(local_dir):
            for name in dirs:
                full_path = os.path.join(root, name).replace('\\', '/')
                logging.debug('adding ' + full_path + ' to list')
                if exclusion_list is not None:
                    exclusion_found = False
                    for exclusion in exclusion_list:
                        if exclusion in full_path:
                            exclusion_found = True
                    if exclusion_found is True:
                        logging.debug('exclusion ' + exclusion + ' applied for path ' + full_path)
                        continue
                if self.__exclude_regex is not None and regexp.search(full_path) is not None:
                    logging.debug('regexp match for path: ' + full_path)
                    continue
                tmp_clfile = clfile.ClFile()
                tmp_clfile.is_dir = True
                tmp_clfile.path = os.path.dirname(full_path)
                tmp_clfile.name = name
                tmp_clfile.size = "-1"
                tmp_clfile.mod_time = os.stat(full_path).st_mtime
                clfiles[common.normalize_path(tmp_clfile.path+'/'+tmp_clfile.name)] = tmp_clfile
            for name in files:
                full_path = os.path.join(root, name)
                logging.debug('adding ' + full_path + ' to list')
                if exclusion_list is not None:
                    exclusion_found = False
                    for exclusion in exclusion_list:
                        if exclusion in full_path:
                            exclusion_found = True
                    if exclusion_found is True:
                        logging.debug('exclusion ' + exclusion + ' applies for ' + full_path)
                        continue
                if self.__exclude_regex is not None and regexp.search(full_path) is not None:
                    logging.debug('regexp match for path: ' + full_path)
                    continue
                tmp_clfile = clfile.ClFile()
                tmp_clfile.is_dir = False
                tmp_clfile.path = os.path.dirname(full_path)
                tmp_clfile.name = name
                tmp_clfile.size = os.stat(full_path).st_size
                tmp_clfile.mod_time = os.stat(full_path).st_mtime
                if self._compare_method == 'md5':
                    tmp_clfile.md5 = common.get_md5(full_path)
                clfiles[common.normalize_path(tmp_clfile.path+'/'+tmp_clfile.name)] = tmp_clfile
        logging.debug('retrieved ' + str(len(clfiles)) + ' files')
        return clfiles

    def index_local_file(self, local_file, exclusion_list=None):
        common.print_line('indexing local file: ' + local_file + '...')
        full_path = os.path.abspath(local_file)
        if exclusion_list is not None and any(exclusion in full_path for exclusion in exclusion_list):
            return {}
        if self.__exclude_regex is not None and re.search(self.__exclude_regex, full_path) is not None:
            return {}
        tmp_clfile = clfile.ClFile()
        tmp_clfile.is_dir = False
        tmp_clfile.path = os.path.dirname(full_path)
        tmp_clfile.name = os.path.basename(full_path)
        tmp_clfile.size = os.stat(full_path).st_size
        tmp_clfile.mod_time = os.stat(full_path).st_mtime
        if self._compare_method == 'md5':
            tmp_clfile.md5 = common.get_md5(full_path)
        return {common.normalize_path(full_path): tmp_clfile}

    def index_remote_dir(self, remote, remote_path, exclusion_list=None):
        source = remote + remote_path
        common.print_line('indexing rclone remote: ' + source + '...')
        if self.__exclude_regex is not None:
            regexp = re.compile(self.__exclude_regex)
        else:
            regexp = None
        clfiles = {}
        try:
            json_out = self._rclone.lsjson(remote, remote_path, ['--recursive', '--fast-list'], True)
        except exceptions.FileNotFoundException:
            json_out = '[]'
        rows = json.loads(json_out)
        for row in rows:
            if not isinstance(row, dict):
                continue
            full_path = self._join_rclone_path(remote_path, row.get('Path', ''))
            source_key = remote + full_path
            if exclusion_list is not None:
                exclusion_found = False
                for exclusion in exclusion_list:
                    if exclusion in source_key:
                        exclusion_found = True
                if exclusion_found is True:
                    logging.debug('exclusion ' + exclusion + ' applied for path ' + source_key)
                    continue
            if regexp is not None and regexp.search(source_key) is not None:
                logging.debug('regexp match for path: ' + source_key)
                continue
            tmp_clfile = clfile.ClFile()
            tmp_clfile.remote = remote
            tmp_clfile.is_dir = row.get('IsDir')
            tmp_clfile.path = remote + os.path.dirname(full_path).replace('\\', '/')
            tmp_clfile.name = row.get('Name')
            tmp_clfile.size = row.get('Size')
            tmp_clfile.mime_type = row.get('MimeType')
            tmp_clfile.mod_time = row.get('ModTime')
            tmp_clfile.id = row.get('ID')
            clfiles[common.normalize_path(source_key)] = tmp_clfile
        logging.debug('retrieved ' + str(len(clfiles)) + ' remote files')
        return clfiles

    def _join_rclone_path(self, base, path):
        base = (base or '').replace('\\', '/')
        path = (path or '').replace('\\', '/')
        if base in ('', '/'):
            if base == '/' and not path.startswith('/'):
                return '/' + path
            return path
        return base.rstrip('/') + '/' + path.lstrip('/')

    def compare_clfiles(self, local_dir, local_clfiles, remote_clfiles, delete_file=True):
        remote_root = self.get_backup_remote_root(local_dir)
        return self.compare_clfiles_for_remote_root(
            local_dir,
            local_clfiles,
            remote_clfiles,
            delete_file,
            remote_root,
        )

    def compare_clfiles_for_remote_root(
            self,
            local_dir,
            local_clfiles,
            remote_clfiles,
            delete_file=True,
            remote_root=None,
            source_is_remote=False):
        common.print_line('calculating differences...')
        logging.debug('comparing clfiles')
        logging.debug('local directory: ' + local_dir)
        logging.debug('local clfiles size: ' + str(len(local_clfiles)))
        logging.debug('remote clfiles size: ' + str(len(remote_clfiles)))
        if remote_root is None:
            remote_root = self.get_backup_remote_root(local_dir)
        local_remote_keys = {}
        for local_path in local_clfiles:
            local_clfile = local_clfiles[local_path]
            full_path = local_clfile.path + '/' + local_clfile.name
            remote_key = self.remote_key_for_source_path(local_dir, full_path, remote_root, source_is_remote)
            local_remote_keys[remote_key] = local_clfile
        operations = []
        for local_path in local_clfiles:
            local_clfile = local_clfiles[local_path]
            if local_clfile.is_dir:
                continue
            remote_name = self.remote_key_for_source_path(
                local_dir,
                local_clfile.path + '/' + local_clfile.name,
                remote_root,
                source_is_remote,
            )
            remote_path = os.path.dirname(remote_name).replace('\\', '/')
            if remote_name not in remote_clfiles:
                logging.debug('compare file local=%s remote=%s result=add', local_path, remote_name)
                local_clfile.remote_path = remote_path
                op = operation.Operation(operation.Operation.ADD,
                                         local_clfile, None)
                operations.append(op)
            else:
                logging.debug('compare file local=%s remote=%s result=existing', local_path, remote_name)
                remote_clfile = remote_clfiles[remote_name]
                if self._compare_method == 'size':
                    size_local = local_clfile.size
                    size_remote = remote_clfile.size
                    current_remote = remote_clfiles[remote_name].remote
                    logging.debug('local_file.size:' + str(local_clfile.size) +
                                  ', remote_clfile.size:' + str(remote_clfile.size))
                    if size_local != size_remote:
                        logging.debug('file has changed')
                        local_clfile.remote_path = remote_path
                        local_clfile.remote = current_remote
                        op = operation.Operation(operation.Operation.UPDATE,
                                                 local_clfile, remote_clfile)
                        operations.append(op)
                elif self._compare_method == 'md5':
                    local_md5 = local_clfile.md5
                    remote_md5 = remote_clfile.md5
                    current_remote = remote_clfiles[remote_name].remote
                    logging.debug('local_file.md5:' + str(local_md5) +
                                  ', remote_clfile.md5:' + str(remote_md5))
                    if local_md5 != remote_md5:
                        logging.debug('file has changed')
                        local_clfile.remote_path = remote_path
                        local_clfile.remote = current_remote
                        op = operation.Operation(operation.Operation.UPDATE,
                                                 local_clfile, remote_clfile)
                        operations.append(op)
                else:
                    logging.error('compare_method: ' + self._compare_method + ' not valid!')
                    raise Exception('compare_method: ' + self._compare_method + ' not valid!')

        if delete_file is True:
            reverse_keys = common.sort_dict_keys(remote_clfiles, True)
            for remote_path in reverse_keys:
                remote_clfile = remote_clfiles[remote_path]
                logging.debug('checking file ' + remote_path + ' for deletion')
                if remote_path not in local_remote_keys:
                    logging.debug('file ' + remote_path + ' has been deleted')
                    remote_clfile.remote_path = os.path.dirname(remote_path).replace('\\', '/')
                    op = operation.Operation(operation.Operation.REMOVE,
                                             remote_clfile, None)
                    operations.append(op)
        common.print_line('found ' + str(len(operations)) + ' differences')
        return operations

    def ls_matching_local_files(
            self,
            local_dir,
            local_clfiles,
            remote_root=None,
            remotes=None,
            normalize_path=True,
            source_is_remote=False):
        if remote_root is None:
            remote_root = self.get_backup_remote_root(local_dir)
        wanted_by_parent = {}
        for local_path in local_clfiles:
            local_clfile = local_clfiles[local_path]
            if local_clfile.is_dir:
                continue
            remote_key = self.remote_key_for_source_path(
                local_dir,
                local_clfile.path + '/' + local_clfile.name,
                remote_root,
                source_is_remote,
            )
            remote_parent = os.path.dirname(remote_key).replace('\\', '/')
            wanted_by_parent.setdefault(remote_parent, set()).add(remote_key)

        remote_clfiles = {}
        for remote_parent in sorted(wanted_by_parent):
            parent_files = self.ls_shallow(remote_parent, remotes=remotes, normalize_path=normalize_path)
            for remote_key in wanted_by_parent[remote_parent]:
                if remote_key in parent_files:
                    remote_clfiles[remote_key] = parent_files[remote_key]
        return remote_clfiles

    def _reserve_backup_capacity(self, remote, size):
        """Reserve known free space before one of two concurrent transfers starts."""
        if self._distribution_type != 'mas':
            return True
        required = self._required_free_for_upload(size)
        free = self._known_free_for_remote(remote)
        if free is None:
            return False
        reserved = self._reserved_free.get(remote, 0)
        if free - reserved < required:
            return False
        self._reserved_free[remote] = reserved + required
        return True

    def _release_backup_capacity(self, remote, size):
        if self._distribution_type != 'mas':
            return
        required = self._required_free_for_upload(size)
        remaining = max(0, self._reserved_free.get(remote, 0) - required)
        if remaining:
            self._reserved_free[remote] = remaining
        else:
            self._reserved_free.pop(remote, None)

    def _copy_add_operation(self, op, target_remote, dry_run):
        """Copy one file with capacity reservation and quota-aware fallback.

        Calls are made by at most two directory-batch workers.  Reserving
        capacity prevents both workers from selecting the same last free bytes.
        """
        candidates = None
        if target_remote is None:
            with self._backup_transfer_lock:
                candidates = self.get_eligible_remotes(int(op.src.size))
                exhausted = getattr(self, '_run_quota_exhausted_remotes', set())
                candidates = [remote for remote in candidates if remote not in exhausted]
        else:
            # An explicitly selected target has no fallback.  Do not keep
            # issuing one failing rclone request per file after Drive has
            # already confirmed that this target is full.
            if target_remote in getattr(self, '_run_quota_exhausted_remotes', set()):
                return False
            candidates = [target_remote]
        if not candidates:
            raise Exception('no remote has enough known free space')
        if dry_run:
            common.print_line('backing up file ' + op.src.path + '/' + op.src.name +
                              ' -> ' + candidates[0] + op.src.remote_path)
            return

        errors = []
        for remote in candidates:
            reserved = False
            if target_remote is None:
                with self._backup_transfer_lock:
                    if remote in getattr(self, '_run_quota_exhausted_remotes', set()):
                        continue
                    if remote in getattr(self, '_run_unavailable_remotes', {}):
                        continue
                    reserved = self._reserve_backup_capacity(remote, int(op.src.size))
                if not reserved:
                    continue
            try:
                if not self._show_progress:
                    common.print_line('backing up file ' + op.src.path + '/' + op.src.name +
                                      ' -> ' + remote + op.src.remote_path)
                self._ensure_target_directory(remote, op.src.remote_path)
                self.copy(op.src.path + '/' + op.src.name, op.src.remote_path, remote)
            except Exception as exc:
                errors.append(exc)
                with self._backup_transfer_lock:
                    if reserved:
                        self._release_backup_capacity(remote, int(op.src.size))
                    if self._is_storage_quota_exceeded(exc):
                        self.mark_remote_quota_exhausted(remote)
                        logging.warning('copy to %s failed: storage quota exceeded; marking remote full', remote)
                    elif target_remote is None:
                        self.mark_remote_unavailable_for_run(remote, exc)
                continue
            with self._backup_transfer_lock:
                if reserved:
                    self._release_backup_capacity(remote, int(op.src.size))
                if target_remote is None:
                    self._record_confirmed_transfer(
                        remote, op.src.remote_path, op.src.name, 0, op.src.size
                    )
            return
        raise Exception('; '.join(str(error) for error in errors) or
                        'no remote retained enough free space for this directory batch')

    def _directory_add_batches(self, operations):
        """Keep folder contents together, splitting large folders into file batches."""
        batches = {}
        for op in operations:
            if op.operation != operation.Operation.ADD or op.src.is_dir:
                continue
            batches.setdefault((op.src.path, op.src.remote_path), []).append(op)
        result = []
        for key in sorted(batches):
            files = batches[key]
            for index in range(0, len(files), BACKUP_DIRECTORY_BATCH_MAX_FILES):
                result.append(files[index:index + BACKUP_DIRECTORY_BATCH_MAX_FILES])
        return result

    def backup(self, local_dir, delete_files=True, dry_run=False, target=None):
        logging.debug('backing up directory ' + local_dir)
        source_remote, source_path = self.parse_backup_target(local_dir)
        source_is_remote = source_remote is not None
        source_is_local_file = not source_is_remote and os.path.isfile(local_dir)
        if not source_is_remote and getattr(getattr(self, '_rclone', None), '_rc_url', None) is not None:
            rc_local_remote = self._config.get('rclone_rc_local_remote')
            if rc_local_remote in (None, ''):
                raise Exception(
                    'rclone_rc_local_remote is required for a local-path backup through rclone RC'
                )
            source_remote = str(rc_local_remote).rstrip(':') + ':'
            source_path = local_dir
            source_is_remote = True
        if source_is_remote and self._compare_method == 'md5':
            raise Exception("rclone remote source backup supports compare_method=size")
        if not source_is_remote and not common.is_dir(local_dir) and not source_is_local_file:
            logging.error("local source " + local_dir + " not found. Cannot continue!")
            raise Exception("Local source " + local_dir + " not found")
        target_remote, target_path = self.parse_backup_target(target)
        if target_path is None:
            if source_is_remote:
                remote_root = self.get_backup_remote_root_for_remote_source(source_remote, source_path)
            elif source_is_local_file:
                remote_root = ''
            else:
                remote_root = self.get_backup_remote_root(local_dir)
        elif target_remote is not None and target_path == '' and not source_is_remote and not source_is_local_file:
            remote_root = '/' + os.path.basename(os.path.abspath(local_dir))
        else:
            remote_root = target_path
        logging.debug('backup remote root: ' + remote_root)
        if source_is_remote:
            local_clfiles = self.index_remote_dir(source_remote, source_path, self.__exclusion_list)
            source_root = source_remote + source_path
        elif source_is_local_file:
            local_clfiles = self.index_local_file(local_dir, self.__exclusion_list)
            source_root = os.path.dirname(os.path.abspath(local_dir))
        else:
            local_clfiles = self.index_local_dir(local_dir, self.__exclusion_list)
            source_root = local_dir
        target_remotes = [target_remote] if target_remote is not None else None
        normalize_remote_path = target_remote is None
        effective_delete_files = delete_files and not source_is_local_file
        if effective_delete_files is True or self._compare_method == 'md5':
            remote_clfiles = self.ls(remote_root, remotes=target_remotes, normalize_path=normalize_remote_path)
        else:
            remote_clfiles = self.ls_matching_local_files(
                source_root,
                local_clfiles,
                remote_root,
                target_remotes,
                normalize_remote_path,
                source_is_remote,
            )
        ops = self.compare_clfiles_for_remote_root(
            source_root,
            local_clfiles,
            remote_clfiles,
            effective_delete_files,
            remote_root,
            source_is_remote,
        )
        if self._show_progress:
            bar = Bar('Progress', max=len(ops), suffix='%(index)d/%(max)d %(percent)d%% [%(elapsed_td)s/%(eta_td)s]')
        if dry_run is True:
            common.print_line('performing a dry run. no changes are committed')
            add_batches = self._directory_add_batches(ops)
            common.print_line(
                'dry-run plan: {} operation(s), {} directory batch(es), {} transfer worker(s)'.format(
                    len(ops), len(add_batches), BACKUP_TRANSFER_WORKERS
                )
            )
            if self._show_progress:
                bar.finish()
            # Quota selection is intentionally skipped: a dry run never
            # transfers data, and checking every file made large plans slow.
            return
        failures = []
        failure_lock = threading.Lock()

        def record_failure(op, error, remotes=None):
            error_text = re.sub(
                r'(?i)(password|secret|token)=\S+',
                r'\1=<redacted>',
                str(error).replace('\\n', ' ').replace('\\r', ' '),
            )[:300]
            path = op.src.path + '/' + op.src.name
            detail = op.operation + ' ' + path
            if remotes:
                detail += ' [' + ', '.join(remotes) + ']'
            detail += ': ' + error_text
            with failure_lock:
                failures.append(detail)
            logging.error('backup operation failed: ' + detail)

        parallel_add_ids = set()
        worker_count = getattr(self, '_backup_transfer_workers', 1)
        if (not dry_run and not self._show_progress and worker_count == BACKUP_TRANSFER_WORKERS):
            directory_batches = self._directory_add_batches(ops)
            if directory_batches:
                parallel_add_ids = set(id(op) for batch in directory_batches for op in batch)

                def copy_batch(batch):
                    for add_op in batch:
                        try:
                            self._copy_add_operation(add_op, target_remote, dry_run)
                        except Exception as exc:
                            record_failure(add_op, exc)

                with ThreadPoolExecutor(max_workers=BACKUP_TRANSFER_WORKERS) as executor:
                    futures = [executor.submit(copy_batch, batch) for batch in directory_batches]
                    for future in futures:
                        future.result()

        for op in ops:
            logging.debug('operation: ' + op.operation + ", path: " + op.src.path)
            if self._show_progress:
                bar_title = op.src.name.ljust(25, '.')
                if len(bar_title) > 25:
                    bar_title = bar_title[0:25]
                # progress formats ``message`` with %-interpolation on every update.
                bar.message = 'file:' + bar_title.replace('%', '%%')
            if op.src.is_dir and op.operation != operation.Operation.REMOVE:
                logging.debug('skipping directory ' + op.src.path)
            elif id(op) in parallel_add_ids:
                # The directory-batch workers already processed this ADD.
                pass
            else:
                if op.operation == operation.Operation.ADD:
                    candidates = None
                    try:
                        if (target_remote is not None and
                                target_remote in getattr(self, '_run_quota_exhausted_remotes', set())):
                            # The first quota failure is already recorded.  An
                            # explicit target cannot fall back to another
                            # remote, so later ADDs must not retry it.
                            continue
                        if target_remote is None:
                            candidates = self.get_eligible_remotes(int(op.src.size))
                            exhausted = getattr(self, '_run_quota_exhausted_remotes', set())
                            candidates = [remote for remote in candidates if remote not in exhausted]
                        else:
                            candidates = [target_remote]
                        if not candidates:
                            raise Exception('no remote has enough known free space')
                        if dry_run is True:
                            candidates = candidates[:1]
                        copied = False
                        errors = []
                        for remote in candidates:
                            logging.debug('trying remote: ' + remote)
                            if not self._show_progress:
                                common.print_line('backing up file ' + op.src.path + '/' + op.src.name +
                                                  ' -> ' + remote + op.src.remote_path)
                            if dry_run is True:
                                copied = True
                                break
                            try:
                                self._ensure_target_directory(remote, op.src.remote_path)
                                self.copy(op.src.path + '/' + op.src.name, op.src.remote_path, remote)
                            except Exception as e:
                                errors.append(e)
                                if self._is_storage_quota_exceeded(e):
                                    self.mark_remote_quota_exhausted(remote)
                                    logging.warning('copy to ' + remote + ' failed: storage quota exceeded; marking remote full')
                                else:
                                    if target_remote is None:
                                        self.mark_remote_unavailable_for_run(remote, e)
                                    logging.warning('copy to ' + remote + ' failed: ' + str(e))
                                continue
                            if target_remote is None:
                                self._record_confirmed_transfer(
                                    remote, op.src.remote_path, op.src.name, 0, op.src.size
                                )
                            copied = True
                            break
                        if not copied:
                            raise Exception('; '.join(str(error) for error in errors))
                    except Exception as e:
                        record_failure(op, e, candidates)
                elif op.operation == operation.Operation.UPDATE:
                    try:
                        if target_remote is None:
                            self.ensure_remote_has_enough_space(op.src.remote, int(op.src.size))
                        if not self._show_progress:
                            common.print_line('backing up file ' + op.src.path + '/' + op.src.name +
                                              ' -> ' + op.src.remote + ':' + op.src.remote_path)
                        if dry_run is False:
                            self._ensure_target_directory(op.src.remote, op.src.remote_path)
                            self.copy(op.src.path + '/' + op.src.name, op.src.remote_path, op.src.remote)
                            if target_remote is None:
                                previous_size = 0 if op.dst is None or op.dst.size is None else op.dst.size
                                self._record_confirmed_transfer(
                                    op.src.remote,
                                    op.src.remote_path,
                                    op.src.name,
                                    previous_size,
                                    op.src.size,
                                )
                    except Exception as e:
                        if self._is_storage_quota_exceeded(e):
                            self.mark_remote_quota_exhausted(op.src.remote)
                        record_failure(op, e, [op.src.remote])
                elif op.operation == operation.Operation.REMOVE and delete_files is True:
                    try:
                        if not self._show_progress:
                            common.print_line('removing ' + op.src.remote + op.src.path)
                        if dry_run is False:
                            if op.src.is_dir:
                                self.rmdir(op.src.path, op.src.remote)
                            else:
                                self.delete_file(op.src.path, op.src.remote)
                                if target_remote is None:
                                    self.mark_remote_used(op.src.remote, -int(op.src.size or 0))
                    except Exception as e:
                        record_failure(op, e, [op.src.remote])
            if self._show_progress:
                bar.next()
        if self._show_progress:
            bar.finish()
        if failures:
            raise Exception(
                'backup completed with ' + str(len(failures)) + ' failed operation(s): ' +
                ' | '.join(failures)
            )

    def parse_backup_target(self, target):
        if target in (None, ''):
            return None, None
        target = target.replace('\\', '/')
        if ':' in target and not target.startswith('/'):
            remote, path = target.split(':', 1)
            if remote == '':
                raise Exception("invalid backup target " + target)
            return remote + ':', path
        path = '/' + target.strip('/')
        return None, path

    def get_backup_remote_root_for_remote_source(self, remote, path):
        path = (path or '').replace('\\', '/').strip('/')
        if path == '':
            return '/' + remote.rstrip(':')
        return '/' + os.path.basename(path)

    def get_backup_remote_root(self, local_dir):
        abs_local_dir = os.path.realpath(local_dir).replace('\\', '/')
        abs_cwd = os.path.realpath(os.getcwd()).replace('\\', '/')
        rel_path = os.path.relpath(abs_local_dir, abs_cwd).replace('\\', '/')
        if rel_path == '.':
            rel_path = os.path.basename(abs_local_dir)
        if rel_path.startswith('../') or rel_path == '..' or os.path.isabs(rel_path):
            rel_path = os.path.basename(abs_local_dir)
        return '/' + rel_path.strip('/')

    def remote_key_for_local_path(self, local_dir, path, remote_root=None):
        if remote_root is None:
            remote_root = self.get_backup_remote_root(local_dir)
        rel_path = os.path.relpath(os.path.realpath(path), os.path.realpath(local_dir)).replace('\\', '/')
        if rel_path == '.':
            return remote_root
        return common.normalize_path(remote_root.rstrip('/') + '/' + rel_path)

    def remote_key_for_source_path(self, source_root, path, remote_root=None, source_is_remote=False):
        if not source_is_remote:
            return self.remote_key_for_local_path(source_root, path, remote_root)
        if remote_root is None:
            remote, remote_path = self.parse_backup_target(source_root)
            remote_root = self.get_backup_remote_root_for_remote_source(remote, remote_path)
        source_root = source_root.replace('\\', '/').rstrip('/')
        path = path.replace('\\', '/')
        if path == source_root:
            return remote_root
        if source_root != '' and path.startswith(source_root + '/'):
            rel_path = path[len(source_root) + 1:]
        else:
            rel_path = os.path.basename(path)
        if rel_path == '.':
            return remote_root
        return common.normalize_path(remote_root.rstrip('/') + '/' + rel_path)

    def restore_old(self, remote_path, local_dir):
        logging.debug('restoring directory ' + local_dir + ' from ' + remote_path)
        if not common.is_dir(local_dir):
            #logging.error('directory ' + local_dir + ' not found')
            common.print_line('destination directory ' + local_dir + ' not found!')
            return
            #raise Exception('directory ' + local_dir + ' not found')
        remote_clfiles = self.ls(remote_path)
        for remote_clfile in remote_clfiles:
            remote = remote_clfiles[remote_clfile].remote
            path = remote_clfiles[remote_clfile].path
            common.print_line('restoring file ' + remote+os.path.dirname(path) + ' -> ' + local_dir)
            logging.debug('restoring file ' + os.path.dirname(path) + ' from remote '
                          + remote)
            self.copy_new(remote+os.path.dirname(path), local_dir)


    def restore(self, remote_path, local_dir, dry_run=False):
        logging.debug('restoring directory ' + local_dir + ' from ' + remote_path)
        if not common.is_dir(local_dir):
            #logging.error('directory ' + local_dir + ' not found')
            common.print_line('destination directory ' + local_dir + ' not found!')
            return
            #raise Exception('directory ' + local_dir + ' not found')
        for remote in self.get_remotes():
            common.print_line('restoring file ' + remote+remote_path + ' -> ' + local_dir)
            logging.debug('restoring file ' + remote+remote_path + ' -> ' + local_dir)
            if dry_run is False:
                self.copy_new(remote+remote_path, local_dir, True)


    def rmdir(self, directory, remote):
        logging.debug('removing directory ' + remote+directory)
        self._rclone.rmdir(remote, directory)
        if self._sa_registry is not None:
            self._sa_registry.invalidate_ls_cache_for_remote(remote)
        self._clear_memory_ls_cache()

    def get_version(self):
        logging.debug('getting version')

    def touch(self, file):
        logging.debug('touching file ' + file)

    def delete_file(self, file, remote):
        logging.debug('deleting file ' + remote+file)
        self._rclone.delete_file(remote, file)
        if self._sa_registry is not None:
            self._sa_registry.invalidate_ls_cache_for_remote(remote)
        self._clear_memory_ls_cache()

    def delete(self, path, remote):
        logging.debug('deleting path ' + remote+path)
        self._rclone.delete(remote, path)
        if self._sa_registry is not None:
            self._sa_registry.invalidate_ls_cache_for_remote(remote)
        self._clear_memory_ls_cache()

    def copy(self, src, dst, remote):
        logging.debug('copy ' + src + ' to ' + remote + dst)
        if self._rclone_move:
            self._rclone.move(src, remote+dst)
        else:
            self._rclone.copy(src, remote+dst)

    def copy_new(self, src, dst, no_error=False):
        logging.debug('copy ' + src + ' to ' + dst)
        if self._rclone_move:
            self._rclone.move(src, dst)
        else:
            self._rclone.copy(src, dst, [], no_error)

    def move(self, src, dst):
        logging.debug('move ' + src + ' to ' + dst)

    def _clear_memory_ls_cache(self):
        self._cache = {}
        self._cache_counter = {}

    def sync(self, path):
        logging.debug('synchronize path ' + path)

    def remove_duplicates(self, path, report_only=False):
        files = self.ls(path, True)
        common.print_line('analyzing for duplications...')
        keys = common.sort_dict_keys(files)
        duplicates = []
        for key in keys:
            if key.endswith(ClSync.duplicate_suffix):
                logging.debug('found duplicate file: ' + key)
                date1 = common.get_datetime_from_iso8601(files[key].mod_time)
                logging.debug(key + ' timestamp: ' + str(date1.timestamp()))
                key2 = key.replace(ClSync.duplicate_suffix, '')
                date2 = common.get_datetime_from_iso8601(files[key2].mod_time)
                logging.debug(key2 + ' timestamp: ' + str(date2.timestamp()))
                if date1.timestamp() > date2.timestamp():
                    logging.debug(key + ' is newer than ' + key2)
                    file_to_remove = files[key2].remote + key2
                    common.print_line('found duplicate file. Removing: ' + file_to_remove + '...')
                    duplicates.append(key2)
                    if report_only is False:
                        self.delete_file(key2, files[key2].remote)
                elif date1.timestamp() == date1.timestamp():
                    logging.debug(key + ' is equal to ' + key2)
                    file_to_remove = files[key2].remote + key2
                    common.print_line('found duplicate file. Removing: ' + file_to_remove + '...')
                    duplicates.append(key2)
                    if report_only is False:
                        self.delete_file(key2, files[key2].remote)
                else:
                    logging.debug(key + ' is older than ' + key2)
                    file_to_remove = files[key].remote + key
                    common.print_line('found duplicate file. Removing: ' + file_to_remove + '...')
                    duplicates.append(key)
                    if report_only is False:
                        self.delete_file(key, files[key].remote)
                logging.debug('file to remove: ' + file_to_remove)
        return duplicates

    def find(self, regex):
        logging.debug('finding files with regular expression ' + regex)
        return self.ls('/', with_dups=False, regex=regex)
