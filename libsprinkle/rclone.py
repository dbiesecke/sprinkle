#!/usr/bin/env python3
"""
rclone wrapper module
"""
__author__ = "Michael Montuori [michael.montuori@gmail.com]"
__copyright__ = "Copyright 2017 Michael Montuori. All rights reserved."
__credits__ = ["Warren Crigger"]
__license__ = "GPLv3"
__version__ = "1.0"
__revision__ = "2"

import logging
import json
import os
import random
from libsprinkle import common
from libsprinkle import exceptions


def generate_rclone_config(
        json_dir,
        output_file,
        root_folder_id,
        max_accounts=None,
        prefix="dst",
        start_index=101,
        return_entries=False,
        shuffle=True):
    """Generate an rclone configuration from service account files.

    The function scans ``json_dir`` for ``.json`` files and writes
    configuration sections for each file. Sections are named using
    ``prefix`` followed by a counter starting at ``start_index``.

    Parameters
    ----------
    json_dir: str
        Directory containing the service account ``.json`` files.
    output_file: str
        Path where the generated configuration should be written.
    root_folder_id: str
        Google Drive root folder ID for each remote.
    max_accounts: int, optional
        Limit the number of JSON files processed.  When ``None`` all
        files are used.
    prefix: str, optional
        Prefix for the remote names.  Defaults to ``dst``.
    start_index: int, optional
        Starting index for remote names.  Defaults to ``101``.

    Returns
    -------
    str
        The generated configuration content.
    """
    if not os.path.isdir(json_dir):
        raise ValueError("Directory {} not found".format(json_dir))

    files = [
        os.path.join(os.path.abspath(json_dir), f)
        for f in os.listdir(json_dir)
        if f.endswith(".json")
    ]
    return generate_rclone_config_from_files(
        files,
        output_file,
        root_folder_id,
        max_accounts=max_accounts,
        prefix=prefix,
        start_index=start_index,
        return_entries=return_entries,
        shuffle=shuffle,
    )


def generate_rclone_config_from_files(
        json_files,
        output_file,
        root_folder_id=None,
        max_accounts=None,
        prefix="dst",
        start_index=101,
        return_entries=False,
        shuffle=True):
    """Generate an rclone configuration from explicit service account files."""
    files = [os.path.abspath(path) for path in json_files]
    if shuffle:
        files = random.sample(files, len(files))
    else:
        files = sorted(files)
    if max_accounts is not None:
        files = files[:max_accounts]

    count = start_index - 1
    lines = []
    entries = []
    for filename in files:
        count += 1
        remote = "{}{}".format(prefix, count)
        lines.extend([
            "[{}]".format(remote),
            "type = drive",
            "scope = drive",
            "service_account_file = {}".format(filename),
        ])
        if root_folder_id:
            lines.append("root_folder_id = {}".format(root_folder_id))
        lines.append("")
        entries.append({"remote": remote, "path": filename})

    config_text = "\n".join(lines)
    with open(output_file, "w") as conf_fp:
        conf_fp.write(config_text)
    if return_entries:
        return config_text, entries
    return config_text


def generate_rclone_combine_config(upstreams, output_file, group_size=50, prefix="sa_group"):
    """Generate optional rclone combine remotes from upstream specs."""
    if group_size < 1:
        raise ValueError("group_size must be at least 1")
    specs = []
    for upstream in upstreams:
        if isinstance(upstream, dict):
            remote = upstream["remote"].rstrip(":")
            specs.append("{}={}:".format(remote, remote))
        else:
            specs.append(str(upstream))
    lines = []
    group = 1
    for index in range(0, len(specs), group_size):
        lines.extend([
            "[{}{}]".format(prefix, group),
            "type = combine",
            "upstreams = {}".format(" ".join(specs[index:index + group_size])),
            "",
        ])
        group += 1
    config_text = "\n".join(lines)
    with open(output_file, "w") as conf_fp:
        conf_fp.write(config_text)
    return config_text

