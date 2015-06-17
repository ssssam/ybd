#!/usr/bin/python
# Copyright (C) 2015 Codethink Limited
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.


# I'm a Python script for running YBD lots of times. I will only work
# if deployed by the Ansible script that accompanies me, probably.


import yaml

import glob
import itertools
import os
import shutil
import socket
import subprocess


BUILDER_NAME = 'http://%s' % socket.gethostname()


def create_artifacts_directory(prefix='artifacts'):
    '''Create a new directory to store output of a build.

    The directory will be named artifacts-0, unless that directory already
    exists, in which case it will be named artifacts-1, and so on.

    '''
    for i in itertools.count():
        path = '%s-%02i' % (prefix, i)
        if not os.path.exists(path):
            os.mkdir(path)
            return path


def set_ybd_config(in_def_file, out_def_file=None):
    out_def_file = out_def_file or in_def_file
    with open(in_def_file, 'r') as f:
        settings = yaml.safe_load(f.read())
    settings['artifacts'] = '/home/build/artifacts'
    settings['ccache_dir'] = '/home/build/ccache'
    settings['gits'] = '/home/build/gits'
    # This must be on the same partition as 'artifacts' dir, or staging
    # area construction will fail.
    settings['tmp'] = '/home/build/tmp'
    settings['deployment'] = '/home/build/tmp/deployments'
    # I don't think these do anything, but to be on the safe side...
    settings['base'] = '/home/build/'
    settings['caches'] = '/home/build/'
    with open(out_def_file, 'w') as f:
        yaml.dump(settings, f)


while True:
    ybd_dir = '/home/build/ybd'
    definitions_dir = '/home/build/definitions'

    # FIXME: might be good to autoupdate YBD as well as definitions.

    if not os.path.exists(definitions_dir):
        subprocess.check_call(
            ['git', 'clone',
             'git://git.baserock.org/baserock/baserock/definitions',
             definitions_dir])
    else:
        subprocess.check_call(
            ['git', 'pull', '--force', 'origin', 'HEAD'],
            cwd=definitions_dir)

    set_ybd_config(
        os.path.join(ybd_dir, 'ybd.def'),
        os.path.join(definitions_dir, 'ybd.def')
    )

    ybd = os.path.join(ybd_dir, 'ybd.py')
    subprocess.check_call(
        [ybd, 'systems/build-system-x86_64.morph'],
        cwd=definitions_dir)

    # After the build, old artifacts get put in a separate directory.

    old_artifacts_dir = create_artifacts_directory('/home/build/old-artifacts')

    artifacts = glob.glob('/home/build/artifacts/*')

    if len(artifacts) == 0:
        raise RuntimeError("Build didn't produce any artifacts.")

    for artifact in artifacts:
        if artifact.endwith('.unpacked'):
            shutil.rmtree(artifact)
        else:
            subprocess.check_call(
                ['/home/cache/morph-cache-server/scripts/submit-build',
                 '--host=localhost', '--builder-name=%s' % BUILDER_NAME,
                 artifact])

            shutil.move(artifact, old_artifacts_dir)
