# Copyright (C) 2016  Codethink Limited
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

import os
import stat
import tarfile
import gzip
import errno
import pwd
import calendar

############################################
# Virtfs metadata file format:
#
#   virtfs.uid=0
#   virtfs.gid=0
#   virtfs.mode=8624
#   virtfs.rdev=1105
#
# The above keys correspond with the corresponding 'struct stat'
# members st_uid, st_gid, st_mode and st_rdev.
#
############################################
VIRTFS_META_DIR  = '.virtfs_metadata'
VIRTFS_UID_KEY   = 'virtfs.uid'
VIRTFS_GID_KEY   = 'virtfs.gid'
VIRTFS_MODE_KEY  = 'virtfs.mode'
VIRTFS_RDEV_KEY  = 'virtfs.rdev'

# Default timestamp override for generating artifacts
MAGIC_TIMESTAMP  = calendar.timegm([2011, 11, 11, 11, 11, 11])

############################################################
#                   Private Methods                        #
############################################################

#
# XXX FIXME: These utilities are apart...
#
#  A.) Python does not give us kernel MKDEV()
#
#  B.) Major & Minor numbers are platform independent,
#      but dev_t values are not, hopefully they will
#      only depend on endienness, and we control our
#      aboriginal kernel so it should not be toooo hard.
#
def _dev_major(dev):
    return ((int)(((dev) >> 8) & 0xff))

def _dev_minor(dev):
    return ((int)((dev) & 0xff))

def _dev_from_major_minor(major, minor):
    return ((int)(((major) << 8) | (minor)))


def _read_virtfs_attrs(dir_name, file_name):
    basename   = os.path.basename(file_name.rstrip('/'))
    attrs_file = os.path.join(dir_name, VIRTFS_META_DIR, basename)
    attrs = {}

    with open(attrs_file, 'r') as virtfs_attrs:
        for line in virtfs_attrs:
            split      = line.rstrip('\n').split('=')
            key        = split[0]
            value      = int(split[1])
            attrs[key] = value

    return attrs

def _write_virtfs_attrs(attrs, dir_name, file_name):
    basename   = os.path.basename(file_name.rstrip('/'))
    attrs_dir  = os.path.join(dir_name, VIRTFS_META_DIR)
    attrs_file = os.path.join(attrs_dir, basename)

    # First ensure the virtfs meta directory, ignore error
    # that the directory exists
    try:
        os.mkdir(attrs_dir)
    except OSError as e:
        if e.errno is not errno.EEXIST:
            raise

    # Dont overwrite existing virtfs metadata files
    if os.path.isfile(attrs_file):
        return

    with open(attrs_file, 'w+') as virtfs_attrs:
        virtfs_attrs.write(VIRTFS_UID_KEY  + '=%s\n' % attrs[VIRTFS_UID_KEY])
        virtfs_attrs.write(VIRTFS_GID_KEY  + '=%s\n' % attrs[VIRTFS_GID_KEY])
        virtfs_attrs.write(VIRTFS_MODE_KEY + '=%s\n' % attrs[VIRTFS_MODE_KEY])
        if VIRTFS_RDEV_KEY in attrs:
            virtfs_attrs.write(VIRTFS_RDEV_KEY + '=%s\n' % attrs[VIRTFS_RDEV_KEY])


