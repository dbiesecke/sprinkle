#!/usr/bin/env python3
"""
clsync module
"""
__author__ = "Michael Montuori [michael.montuori@gmail.com]"
__copyright__ = "Copyright 2017 Michael Montuori. All rights reserved."
__credits__ = ["Warren Crigger"]
__license__ = "GPLv3"
__version__ = "1.1"
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

DEFAULT_LARGE_FILE_THRESHOLD_BYTES = 1024 * 1024 * 1024
DEFAULT_LARGE_FILE_MIN_FREE_BYTES = 512 * 1024 * 1024
DEFAULT_LARGE_FILE_MIN_FREE_PERCENT = 5

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
            self._rclone = rclone.RClone(rclone_config)
        else:
            self._rclone = rclone.RClone(
                rclone_config, self._config['rclone_exe'], self._rclone_retries
            )

        if 'rclone_move' in config:
            self._rclone_move = config['rclone_move']
        else:
            self._rclone_move = False

    def get_remotes(self):
        logging.debug('getting rclone remotes')
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

    def ls(self, file, with_dups=False, regex=None, stop_after_first=None):
        return self._ls(file, with_dups, regex, stop_after_first, recursive=True)

    def ls_shallow(self, file, with_dups=False, regex=None, stop_after_first=None):
        return self._ls(file, with_dups, regex, stop_after_first, recursive=False)

    def _ls(self, file, with_dups=False, regex=None, stop_after_first=None, recursive=True):
        logging.debug('lsjson of file: ' + file)
        if stop_after_first is None:
            stop_after_first=self._config['ls_stop_first']
        if not file.startswith('/'):
            logging.debug('adding / ' + file)
            file = '/' + file
        memory_cache_key = (file, with_dups, regex, stop_after_first, recursive)
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
            md5s = self.lsmd5(file, stop_after_first)
        for remote in self.get_remotes():
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
                tmp_file.id = tmp_json_file['ID']
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

    def lsmd5(self, file, stop_after_first=False):
        logging.debug('lsjson of file: ' + file)
        if not file.startswith('/'):
            file = '/' + file
        files = {}
        for remote in self.get_remotes():
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
        if self._distribution_type == 'mas':
            required_size = self._required_free_for_upload(requested_size)
            logging.debug(
                'selecting best remote with the most available space to store size: ' +
                str(requested_size) + ', required free: ' + str(required_size)
            )
            best_remote = None
            highest_size = 0
            size = 0
            for remote in self.get_remotes():
                size = self._known_free_for_remote(remote)
                logging.debug('free of ' + remote + ' is ' + str(size))
                if size is not None and size > highest_size:
                    if required_size <= size:
                        highest_size = size
                        best_remote = remote
            if best_remote is None:
                raise Exception(
                    'no remote has enough known free space for requested size ' +
                    str(requested_size) + ' with required free ' + str(required_size)
                )
            return best_remote
        else:
            logging.error('distribution mode ' + self._distribution_type + ' not supported.')
            raise Exception('unsupported distribution mode ' + self._distribution_type)

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
            self._cached_free[remote] = self._quota_value(quota, 'free')
        return self._cached_free[remote]

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
        if self._sa_registry is not None:
            self._sa_registry.adjust_quota_for_remote(remote, int(size))
            self._sa_registry.invalidate_ls_cache_for_remote(remote)
        self._clear_memory_ls_cache()

    def _get_remote_quota(self, remote):
        cached = None
        if self._sa_registry is not None:
            cached = self._sa_registry.quota_by_remote(remote)
            if cached is not None and not self._sa_registry.should_refresh(cached, self._sa_refresh):
                return self._quota_from_row(cached)
            if cached is not None and self._sa_refresh == 'none':
                return self._quota_from_row(cached)
        try:
            quota = self._rclone.get_about_json(remote, True)
        except Exception as e:
            logging.debug('error refreshing quota for ' + remote + ': ' + str(e))
            quota = None
        if self._sa_registry is not None and cached is not None:
            if quota is None:
                self._sa_registry.update_quota_for_remote(remote, None, 'rclone about failed')
                return self._quota_from_row(cached)
            self._sa_registry.update_quota_for_remote(remote, quota, None)
        return quota

    def _quota_from_row(self, row):
        if row is None:
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
            remote_root=None):
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
            remote_key = self.remote_key_for_local_path(local_dir, full_path, remote_root)
            local_remote_keys[remote_key] = local_clfile
        operations = []
        for local_path in local_clfiles:
            local_clfile = local_clfiles[local_path]
            if local_clfile.is_dir:
                continue
            logging.debug('checking local clfile: ' + local_path + " name: " + local_clfile.name)
            remote_name = self.remote_key_for_local_path(
                local_dir,
                local_clfile.path + '/' + local_clfile.name,
                remote_root,
            )
            remote_path = os.path.dirname(remote_name).replace('\\', '/')
            logging.debug('remote name: ' + remote_name)
            if remote_name not in remote_clfiles:
                logging.debug('not found in remote_clfiles')
                local_clfile.remote_path = remote_path
                op = operation.Operation(operation.Operation.ADD,
                                         local_clfile, None)
                operations.append(op)
            else:
                logging.debug('file found in remote_clfiles')
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
                                                 local_clfile, None)
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
                                                 local_clfile, None)
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

    def ls_matching_local_files(self, local_dir, local_clfiles, remote_root=None):
        if remote_root is None:
            remote_root = self.get_backup_remote_root(local_dir)
        wanted_by_parent = {}
        for local_path in local_clfiles:
            local_clfile = local_clfiles[local_path]
            if local_clfile.is_dir:
                continue
            remote_key = self.remote_key_for_local_path(
                local_dir,
                local_clfile.path + '/' + local_clfile.name,
                remote_root,
            )
            remote_parent = os.path.dirname(remote_key).replace('\\', '/')
            wanted_by_parent.setdefault(remote_parent, set()).add(remote_key)

        remote_clfiles = {}
        for remote_parent in sorted(wanted_by_parent):
            parent_files = self.ls_shallow(remote_parent)
            for remote_key in wanted_by_parent[remote_parent]:
                if remote_key in parent_files:
                    remote_clfiles[remote_key] = parent_files[remote_key]
        return remote_clfiles

    def backup(self, local_dir, delete_files=True, dry_run=False):
        logging.debug('backing up directory ' + local_dir)
        if not common.is_dir(local_dir):
            logging.error("local directory " + local_dir + " not found. Cannot continue!")
            raise Exception("Local directory " + local_dir + " not found")
        remote_root = self.get_backup_remote_root(local_dir)
        logging.debug('backup remote root: ' + remote_root)
        local_clfiles = self.index_local_dir(local_dir, self.__exclusion_list)
        if delete_files is True or self._compare_method == 'md5':
            remote_clfiles = self.ls(remote_root)
        else:
            remote_clfiles = self.ls_matching_local_files(local_dir, local_clfiles, remote_root)
        ops = self.compare_clfiles_for_remote_root(
            local_dir,
            local_clfiles,
            remote_clfiles,
            delete_files,
            remote_root,
        )
        if self._show_progress:
            bar = Bar('Progress', max=len(ops), suffix='%(index)d/%(max)d %(percent)d%% [%(elapsed_td)s/%(eta_td)s]')
        if dry_run is True:
            common.print_line('performing a dry run. no changes are committed')
        for op in ops:
            logging.debug('operation: ' + op.operation + ", path: " + op.src.path)
            if self._show_progress:
                bar_title = op.src.name.ljust(25, '.')
                if len(bar_title) > 25:
                    bar_title = bar_title[0:25]
                bar.message = 'file:' + bar_title
            if op.src.is_dir and op.operation != operation.Operation.REMOVE:
                logging.debug('skipping directory ' + op.src.path)
                continue
            if op.operation == operation.Operation.ADD:
                best_remote = self.get_best_remote(int(op.src.size))
                logging.debug('best remote: ' + best_remote)
                if not self._show_progress:
                    common.print_line('backing up file ' + op.src.path+'/'+op.src.name +
                                  ' -> ' + best_remote+':'+op.src.remote_path)
                if dry_run is False:
                    self.copy(op.src.path+'/'+op.src.name, op.src.remote_path, best_remote)
                    self.mark_remote_used(best_remote, int(op.src.size))
            if op.operation == operation.Operation.UPDATE:
                self.ensure_remote_has_enough_space(op.src.remote, int(op.src.size))
                if not self._show_progress:
                    common.print_line('backing up file ' + op.src.path + '/' + op.src.name +
                                  ' -> ' + op.src.remote + ':' + op.src.remote_path)
                if dry_run is False:
                    self.copy(op.src.path + '/' + op.src.name, op.src.remote_path, op.src.remote)
            if op.operation == operation.Operation.REMOVE and delete_files is True:
                if not self._show_progress:
                    common.print_line('removing ' + op.src.remote+op.src.path)
                if op.src.is_dir:
                    if dry_run is False:
                        try:
                            self.rmdir(op.src.path, op.src.remote)
                        except Exception as e:
                            logging.debug(str(e))
                else:
                    if dry_run is False:
                        self.delete_file(op.src.path, op.src.remote)
            if self._show_progress:
                bar.next()
        if self._show_progress:
            bar.finish()

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
