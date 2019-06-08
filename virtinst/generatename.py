#
# Copyright 2019 Red Hat, Inc.
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.
#

import libvirt


def libvirt_collision(collision_cb, val):
    """
    Run the passed collision function with val as the only argument:
    If libvirtError is raised, return False
    If no libvirtError raised, return True
    """
    check = False
    if val is not None:
        try:
            if collision_cb(val) is not None:
                check = True
        except libvirt.libvirtError:
            pass
    return check


def generate_name(base, collision_cb, suffix="", lib_collision=True,
                  start_num=1, sep="-", force_num=False, collidelist=None):
    """
    Generate a new name from the passed base string, verifying it doesn't
    collide with the collision callback.

    This can be used to generate disk path names from the parent VM or pool
    name. Names generated look like 'base-#suffix', ex:

    If foobar, and foobar-1.img already exist, and:
    base   = "foobar"
    suffix = ".img"

    output = "foobar-2.img"

    :param base: The base string to use for the name (e.g. "my-orig-vm-clone")
    :param collision_cb: A callback function to check for collision,
        receives the generated name as its only arg
    :param lib_collision: If true, the collision_cb is not a boolean function,
        and instead throws a libvirt error on failure
    :param start_num: The number to start at for generating non colliding names
    :param sep: The separator to use between the basename and the
        generated number (default is "-")
    :param force_num: Force the generated name to always end with a number
    :param collidelist: An extra list of names to check for collision
    """
    collidelist = collidelist or []
    base = str(base)

    def collide(n):
        if n in collidelist:
            return True
        if lib_collision:
            return libvirt_collision(collision_cb, tryname)
        else:
            return collision_cb(tryname)

    numrange = list(range(start_num, start_num + 100000))
    if not force_num:
        numrange = [None] + numrange

    for i in numrange:
        tryname = base
        if i is not None:
            tryname += ("%s%d" % (sep, i))
        tryname += suffix

        if not collide(tryname):
            return tryname

    raise ValueError(_("Name generation range exceeded."))