# TarFile module has a function called gettarinfo() which
# creates a TarInfo based on a stat system call, unfortunately
# it does not expose the logic for setting up the TarInfo from
# the stat results, so we duplicate that internal logic here
# and setup the tarinfo from the virtfs stat info instead of
# a proper os.stat()
#
# We pass the directory name here because we need to actually
# read the file content in the case of a symlink
def _apply_virtfs_attrs(tar_f, tarinfo, dir_name, attrs):

    linkname = ""
    basename = os.path.basename(tarinfo.name.rstrip('/'))
    fullpath = os.path.join(dir_name, basename)
    stmd     = attrs[VIRTFS_MODE_KEY]

    if stat.S_ISREG(stmd):
        # NOTE: The regular TarFile algorithm takes everything from
        # an lstat of the file to add, for virtfs, we have some of the
        # information available from the virtfs metadata but to resolve
        # hardlinks we need to also perform an lstat on the file.
        #
        # The following does pretty much exactly what TarFile does
        # to fill in the blanks.
        statres = os.lstat(fullpath)
        inode   = (statres.st_ino, statres.st_dev)
        if not tar_f.dereference and statres.st_nlink > 1 and \
           inode in tar_f.inodes and tarinfo.name != tar_f.inodes[inode]:
            # Is it a hardlink to an already archived file?
            type     = tarfile.LNKTYPE
            linkname = tar_f.inodes[inode]
        else:
            # The inode is added only if its valid.
            type = tarfile.REGTYPE
            if inode[0]:
                tar_f.inodes[inode] = tarinfo.name

    elif stat.S_ISDIR(stmd):
        type = tarfile.DIRTYPE
    elif stat.S_ISFIFO(stmd):
        type = tarfile.FIFOTYPE
    elif stat.S_ISLNK(stmd):

        type = tarfile.SYMTYPE

        # A symlink target is stored directly as the content
        # of the file, better read the file to resolve the link target
        with open(fullpath, 'r') as linkfile:
            linkname = linkfile.read()

    elif stat.S_ISCHR(stmd):
        type = tarfile.CHRTYPE
    elif stat.S_ISBLK(stmd):
        type = tarfile.BLKTYPE
    else:
        raise Exception('Failed to interpret file type from virtfs stat mode')

    # Fill the TarInfo object
    #
    # NOTE: Python's tarfile tries to read the uid/gid names
    #       from the running system, but when interpreting the virtfs
    #       attributes we simply cannot expect to know the name of
    #       a user or group id.
    tarinfo.mode  = stmd
    tarinfo.uid   = attrs[VIRTFS_UID_KEY]
    tarinfo.gid   = attrs[VIRTFS_GID_KEY]
    tarinfo.uname = str(attrs[VIRTFS_UID_KEY])
    tarinfo.gname = str(attrs[VIRTFS_GID_KEY])

    tarinfo.type     = type
    tarinfo.linkname = linkname

    if type != tarfile.REGTYPE:
        tarinfo.size = 0L

    if type in (tarfile.CHRTYPE, tarfile.BLKTYPE):
        st_rdev = attrs[VIRTFS_RDEV_KEY]
        tarinfo.devmajor = _dev_major(st_rdev)
        tarinfo.devminor = _dev_minor(st_rdev)

# Creates the appropriate virtfs attributes from the
# given tarinfo... this is largely the reverse of _apply_virtfs_attrs()
# except that we look deep into python's stat.py implementation and
# construct the mode and rdev bits by hand
def _extract_virtfs_attrs(tarinfo):

    # The tarinfo.mode holds permission bits
    # but not the file type bits from struct stat,
    # these need to be or'ed onto the final mode
    stmd = tarinfo.mode
    if tarinfo.type in (tarfile.REGTYPE, tarfile.LNKTYPE):
        stmd |= stat.S_IFREG
    elif tarinfo.type is tarfile.DIRTYPE:
        stmd |= stat.S_IFDIR
    elif tarinfo.type is tarfile.FIFOTYPE:
        stmd |= stat.S_IFIFO
    elif tarinfo.type is tarfile.SYMTYPE:
        stmd |= stat.S_IFLNK
    elif tarinfo.type is tarfile.CHRTYPE:
        stmd |= stat.S_IFCHR
    elif tarinfo.type is tarfile.BLKTYPE:
        stmd |= stat.S_IFBLK

    # Create and return the attrs
    attrs = {}
    attrs[VIRTFS_UID_KEY]  = tarinfo.uid
    attrs[VIRTFS_GID_KEY]  = tarinfo.gid
    attrs[VIRTFS_MODE_KEY] = stmd

    if tarinfo.type in (tarfile.CHRTYPE, tarfile.BLKTYPE):
        attrs[VIRTFS_RDEV_KEY] = _dev_from_major_minor(tarinfo.devmajor, tarinfo.devminor)

    return attrs

