#
# Common code for all guests
#
# Copyright 2006-2009  Red Hat, Inc.
# Jeremy Katz <katzj@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free  Software Foundation; either version 2 of the License, or
# (at your option)  any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.

import os
import platform
import logging
import copy

import virtinst
from virtinst import osxml
from virtinst import util
from virtinst.xmlbuilder import XMLBuilder, XMLProperty

XEN_SCRATCH = "/var/lib/xen"
LIBVIRT_SCRATCH = "/var/lib/libvirt/boot"


class Installer(XMLBuilder):
    """
    Installer classes attempt to encapsulate all the parameters needed
    to 'install' a guest: essentially, booting the guest with the correct
    media for the OS install phase (if there is one), and setting up the
    guest to boot to the correct media for all subsequent runs.

    Some of the actual functionality:

        - Determining what type of install media has been requested, and
          representing it correctly to the Guest

        - Fetching install kernel/initrd or boot.iso from a URL

        - Setting the boot device as appropriate depending on whether we
          are booting into an OS install, or booting post-install

    Some of the information that the Installer needs to know to accomplish
    this:

        - Install media location (could be a URL, local path, ...)
        - Virtualization type (parameter 'os_type') ('xen', 'hvm', etc.)
        - Hypervisor name (parameter 'type') ('qemu', 'kvm', 'xen', etc.)
        - Guest architecture ('i686', 'x86_64')
    """
    _dumpxml_xpath = "/domain/os"
    _has_install_phase = True

    def __init__(self, conn, parsexml=None, parsexmlnode=None):
        XMLBuilder.__init__(self, conn, parsexml, parsexmlnode)

        self._location = None
        self._cdrom = False
        self._scratchdir = None

        self.initrd_injections = []
        self.bootconfig = osxml.OSXML(self.conn, parsexml, parsexmlnode)

        self._install_kernel = None
        self._install_initrd = None
        self._install_args = None

        # Devices created/added during the prepare() stage
        self.install_devices = []

        self._tmpfiles = []
        self._tmpvols = []


    #####################
    # XML related props #
    #####################

    def _set_type(self, val):
        self.bootconfig.type = val
    type = property(lambda s: s.bootconfig.type, _set_type)
    def _set_os_type(self, val):
        self.bootconfig.os_type = val
    os_type = property(lambda s: s.bootconfig.os_type, _set_os_type)
    def _set_machine(self, val):
        self.bootconfig.machine = val
    machine = property(lambda s: s.bootconfig.machine, _set_machine)
    def _set_arch(self, val):
        self.bootconfig.arch = val
    arch = property(lambda s: s.bootconfig.arch, _set_arch)
    def _set_loader(self, val):
        self.bootconfig.loader = val
    loader = property(lambda s: s.bootconfig.loader, _set_loader)
    def _set_init(self, val):
        self.bootconfig.init = val
    init = property(lambda s: s.bootconfig.init, _set_init)


    ######################
    # Non-XML properties #
    ######################

    def get_scratchdir(self):
        if not self.scratchdir_required():
            return None

        if not self._scratchdir:
            self._scratchdir = self._get_scratchdir()
            logging.debug("scratchdir=%s", self._scratchdir)
        return self._scratchdir
    scratchdir = property(get_scratchdir)

    def get_cdrom(self):
        return self._cdrom
    def set_cdrom(self, enable):
        if enable not in [True, False]:
            raise ValueError(_("Guest.cdrom must be a boolean type"))
        self._cdrom = enable
    cdrom = property(get_cdrom, set_cdrom)

    def get_location(self):
        return self._location
    def set_location(self, val):
        self._location = self._validate_location(val)
    location = property(get_location, set_location)

    def get_extra_args(self):
        return self._install_args
    def set_extra_args(self, val):
        self._install_args = val
    extraargs = property(get_extra_args, set_extra_args)


    ###################
    # Private helpers #
    ###################

    def _get_system_scratchdir(self):
        if platform.system() == "SunOS":
            return "/var/tmp"

        if self.type == "test":
            return "/tmp"
        elif self.type == "xen":
            return XEN_SCRATCH
        else:
            return LIBVIRT_SCRATCH

    def _get_scratchdir(self):
        scratch = None
        if not self.conn.is_session_uri():
            scratch = self._get_system_scratchdir()

        if (not scratch or
            not os.path.exists(scratch) or
            not os.access(scratch, os.W_OK)):
            scratch = os.path.expanduser("~/.virtinst/boot")
            if not os.path.exists(scratch):
                os.makedirs(scratch, 0751)

        return scratch

    def _build_boot_order(self, isinstall, guest):
        bootdev = self._get_bootdev(isinstall, guest)
        if bootdev is None:
            # None here means 'kernel boot'
            return []

        bootorder = [bootdev]

        # If guest has an attached disk, always have 'hd' in the boot
        # list, so disks are marked as bootable/installable (needed for
        # windows virtio installs, and booting local disk from PXE)
        for disk in guest.get_devices("disk"):
            if disk.device == disk.DEVICE_DISK:
                bootdev = self.bootconfig.BOOT_DEVICE_HARDDISK
                if bootdev not in bootorder:
                    bootorder.append(bootdev)
                break

        return bootorder

    def _make_cdrom_dev(self, path):
        dev = virtinst.VirtualDisk(self.conn)
        dev.path = path
        dev.device = dev.DEVICE_CDROM
        dev.read_only = True
        dev.validate()
        return dev

    def _get_xml_config(self, guest, isinstall):
        """
        Generate the portion of the guest xml that determines boot devices
        and parameters. (typically the <os></os> block)

        @param guest: Guest instance we are installing
        @type guest: L{Guest}
        @param isinstall: Whether we want xml for the 'install' phase or the
                          'post-install' phase.
        @type isinstall: C{bool}
        """
        # pylint: disable=W0221
        # Argument number differs from overridden method
        if isinstall and not self.has_install_phase():
            return

        bootconfig = self.bootconfig.copy()
        bootorder = self._build_boot_order(isinstall, guest)

        if not bootconfig.bootorder:
            bootconfig.bootorder = bootorder

        if isinstall:
            bootconfig = bootconfig.copy()
            if self._install_kernel:
                bootconfig.kernel = self._install_kernel
            if self._install_initrd:
                bootconfig.initrd = self._install_initrd
            if self._install_args:
                bootconfig.kernel_args = self._install_args

        return self.bootconfig._get_osblob_helper(guest, isinstall,
                                                  bootconfig, self.bootconfig)


    ##########################
    # Internal API overrides #
    ##########################

    def _get_bootdev(self, isinstall, guest):
        raise NotImplementedError("Must be implemented in subclass")

    def _validate_location(self, val):
        return val

    def _prepare(self, guest, meter):
        ignore = guest
        ignore = meter


    ##############
    # Public API #
    ##############

    def scratchdir_required(self):
        """
        Returns true if scratchdir is needed for the passed install parameters.
        Apps can use this to determine if they should attempt to ensure
        scratchdir permissions are adequate
        """
        return False

    is_hvm = lambda s: s.bootconfig.is_hvm()
    is_xenpv = lambda s: s.bootconfig.is_xenpv()
    is_container = lambda s: s.bootconfig.is_container()

    def has_install_phase(self):
        """
        Return True if the requested setup is actually installing an OS
        into the guest. Things like LiveCDs, Import, or a manually specified
        bootorder do not have an install phase.
        """
        return self._has_install_phase

    def cleanup(self):
        """
        Remove any temporary files retrieved during installation
        """
        for f in self._tmpfiles:
            logging.debug("Removing " + f)
            os.unlink(f)

        for vol in self._tmpvols:
            logging.debug("Removing volume '%s'", vol.name())
            vol.delete(0)

        self._tmpvols = []
        self._tmpfiles = []
        self.install_devices = []

    def prepare(self, guest, meter):
        self.cleanup()
        self._prepare(guest, meter)

    def check_location(self):
        """
        Validate self.location seems to work. This will might hit the
        network so we don't want to do it on demand.
        """
        return True

    def detect_distro(self):
        """
        Attempt to detect the distro for the Installer's 'location'. If
        an error is encountered in the detection process (or if detection
        is not relevant for the Installer type), (None, None) is returned

        @returns: (distro type, distro variant) tuple
        """
        return (None, None)

    def guest_from_installer(self):
        """
        Return a L{Guest} instance wrapping the current installer.

        If all the appropriate values are present in the installer
        (conn, type, os_type, arch, machine), we have everything we need
        to determine what L{Guest} class is expected and what default values
        to pass it. This is a convenience method to save the API user from
        having to enter all these known details twice.
        """
        guest, domain = self.conn.caps.guest_lookup(os_type=self.os_type,
                                                    typ=self.type,
                                                    arch=self.arch,
                                                    machine=self.machine)

        gobj = virtinst.Guest(self.conn)
        gobj.installer = self
        gobj.arch = guest.arch
        gobj.emulator = domain.emulator
        self.loader = domain.loader

        return gobj


