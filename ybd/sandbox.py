# Copyright (C) 2011-2016  Codethink Limited
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
#
# =*= License: GPL-2 =*=

import contextlib
import os
import pipes
import shutil
import stat
import tempfile
from subprocess import call, check_call, PIPE

import app
import cache
import utils
from repos import get_repo_url


def _aboriginal_start():
    # Assert configuration
    #
    if 'aboriginal-controller' not in app.config:
        app.exit('SETUP', 'ERROR: required configuration not specified:', 'aboriginal-controller')
    if 'aboriginal-system' not in app.config:
        app.exit('SETUP', 'ERROR: required configuration not specified:', 'aboriginal-system')

    #
    # Ensure we have a worker for this instance
    #
    app.log('SETUP', "Ensuring an Aboriginal build worker is running")
    workdir = os.path.join(app.config['workers'], ('worker-' + str(app.config.get('fork', 0))))
    aboriginal_start = os.path.join(app.config['aboriginal-controller'], 'aboriginal-start')
    check_call([ aboriginal_start,
                 '--silent',
                 '--cpus', str(app.config['max-jobs']),
                 '--emulator', app.config['aboriginal-system'],
                 '--workdir', workdir,
                 '--directory', app.config['tmp'] ])


@contextlib.contextmanager
def setup(this):
    currentdir = os.getcwd()
    tempfile.tempdir = app.config['tmp']
    this['sandbox'] = tempfile.mkdtemp()
    os.environ['TMPDIR'] = app.config['tmp']
    app.config['sandboxes'] += [this['sandbox']]
    this['build'] = os.path.join(this['sandbox'], this['name'] + '.build')
    this['install'] = os.path.join(this['sandbox'], this['name'] + '.inst')
    this['baserockdir'] = os.path.join(this['install'], 'baserock')
    this['tmp'] = os.path.join(this['sandbox'], 'tmp')
    for directory in ['build', 'install', 'tmp', 'baserockdir']:
        os.makedirs(this[directory])
    this['log'] = os.path.join(app.config['artifacts'],
                               this['cache'] + '.build-log')
    if app.config.get('instances'):
        this['log'] += '.' + str(app.config.get('fork', 0))
    assembly_dir = this['sandbox']
    for directory in ['dev', 'tmp']:
        call(['mkdir', '-p', os.path.join(assembly_dir, directory)])

    # Start up the aboriginal worker for this instance, this should
    # probably be done elsewhere but works here
    _aboriginal_start()

    try:
        yield
    finally:
        app.remove_dir(this['sandbox'])


def install(defs, this, component):
    # populate this['sandbox'] with the artifact files from component
    if os.path.exists(os.path.join(this['sandbox'], 'baserock',
                                   component['name'] + '.meta')):
        return
    if app.config.get('log-verbose'):
        app.log(this, 'Sandbox: installing %s' % component['cache'])
    if cache.get_cache(defs, component) is False:
        app.exit(this, 'ERROR: unable to get cache for', component['name'])
    unpackdir = cache.get_cache(defs, component) + '.unpacked'
    if this.get('kind') is 'system':
        utils.copy_all_files(unpackdir, this['sandbox'])
    else:
        utils.hardlink_all_files(unpackdir, this['sandbox'])


def ldconfig(this):
    conf = os.path.join(this['sandbox'], 'etc', 'ld.so.conf')
    if os.path.exists(conf):
        run_sandboxed (this, 'ldconfig')
    else:
        app.log(this, 'No %s, not running ldconfig' % conf)


def argv_to_string(argv):
    return ' '.join(map(pipes.quote, argv))


def run_sandboxed(this, command, env=None, allow_parallel=False):

    cur_makeflags=None
    if env is not None:
        cur_makeflags = env.get("MAKEFLAGS")
        if not allow_parallel:
            env.pop("MAKEFLAGS", None)

        app.log(this, 'Running command:\n%s' % command)
    with open(this['log'], "a") as logfile:
        logfile.write("# # %s\n" % command)

    # First serialize the build command into a ./build.sh script in the root
    # of the staging area at this['sandbox']
    #
    # Script should start with cd /this['name'].build
    aboriginal_control = os.path.join(app.config['aboriginal-controller'], 'aboriginal-controller')
    workdir = os.path.join(app.config['workers'], ('worker-' + str(app.config.get('fork', 0))))
    sandboxdir = os.path.basename(this['sandbox'].rstrip(os.path.sep))
    builddir = this['name'] + '.build';
    buildscript = os.path.join(this['sandbox'], 'build.sh')
    buildlog = os.path.join(this['sandbox'], 'build.log')

    with open(buildscript, "w") as script:
        script.write ('#!/bin/bash\n')
        script.write ('\n')

        if env is not None:
            script.write ('# Setup environment\n')
            for envvar, envval in env.iteritems():
                script.write ('export %s="%s"\n' % (envvar, envval))
            script.write ('\n')

        script.write ('# Switch to build directory\n')
        script.write ('cd %s\n' % builddir)
        script.write ('\n')

        script.write ('# Run commands\n')
        script.write ('%s\n' % command)

    st = os.stat(buildscript)
    os.chmod(buildscript, st.st_mode | stat.S_IEXEC)

    # Prepare arguments to launch the aboriginal build
    #
    launchArgs = [ aboriginal_control,
                   '--workdir', workdir,
                   '--directory', sandboxdir,
                   '--execute' ]

    if this.get('build-mode') == 'prelibc':
        launchArgs.append('--bootstrap')

    if app.config.get('log-verbose'):
        launchArgs.append('--verbose')

    # It's important to redirect stdin to /dev/null, otherwise some
    # build scripts trying to be smart think that there is a tty
    # and call things like tput to set bold face (looking at gnome-autogen.sh)
    launchArgs.append('./build.sh > build.log 2>&1 < /dev/null')

    # Tail the build log into the expected location while the build runs
    exit_code = call(launchArgs)

    # Append this build log to the main log file, it's possible
    try:
        with open(this['log'], 'a') as logfile, open(buildlog, 'r') as commandlog:
            logfile.write(commandlog.read())
    except:
        # We ran build.sh which asks for #!/bin/bash and directed to build.log,
        # even if there is no output this will create build.log, the only way
        # to reach this exception is if /bin/bash failed to run, this can happen.
        app.log(this, 'ERROR: Non functional shell while running command:\n\n', command)
        call(['tail', '-n', '200', this['log']])
        app.log(this, 'ERROR: log file is at', this['log'])
        app.exit(this, 'ERROR: sandbox debris is at', this['sandbox'])

    if exit_code != 0:
        app.log(this, 'ERROR: command failed in directory %s:\n\n' %
                os.getcwd(), command)
        call(['tail', '-n', '200', this['log']])
        app.log(this, 'ERROR: log file is at', this['log'])
        app.exit(this, 'ERROR: sandbox debris is at', this['sandbox'])

    if cur_makeflags is not None:
        env['MAKEFLAGS'] = cur_makeflags

    return exit_code