def _extract_tarinfo_into_staging(tar_f, tarinfo, directory):

    # Assign ownership to the calling process.
    tarinfo.uid = os.geteuid()
    tarinfo.gid = os.getegid()
    tarinfo.uname = pwd.getpwuid(tarinfo.uid)[0]
    tarinfo.gname = pwd.getpwuid(tarinfo.gid)[0]

    # Set mode to read/write for the owner only, except
    # for directories which also need the executable bit set
    tarinfo.mode = 0600

    if tarinfo.type in (tarfile.REGTYPE, tarfile.LNKTYPE):
        # Let TarFile do the regular thing for regular
        # files and also for hardlinks
        tar_f.extract (tarinfo, directory)

    elif tarinfo.type is tarfile.DIRTYPE:
        # Directories are executable
        tarinfo.mode = 0700
        tar_f.extract (tarinfo, directory)

    elif tarinfo.type is tarfile.SYMTYPE:
        # Symlinks are special, they need to be
        # converted to a regular file with linkname as
        # the actual content
        linkname = tarinfo.linkname
        fullpath = os.path.join(directory, tarinfo.name)
        with open(fullpath, 'w+') as fakelink:
            fakelink.write(linkname)
            os.chmod(fullpath, 0600)

    elif tarinfo.type in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
        # These are just 0 length regular files with the
        # interesting attributes set in the virtfs metadata
        tarinfo.type = tarfile.REGTYPE
        tarinfo.devmajor = None
        tarinfo.devminor = None

        tar_f.extract (tarinfo, directory)


def _add_directory_to_tarfile(tar_f, dir_name, dir_arcname, mtime):

    def filter_virtfs_attrs(tarinfo):

        # Read the virtfs attributes for this file and apply them to the TarInfo,
        # if we fail to read the virtfs directory/file then we assume it's a file
        # we created on the host, outside of the virtfs, and it's safe to just
        # add the file unfiltered.
        try:
            attrs = _read_virtfs_attrs (dir_name, tarinfo.name)
            _apply_virtfs_attrs (tar_f, tarinfo, dir_name, attrs)
        except IOError as e:
            if e.errno is errno.ENOENT:
                pass

        # Set the mtime
        tarinfo.mtime = mtime

        # Some data gets created outside of the build sandbox, like
        # the baserock chunk metadata, we want to preserve the uid/gid
        # of non-root files created by the VM, but we want to give any
        # files created outside of the sandbox to root
        if tarinfo.uid   == os.geteuid() and \
           tarinfo.gid   == os.getegid() and \
           tarinfo.uname == pwd.getpwuid(tarinfo.uid)[0] and \
           tarinfo.gname == pwd.getpwuid(tarinfo.gid)[0]:
            tarinfo.uid = tarinfo.gid = 0;
            tarinfo.uname = tarinfo.gname = '0'

        return tarinfo


    for filename in sorted(os.listdir(dir_name)):

        # Dont add the metadata file itself to the archive.
        if filename == VIRTFS_META_DIR:
            continue

        name    = os.path.join(dir_name, filename)
        arcname = os.path.join(dir_arcname, filename)

        tar_f.add(name=name, arcname=arcname, recursive=False, filter=filter_virtfs_attrs)

        if os.path.isdir(name) and not os.path.islink(name):
            _add_directory_to_tarfile(tar_f, name, arcname, mtime)


def _stage_tarinfo_to_directory(tar_f, tarinfo, directory):

    fullpath = os.path.join(directory, tarinfo.name.rstrip(os.path.sep))
    stripped = fullpath.rstrip(os.path.sep)
    split    = os.path.split (fullpath)
    dir_name = split[0]
    filename = split[1]

    # Use existing tarinfo to write out the virtfs metadata
    attrs = _extract_virtfs_attrs(tarinfo)
    _write_virtfs_attrs(attrs, dir_name, filename)

    # Modify the tarinfo inline
    _extract_tarinfo_into_staging(tar_f, tarinfo, directory)


