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
        sample_size=None,
        prefix="dst",
        start_index=101):
    """Generate an rclone configuration from service account files.

    The function scans ``json_dir`` for ``.json`` files, shuffles them to
    provide a random selection and writes configuration sections for each
    file.  Sections are named using ``prefix`` followed by a counter
    starting at ``start_index``.

    Parameters
    ----------
    json_dir: str
        Directory containing the service account ``.json`` files.
    output_file: str
        Path where the generated configuration should be written.
    root_folder_id: str
        Google Drive root folder ID for each remote.
    sample_size: int, optional
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

    files = [f for f in os.listdir(json_dir) if f.endswith(".json")]
    random.shuffle(files)
    if sample_size is not None:
        files = files[:sample_size]

    count = start_index - 1
    lines = []
    for filename in files:
        count += 1
        lines.extend([
            "[{}{}]".format(prefix, count),
            "type = drive",
            "scope = drive",
            "service_account_file = {}".format(
                os.path.join(os.path.abspath(json_dir), filename)
            ),
            "root_folder_id = {}".format(root_folder_id),
            "",
        ])

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
        if rclone_exe is not "rclone" and not common.is_file(rclone_exe):
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
        if result['error'] is not '':
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
        if result['error'] is not '':
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
        if result['error'] is not '':
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

    def get_about(self, remote):
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
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] is not '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        aboutjson = result['out'].splitlines()
        logging.debug('returning ' + str(aboutjson))
        return aboutjson

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
        if result['error'] is not '':
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
        if result['error'] is not '':
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
        if result['error'] is not '':
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
        if result['error'] is not '':
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
        if result['error'] is not '':
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
        if result['error'] is not '':
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
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(src)
        command_with_args.append(dst)
        logging.debug('command args: ' + str(command_with_args))
        result = common.execute(command_with_args, no_error)
        logging.debug('result: ' + str(result))
        if result['error'] is not '':
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
        command_with_args.append("--retries")
        command_with_args.append(self._rclone_retries)
        command_with_args.append(src)
        command_with_args.append(dst)
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] is not '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        out = result['out'].splitlines()
        logging.debug('returning ' + str(out))
        return out

    def get_free(self, remote):
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
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] is not '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        aboutjson = result['out']
        json_obj = json.loads(aboutjson)
        # simple fix for accounts with errors
        if "free" in json_obj:
            logging.debug('free ' + str(json_obj['free']))
            return json_obj['free']
        else:
            return 1
#        return json_obj['free']


    def get_size(self, remote):
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
        result = common.execute(command_with_args)
        logging.debug('result: ' + str(result))
        if result['error'] is not '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        aboutjson = result['out']
        json_obj = json.loads(aboutjson)
        # simple fix for accounts with errors
        if "total" in json_obj:
            logging.debug('total ' + str(json_obj['total']))
            return json_obj['total']
        else:
            return 1
        #return json_obj['total']

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
        if result['error'] is not '':
            logging.error('error getting remotes objects')
            raise Exception('error getting remote object. ' + result['error'])
        out = result['out'].splitlines()
        logging.debug('returning ' + str(out))
        return out