def run_extension(this, deployment, step, method):

    # XXX This needs to be fixed to run inside the sandbox,
    #     we need to first ensure we have built and staged all the
    #     host tooling, i.e. python, in the image in order to
    #     run the extensions, then we need to actually run the
    #     extensions in the sandbox.
    app.log(this, 'Running %s extension:' % step, method)
    extensions = utils.find_extensions()
    tempfile.tempdir = tmp = app.config['tmp']
    cmd_tmp = tempfile.NamedTemporaryFile(delete=False)
    cmd_bin = extensions[step][method]

    envlist = ['UPGRADE=yes'] if method == 'ssh-rsync' else ['UPGRADE=no']

    if 'PYTHONPATH' in os.environ:
        envlist.append('PYTHONPATH=%s:%s' % (os.environ['PYTHONPATH'],
                                             app.config['extsdir']))
    else:
        envlist.append('PYTHONPATH=%s' % app.config['extsdir'])

    for key, value in deployment.iteritems():
        if key.isupper():
            envlist.append("%s=%s" % (key, value))

    command = ["env"] + envlist + [cmd_tmp.name]

    if step in ('write', 'configure'):
        command.append(this['sandbox'])

    if step in ('write', 'check'):
        command.append(deployment.get('location') or
                       deployment.get('upgrade-location'))

    with app.chdir(app.config['defdir']):
        try:
            with open(cmd_bin, "r") as infh:
                shutil.copyfileobj(infh, cmd_tmp)
            cmd_tmp.close()
            os.chmod(cmd_tmp.name, 0o700)

            if call(command):
                app.log(this, 'ERROR: %s extension failed:' % step, cmd_bin)
                raise SystemExit
        finally:
            os.remove(cmd_tmp.name)
    return

def env_vars_for_build(defs, this):
    env = {}
    arch_dict = {
        'i686': "x86_32",
        'armv8l64': "aarch64",
        'armv8b64': "aarch64_be",
        'mips64b': 'mips64',
        'mips64l': 'mips64el',
        'mips32b': 'mips',
        'mips32l': 'mipsel',
    }

    env['DESTDIR'] = os.path.join('/', os.path.basename(this.get('install')))
    env['PREFIX'] = this.get('prefix') or '/usr'
    env['MAKEFLAGS'] = '-j%s' % (this.get('max-jobs') or
                                 app.config['max-jobs'])
    env['TERM'] = 'dumb'
    env['SHELL'] = '/bin/bash'
    env['USER'] = env['USERNAME'] = env['LOGNAME'] = 'tomjon'
    env['LC_ALL'] = 'C'
    env['HOME'] = '/tmp'
    env['TZ'] = 'UTC'

    arch = app.config['arch']
    cpu = arch_dict.get(arch, arch)
    abi = ''
    if arch.startswith(('armv7', 'armv5')):
        abi = 'eabi'
    elif arch.startswith('mips64'):
        abi = 'abi64'
    env['TARGET'] = cpu + '-baserock-linux-gnu' + abi
    env['TARGET_MUSL'] = cpu + '-bootstrap-linux-musl' + abi
    env['MORPH_ARCH'] = arch
    env['DEFINITIONS_REF'] = app.config['def-version']
    env['PROGRAM_REF'] = app.config['my-version']
    if this.get('SOURCE_DATE_EPOCH'):
        env['SOURCE_DATE_EPOCH'] = this['SOURCE_DATE_EPOCH']

    return env


def create_devices(this):

    destdir = os.path.join('/', os.path.basename(this['install'].rstrip(os.path.sep)))
    for device in this['devices']:
        destfile = os.path.join(destdir, device['filename'].strip(os.path.sep))

        app.log(this, "Creating device node", device['filename'])
        run_sandboxed(this, 'mknod -m ' + \
                      device['permissions'] + ' ' + destfile + ' ' + device['type'] + ' ' + \
                      str(device['major']) + ' ' + \
                      str(device['minor']) + \
                      ' && ' +  \
                      'chown ' + str(device['uid']) + ':' + str(device['gid']) + ' ' + destfile + \
                      ' || exit 1')


def list_files(component):
    try:
        app.log(component, 'Sandbox %s contains\n' % component['sandbox'],
                os.listdir(component['sandbox']))
        files = os.listdir(os.path.join(component['sandbox'], 'baserock'))
        app.log(component,
                'Baserock directory contains %s items\n' % len(files),
                sorted(files))
    except:
        app.log(component, 'No baserock directory in', component['sandbox'])