class RClone:

    def __init__(self, config_file=None, rclone_exe="rclone", rclone_retries="1"):
        logging.debug('constructing RClone')
        if config_file is not None and not common.is_file(config_file):
            logging.error("configuration file " + str(config_file) + " not found. Cannot continue!")
            raise Exception("Configuration file " + str(config_file) + " not found")
        if rclone_exe != "rclone" and not common.is_file(rclone_exe):
            #logging.error("rclone executable " + str(rclone_exe) + " not found. Cannot continue!")
            common.print_line('RCLONE.EXE not in PATH. Put it in PATH or modify libsprinkle.conf to point to it.')
            raise Exception("rclone executable " + str(rclone_exe) + " not found")
        self._config_file = config_file
        self._rclone_exe = rclone_exe
        self._rclone_retries = rclone_retries

    def get_remotes(self, extra_args=[]):
        logging.debug('listing remotes')
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("listremotes")
        for extra_arg in extra_args:
            command_with_args.append(extra_arg)
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--auto-confirm")
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['code'] == -20:
            logging.error("rclone executable not found. Please make sure it's in the PATH or in the config file")
            raise Exception("rclone executable not found. Please make sure it's in the PATH or in the config file")
        if result['error'] != '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        remotes = result['out'].splitlines()
        return remotes

    def lsjson(self, remote, directory, extra_args=[], no_error=False):
        logging.debug('running lsjson for ' + remote + directory)
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("lsjson")
        for extra_arg in extra_args:
            command_with_args.append(extra_arg)
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(remote + directory)
        result = common.execute(command_with_args, no_error)
        logging.debug('result: ' + str(result)[0:128])
        if result['error'] != '':
            if no_error is False:
                # logging.error('error getting remotes objects')
                if result['error'].find("directory not found") != -1:
                    raise exceptions.FileNotFoundException(result['error'])
                else:
                    raise Exception('error getting remote object. ' + result['error'])
        if 'out' in result and result['out'] == '[\n':
            lsjson = '[]'
        else:
            lsjson = result['out']
        logging.debug('returning ' + str(lsjson)[0:128])
        return lsjson

    def md5sum(self, remote, directory, extra_args=[], no_error=False):
        logging.debug('running lsjson for ' + remote + directory)
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("md5sum")
        for extra_arg in extra_args:
            command_with_args.append(extra_arg)
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--auto-confirm")
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(remote+directory)
        result = common.execute(command_with_args, no_error)
        logging.debug('result: ' + str(result)[0:128])
        if result['error'] != '':
            if no_error is False:
                #logging.error('error getting remotes objects')
                if result['error'].find("directory not found") != -1:
                    raise exceptions.FileNotFoundException(result['error'])
                else:
                    raise Exception('error getting remote object. ' + result['error'])
        if 'out' in result and result['out'] == '[\n':
            lsjson = '[]'
        else:
            lsjson = result['out']
        logging.debug('returning ' + str(lsjson)[0:128])
        return lsjson

    def get_about_json_with_error(self, remote):
        logging.debug('running about for ' + remote)
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("about")
        command_with_args.append("--json")
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--auto-confirm")
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(remote)
        result = common.execute(command_with_args, True)
        logging.debug('result: ' + str(result))
        if result.get('code', 0) != 0 or result.get('error') not in (None, ''):
            error = result.get('error') or result.get('out') or 'rclone about failed'
            return None, str(error)
        if result.get('out') in (None, ''):
            return None, 'rclone about returned empty output'
        try:
            return json.loads(result['out']), None
        except Exception as exc:
            return None, 'rclone about returned invalid json: {}'.format(exc.__class__.__name__)

    def get_about_json(self, remote, no_error=False):
        quota, error = self.get_about_json_with_error(remote)
        if error is not None:
            logging.error('error getting remote quota')
            if no_error:
                return None
            raise Exception('error getting remote quota. ' + error)
        return quota

    def get_about(self, remote):
        aboutjson = self.get_about_json(remote)
        if aboutjson is None:
            return []
        logging.debug('returning ' + str(aboutjson))
        return json.dumps(aboutjson).splitlines()

    def mkdir(self, remote, directory):
        logging.debug('running mkdir for ' + remote + ":" + directory)
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("mkdir")
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--auto-confirm")
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(remote + directory)
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] != '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        out = result['out'].splitlines()
        logging.debug('returning ' + str(out))
        return out

    def rmdir(self, remote, directory):
        logging.debug('running rmdir for ' + remote + ":" + directory)
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("rmdir")
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--auto-confirm")
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(remote + directory)
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] != '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        out = result['out'].splitlines()
        logging.debug('returning ' + str(out))
        return out

    def get_version(self):
        logging.debug('running version')
        command_with_args = [self._rclone_exe, "version"]
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] != '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        out = result['out'].splitlines()
        logging.debug('returning ' + str(out))
        return out

    def touch(self, remote, file):
        logging.debug('running touch for ' + remote + ":" + file)
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("touch")
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--auto-confirm")
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(remote + file)
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] != '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        out = result['out'].splitlines()
        logging.debug('returning ' + str(out))
        return out

    def delete_file(self, remote, file):
        logging.debug('running deleteFile for ' + remote + ":" + file)
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("deletefile")
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--auto-confirm")
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(remote + file)
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] != '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        out = result['out'].splitlines()
        logging.debug('returning ' + str(out))
        return out

    def delete(self, remote, file):
        logging.debug('running delete for ' + remote + ":" + file)
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("delete")
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--auto-confirm")
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(remote + file)
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] != '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        out = result['out'].splitlines()
        logging.debug('returning ' + str(out))
        return out

    def copy(self, src, dst, extra_args=[], no_error=False):
        logging.debug('running copy from ' + src + " to " + dst)
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("copy")
        for extra_arg in extra_args:
            command_with_args.append(extra_arg)
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--auto-confirm")
        command_with_args.append("--local-no-check-updated")
        command_with_args.append("--min-age")
        command_with_args.append("6h")
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(src)
        command_with_args.append(dst)
        logging.debug('command args: ' + str(command_with_args))
        result = common.execute(command_with_args, no_error)
        logging.debug('result: ' + str(result))
        if result['error'] != '':
            if no_error is False:
                logging.error('error getting remotes objects')
                raise Exception('error getting remote object. ' + result['error'])
        out = result['out'].splitlines()
        logging.debug('returning ' + str(out))
        return out

    def move(self, src, dst, extra_args=[]):
        logging.debug('running move from ' + src + " to " + dst)
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("move")
        for extra_arg in extra_args:
            command_with_args.append(extra_arg)
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--auto-confirm")
        command_with_args.append("--local-no-check-updated")
        command_with_args.append("--min-age")
        command_with_args.append("6h")
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(src)
        command_with_args.append(dst)
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] != '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        out = result['out'].splitlines()
        logging.debug('returning ' + str(out))
        return out

    def get_free(self, remote):
        json_obj = self.get_about_json(remote, True)
        if json_obj is not None and "free" in json_obj:
            logging.debug('free ' + str(json_obj['free']))
            return json_obj['free']
        return None

    def get_size(self, remote):
        json_obj = self.get_about_json(remote, True)
        if json_obj is not None and "total" in json_obj:
            logging.debug('total ' + str(json_obj['total']))
            return json_obj['total']
        return None

    def sync(self, src, dst, extra_args=[]):
        logging.debug('running sync from ' + src + " to " + dst)
        command_with_args = []
        command_with_args.append(self._rclone_exe)
        command_with_args.append("sync")
        for extra_arg in extra_args:
            command_with_args.append(extra_arg)
        if self._config_file is not None:
            command_with_args.append("--config")
            command_with_args.append(self._config_file)
        command_with_args.append("--auto-confirm")
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] != '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        out = result['out'].splitlines()
        logging.debug('returning ' + str(out))
        return out