class ContainerInstaller(Installer):
    _has_install_phase = False
    def _get_bootdev(self, isinstall, guest):
        ignore = isinstall
        ignore = guest
        return self.bootconfig.BOOT_DEVICE_HARDDISK


class PXEInstaller(Installer):
    def _get_bootdev(self, isinstall, guest):
        bootdev = self.bootconfig.BOOT_DEVICE_NETWORK

        if (not isinstall and
            [d for d in guest.get_devices("disk") if
             d.device == d.DEVICE_DISK]):
            # If doing post-install boot and guest has an HD attached
            bootdev = self.bootconfig.BOOT_DEVICE_HARDDISK

        return bootdev


class LiveCDInstaller(Installer):
    _has_install_phase = False
    cdrom = True

    def _validate_location(self, val):
        return self._make_cdrom_dev(val).path
    def _prepare(self, guest, meter):
        ignore = guest
        ignore = meter
        self.install_devices.append(self._make_cdrom_dev(self.location))
    def _get_bootdev(self, isinstall, guest):
        return self.bootconfig.BOOT_DEVICE_CDROM


class ImportInstaller(Installer):
    _has_install_phase = False

    # Private methods
    def _get_bootdev(self, isinstall, guest):
        disks = guest.get_devices("disk")
        if not disks:
            return self.bootconfig.BOOT_DEVICE_HARDDISK
        return self._disk_to_bootdev(disks[0])

    def _disk_to_bootdev(self, disk):
        if disk.device == virtinst.VirtualDisk.DEVICE_DISK:
            return self.bootconfig.BOOT_DEVICE_HARDDISK
        elif disk.device == virtinst.VirtualDisk.DEVICE_CDROM:
            return self.bootconfig.BOOT_DEVICE_CDROM
        elif disk.device == virtinst.VirtualDisk.DEVICE_FLOPPY:
            return self.bootconfig.BOOT_DEVICE_FLOPPY
        else:
            return self.bootconfig.BOOT_DEVICE_HARDDISK