def _stage_file_for_virtfs(dir_name, filename, fullpath=None):

    if not fullpath:
        fullpath = os.path.join(dir_name, filename)

    statres = os.lstat(fullpath)
    stmd    = statres.st_mode

    attrs   = {}
    attrs[VIRTFS_UID_KEY] = 0
    attrs[VIRTFS_GID_KEY] = 0
    attrs[VIRTFS_MODE_KEY] = stmd

    if stat.S_ISCHR(stmd) or \
       stat.S_ISBLK(stmd) or \
       stat.S_ISFIFO(stmd):
        attrs[VIRTFS_RDEV_KEY] = statres.st_rdev

        # It would be unusual to manually stage block/char devices
        # or fifos, but if it happens, lets make it a regular
        # file and conform to how virtfs reads/writes it
        os.unlink(fullpath)
        open(fullpath, 'w+').close()

    elif stat.S_ISLNK(stmd):

        # Convert symlinks to regular files holding the
        # link target as data
        linkname = os.readlink (fullpath)
        os.unlink(fullpath)
        with open(fullpath, 'w+') as link:
            link.write(linkname)

    # All files are regularly 0600 and directories 0700
    if stat.S_ISDIR(stmd):
        os.chmod(fullpath, 0700)
    else:
        os.chmod(fullpath, 0600)

    # Write out the virtfs attributes metadata
    _write_virtfs_attrs(attrs, dir_name, filename)

############################################################
#                        Public API                        #
############################################################

# collect_artifact()
# @filename: The filename of the artifact to create
# @root_dir: The base directory to collect
# @compress: Whether the create a gzipped archive or just a regular archive
# @time: The mtime for the created artifact (optional)
#
# Collects files from a virtfs share at @root_dir and creates an artifact named @filename
#
def collect_artifact(filename, root_dir, compress=True, time=MAGIC_TIMESTAMP):

    with open(filename, 'wb') as f:

        fileobject = f
        if (compress):
            gzip_context = gzip.GzipFile(
                filename='', mode='wb', fileobj=f, mtime=time)
            fileobject = gzip_context

        with fileobject as filecontext:
            with tarfile.TarFile(mode='w', fileobj=filecontext) as tar_f:
                _add_directory_to_tarfile(tar_f, root_dir, '.', time)

# stage_artifact()
# @filename: The filename of the artifact to stage
# @directory: The base directory to stage the artifact at
# @compress: Whether the artifact is compressed
#
# Stages the contents of the artifact @filename into @directory,
# converting the results so as to be readable by a virtfs share.
#
def stage_artifact(filename, directory, compress=True):

    mode='r'
    if compress:
        mode='r:gz'

    with tarfile.open(filename, mode) as tar_f:
        for tarinfo in tar_f.getmembers():
            _stage_tarinfo_to_directory(tar_f, tarinfo, directory)


# stage_file()
# @fullpath: The full path of the file in the staging area
#
# Prepares @filename in the staging area to be properly read by
# the virtfs mount.
#
# Since this function modifies files on disk, the staged files
# cannot be expected to be usable on the host, they are only
# to be read by the virtfs mount in the emulator guest.
#
# Staged files can however be collected again in an artifact
# by way of the collect_artifact() function.
#
def stage_file(fullpath):
    stripped = fullpath.rstrip(os.path.sep)
    split    = os.path.split (fullpath)
    dir_name = split[0]
    filename = split[1]

    _stage_file_for_virtfs(dir_name, filename, fullpath)

# stage_directory()
# @dir_name: The directory to stage
#
# Recursively prepares @dir_name to be correctly interpreted
# by a virtfs mount. See stage_file() for more details.
#
def stage_directory(dir_name):

    for filename in os.listdir(dir_name):
        if filename == VIRTFS_META_DIR:
            continue

        name = os.path.join(dir_name, filename)
        _stage_file_for_virtfs (dir_name, filename, name)

        if os.path.isdir(name) and not os.path.islink(name):
            stage_directory(name)
