import logging
import os
import re
import socket
import sys
import telnetlib
import time
from urllib import request

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.template import Context, Template
from orthos2.utils.misc import execute, get_s390_hostname, get_domain
from orthos2.utils.ssh import SSH

from orthos2.utils.cobbler import CobblerServer
from orthos2.data.models import System, Domain

logger = logging.getLogger('orthos')

class RemotePower(models.Model):

    class Action:
        ON = 'on'
        OFF = 'off'
        OFF_SSH = 'off-ssh'
        OFF_REMOTEPOWER = 'off-remotepower'
        REBOOT = 'reboot'
        REBOOT_SSH = 'reboot-ssh'
        REBOOT_REMOTEPOWER = 'reboot-remotepower'
        STATUS = 'status'

        as_list = [
            ON,
            OFF,
            REBOOT,
            STATUS,
            OFF_SSH,
            OFF_REMOTEPOWER,
            REBOOT_SSH,
            REBOOT_REMOTEPOWER
        ]

    class Type:
        TELNET = 0
        SENTRY = 1
        ILO = 2
        IPMI = 3
        DOMINIONPX = 4
        LIBVIRTQEMU = 5
        LIBVIRTLXC = 6
        WEBCURL = 7
        S390 = 8

        @classmethod
        def to_str(cls, index):
            """Return type as string (remote power type name) by index."""
            for type_tuple in RemotePower.TYPE_CHOICES:
                if int(index) == type_tuple[0]:
                    return type_tuple[1]
            raise Exception("Remote power type with ID '{}' doesn't exist!".format(index))

        @classmethod
        def to_int(cls, name):
            """Return type as integer if name matches."""
            for type_tuple in RemotePower.TYPE_CHOICES:
                if name.lower() == type_tuple[1].lower():
                    return type_tuple[0]
            raise Exception("Remote power type '{}' not found!".format(name))

    class Status:
        UNKNOWN = 0
        ON = 1
        OFF = 2
        BOOT = 3
        SHUTDOWN = 4
        PAUSED = 5

        @classmethod
        def to_str(cls, index):
            return {
                cls.UNKNOWN: 'unknown',
                cls.ON: 'on',
                cls.OFF: 'off',
                cls.BOOT: 'boot',
                cls.SHUTDOWN: 'shut down',
                cls.PAUSED: 'paused'
            }.get(index, 'undefined')

    TYPE_CHOICES = (
        (Type.TELNET, 'Telnet'),
        (Type.SENTRY, 'Sentry'),
        (Type.ILO, 'ILO'),
        (Type.IPMI, 'IPMI'),
        (Type.DOMINIONPX, 'Dominion PX'),
        (Type.LIBVIRTQEMU, 'libvirt/qemu'),
        (Type.LIBVIRTLXC, 'libvirt/lxc'),
        (Type.WEBCURL, 'WEBcurl'),
        (Type.S390, 's390')
    )

    def limit_remote_power_device_choices():
        """
        Allow only devices of type remote power.

        This needs to be in callable form because of later assignment of the type variable.
        """
        return {'system': System.Type.REMOTEPOWER}

    machine = models.OneToOneField(
        'data.Machine',
        on_delete=models.CASCADE,
        primary_key=True
    )

    type = models.SmallIntegerField(
        choices=TYPE_CHOICES,
        blank=False,
        null=False,
    )

    management_bmc = models.ForeignKey(
        'data.Machine',
        verbose_name='Management BMC',
        related_name='managed_remotepower',
        blank=True,
        null=True,
        on_delete=models.CASCADE
    )

    remote_power_device = models.ForeignKey(
        'data.Machine',
        related_name='+',
        limit_choices_to=limit_remote_power_device_choices,
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )

    port = models.SmallIntegerField(
        null=True,
        blank=True
    )

    device = models.SmallIntegerField(
        null=True,
        blank=True
    )

    comment = models.CharField(
        max_length=200,
        null=True,
        blank=True
    )

    updated = models.DateTimeField(
        'Updated at',
        auto_now=True
    )

    created = models.DateTimeField(
        'Created at',
        auto_now_add=True
    )

    def __init__(self, *args, **kwargs):
        """
        Cast plain `RemotePower` object to respective subclass.

        Subclasses getting collected automatically by inheritance of `RemotePower` class.
        """
        subclasses = {
            sub.__name__.replace(self.__class__.__name__, '').lower(): sub
            for sub in self.__class__.__subclasses__()
        }
        super(RemotePower, self).__init__(*args, **kwargs)

    def __str__(self):
        if self.type is None:
            return 'None'
        return '{}@{}'.format(self.name, self.machine.fqdn)

    def save(self, *args, **kwargs):
        """Check values before saving the remote power object. Do only save if type is set."""
        self.clean()

        if self.remote_power_device:
            if self.remote_power_device.system_id != System.Type.REMOTEPOWER:
                raise ValidationError(
                    (
                        "Remote power device '{}' must have system type 'REMOTEPOWER'!".format(
                            self.remote_power_device
                        )
                    )
                )

        # check for `None` explicitly because type 0 results in false
        if self.type is not None:
            super(RemotePower, self).save(*args, **kwargs)
        else:
            raise ValidationError("No remote power type set!")

    def clean(self):
        """
        Check for every remote power type if all required fields are set and deletes unutilized
        values.
        """
        errors = []

        if self.type in {self.Type.TELNET, self.Type.DOMINIONPX}:
            if not self.remote_power_device:
                errors.append(ValidationError("Please provide a remote power device!"))

            if self.port is None:
                errors.append(ValidationError("Please provide a port!"))

            # requires: remote_power_device, port
            self.device = None
            self.management_bmc = None

        elif self.type in {self.Type.SENTRY, self.Type.S390}:
            if not self.remote_power_device:
                errors.append(ValidationError("Please provide a remote power device!"))

            # requires: remote_power_device
            self.device = None
            self.management_bmc = None
            self.port = None

        elif self.type in {self.Type.ILO, self.Type.IPMI, self.Type.WEBCURL}:
            if not self.machine.bmc:
                errors.append(ValidationError("Please add at least one BMC to the enclosure!"))

            if not self.management_bmc:
                errors.append(ValidationError("Please select a management BMC!"))

            # requires: management_bmc
            self.device = None
            self.port = None
            self.remote_power_device = None

        elif self.type in {self.Type.LIBVIRTQEMU, self.Type.LIBVIRTLXC}:
            if not self.machine.hypervisor:
                errors.append(ValidationError("No hypervisor found!"))

            # requires: -
            self.device = None
            self.management_bmc = None
            self.port = None
            self.remote_power_device = None

        if errors:
            raise ValidationError(errors)

    @property
    def name(self):
        if self.type is None:
            return None
        return self.Type.to_str(self.type)

    def power_on(self):
        """Power on the machine."""
        self._perform('on')

    def power_off(self):
        """Power off the machine."""
        self._perform('off')

    def reboot(self):
        """Reboot the machine."""
        logger.warning("Not implemented: {}".format(self))

    def get_status(self):
        """Return the current power status."""
        status = self.Status.UNKNOWN
        result = self._perform('status')

        if result and isinstance(result, int):
            status = result
        elif result and result.lower().find('off') > -1:
            status = self.Status.OFF
        elif result and result.lower().find('on') > -1:
            status = self.Status.ON

        return status

    def get_credentials(self, remotepower_type=None, password_only=False):
        """
        Return username and password for remotepower login.

        Check for specific remotepower type if given, use default password/username if no DB entry
        exists. If no password/username can be found, an exception gets raised.

        The return type is a tuple: (<password>, <username>)
        """
        password = None
        username = None

        if remotepower_type is not None:
            password = ServerConfig.objects.by_key(
                'remotepower.{}.password'.format(remotepower_type)
            )

        if not password:
            password = ServerConfig.objects.by_key('remotepower.default.password')
            if not password:
                raise Exception(
                    "No login password available for {}".format(remotepower_type.upper())
                )

        if password_only:
            return (password, None)

        if remotepower_type is not None:
            username = ServerConfig.objects.by_key(
                'remotepower.{}.username'.format(remotepower_type)
            )

        if not username:
            username = ServerConfig.objects.by_key('remotepower.default.username')
            if not username:
                raise Exception(
                    "No login user available for {}".format(remotepower_type.upper())
                )

        return (password, username)

    def _perform(self, action: str):
        logger.info("performing powerswitch on %s with action %s", self.management_bmc.fqdn, action)
        domain = Domain.objects.get(name=get_domain(self.machine.fqdn))
        for server in domain.cobbler_server.all():
            try:
                logger.debug("trying server %s", server.fqdn)
                cobbler_server = CobblerServer(server.fqdn, domain)
                cobbler_server.powerswitch(self.machine, action)
            except Exception as e:
                logger.warning("Powerswitching of %s on %s failed with %s.",self.machine.fqdn,
                server.fqdn, e.msg)
            else:
                break
        else:
            logger.error("powerswitching of %s failed on all cobbler servers", self.machine.fqdn)
