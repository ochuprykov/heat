# vim: tabstop=4 shiftwidth=4 softtabstop=4

#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import os
import pkgutil
from urlparse import urlparse
import yaml

from oslo.config import cfg

from heat.engine import signal_responder
from heat.engine import clients
from heat.engine import resource
from heat.engine import scheduler
from heat.engine.resources import volume

from heat.common import exception
from heat.engine.resources.network_interface import NetworkInterface

from heat.openstack.common.gettextutils import _
from heat.openstack.common import log as logging
from heat.openstack.common import uuidutils

logger = logging.getLogger(__name__)


class Restarter(signal_responder.SignalResponder):
    properties_schema = {'InstanceId': {'Type': 'String',
                                        'Required': True}}
    attributes_schema = {
        "AlarmUrl": ("A signed url to handle the alarm. "
                     "(Heat extension)")
    }

    def _find_resource(self, resource_id):
        '''
        Return the resource with the specified instance ID, or None if it
        cannot be found.
        '''
        for resource in self.stack:
            if resource.resource_id == resource_id:
                return resource
        return None

    def handle_signal(self, details=None):
        if details is None:
            alarm_state = 'alarm'
        else:
            alarm_state = details.get('state', 'alarm').lower()

        logger.info('%s Alarm, new state %s' % (self.name, alarm_state))

        if alarm_state != 'alarm':
            return

        victim = self._find_resource(self.properties['InstanceId'])
        if victim is None:
            logger.info('%s Alarm, can not find instance %s' %
                       (self.name, self.properties['InstanceId']))
            return

        logger.info('%s Alarm, restarting resource: %s' %
                    (self.name, victim.name))
        self.stack.restart_resource(victim.name)

    def _resolve_attribute(self, name):
        '''
        heat extension: "AlarmUrl" returns the url to post to the policy
        when there is an alarm.
        '''
        if name == 'AlarmUrl' and self.resource_id is not None:
            return unicode(self._get_signed_url())


class Instance(resource.Resource):
    # AWS does not require InstanceType but Heat does because the nova
    # create api call requires a flavor
    tags_schema = {'Key': {'Type': 'String',
                           'Required': True},
                   'Value': {'Type': 'String',
                             'Required': True}}

    properties_schema = {'ImageId': {'Type': 'String',
                                     'Required': True},
                         'InstanceType': {'Type': 'String',
                                          'Required': True},
                         'KeyName': {'Type': 'String'},
                         'AvailabilityZone': {'Type': 'String'},
                         'DisableApiTermination': {'Type': 'String',
                                                   'Implemented': False},
                         'KernelId': {'Type': 'String',
                                      'Implemented': False},
                         'Monitoring': {'Type': 'Boolean',
                                        'Implemented': False},
                         'PlacementGroupName': {'Type': 'String',
                                                'Implemented': False},
                         'PrivateIpAddress': {'Type': 'String',
                                              'Implemented': False},
                         'RamDiskId': {'Type': 'String',
                                       'Implemented': False},
                         'SecurityGroups': {'Type': 'List'},
                         'SecurityGroupIds': {'Type': 'List'},
                         'NetworkInterfaces': {'Type': 'List'},
                         'SourceDestCheck': {'Type': 'Boolean',
                                             'Implemented': False},
                         'SubnetId': {'Type': 'String'},
                         'Tags': {'Type': 'List',
                                  'Schema': {'Type': 'Map',
                                             'Schema': tags_schema}},
                         'NovaSchedulerHints': {'Type': 'List',
                                                'Schema': {
                                                    'Type': 'Map',
                                                    'Schema': tags_schema
                                                }},
                         'Tenancy': {'Type': 'String',
                                     'AllowedValues': ['dedicated', 'default'],
                                     'Implemented': False},
                         'UserData': {'Type': 'String'},
                         'Volumes': {'Type': 'List'}}

    attributes_schema = {'AvailabilityZone': ('The Availability Zone where the'
                                              ' specified instance is '
                                              'launched.'),
                         'PrivateDnsName': ('Private DNS name of the specified'
                                            ' instance.'),
                         'PublicDnsName': ('Public DNS name of the specified '
                                           'instance.'),
                         'PrivateIp': ('Private IP address of the specified '
                                       'instance.'),
                         'PublicIp': ('Public IP address of the specified '
                                      'instance.')}

    update_allowed_keys = ('Metadata', 'Properties')
    update_allowed_properties = ('InstanceType',)

    _deferred_server_statuses = ['BUILD',
                                 'HARD_REBOOT',
                                 'PASSWORD',
                                 'REBOOT',
                                 'RESCUE',
                                 'RESIZE',
                                 'REVERT_RESIZE',
                                 'SHUTOFF',
                                 'SUSPENDED',
                                 'VERIFY_RESIZE']

    def __init__(self, name, json_snippet, stack):
        super(Instance, self).__init__(name, json_snippet, stack)
        self.ipaddress = None
        self.mime_string = None

    def _set_ipaddress(self, networks):
        '''
        Read the server's IP address from a list of networks provided by Nova
        '''
        # Just record the first ipaddress
        for n in networks:
            if len(networks[n]) > 0:
                self.ipaddress = networks[n][0]
                break

    def _ipaddress(self):
        '''
        Return the server's IP address, fetching it from Nova if necessary
        '''
        if self.ipaddress is None:
            try:
                server = self.nova().servers.get(self.resource_id)
            except clients.novaclient.exceptions.NotFound as ex:
                logger.warn('Instance IP address not found (%s)' % str(ex))
            else:
                self._set_ipaddress(server.networks)

        return self.ipaddress or '0.0.0.0'

    def _resolve_attribute(self, name):
        res = None
        if name == 'AvailabilityZone':
            res = self.properties['AvailabilityZone']
        elif name in ['PublicIp', 'PrivateIp', 'PublicDnsName',
                      'PrivateDnsName']:
            res = self._ipaddress()

        logger.info('%s._resolve_attribute(%s) == %s' % (self.name, name, res))
        return unicode(res) if res else None

    def _build_userdata(self, userdata):
        if not self.mime_string:
            # Build mime multipart data blob for cloudinit userdata

            def make_subpart(content, filename, subtype=None):
                if subtype is None:
                    subtype = os.path.splitext(filename)[0]
                msg = MIMEText(content, _subtype=subtype)
                msg.add_header('Content-Disposition', 'attachment',
                               filename=filename)
                return msg

            def read_cfg_file():
                rd = pkgutil.get_data('heat', 'cloudinit/config')
                rd = rd.replace('@INSTANCE_USER@',
                                cfg.CONF.instance_user)
                rd = yaml.safe_load(rd)
                return rd

            def add_data_file(fn, data):
                rd = [{'owner': 'root:root',
                      'path': '/var/lib/heat-cfntools/%s' % fn,
                      'permissions': '0700',
                      'content': '%s' % data}]
                return rd

            def yaml_add(yd, fn, data):
                rd = '%s%s' % (yd, yaml.safe_dump(add_data_file(fn, data)))
                return rd

            def read_data_file(fn):
                data = pkgutil.get_data('heat', 'cloudinit/%s' % fn)
                rd = add_data_file(fn, data)
                return rd

            def read_cloudinit_file(fn):
                rd = pkgutil.get_data('heat', 'cloudinit/%s' % fn)
                rd = rd.replace('@INSTANCE_USER@', cfg.CONF.instance_user)
                return rd

            # Create a boto config which the cfntools on the host use to know
            # where the cfn and cw API's are to be accessed
            cfn_url = urlparse(cfg.CONF.heat_metadata_server_url)
            cw_url = urlparse(cfg.CONF.heat_watch_server_url)
            is_secure = cfg.CONF.instance_connection_is_secure
            vcerts = cfg.CONF.instance_connection_https_validate_certificates
            boto_cfg = "\n".join(["[Boto]",
                                  "debug = 0",
                                  "is_secure = %s" % is_secure,
                                  "https_validate_certificates = %s" % vcerts,
                                  "cfn_region_name = heat",
                                  "cfn_region_endpoint = %s" %
                                  cfn_url.hostname,
                                  "cloudwatch_region_name = heat",
                                  "cloudwatch_region_endpoint = %s" %
                                  cw_url.hostname])

            # Build yaml write_files section
            yamld = ''
            yamld = yaml_add(yamld, 'cfn-watch-server',
                             cfg.CONF.heat_watch_server_url)
            yamld = yaml_add(yamld, 'cfn-metadata-server',
                             cfg.CONF.heat_metadata_server_url)
            yamld = yaml_add(yamld, 'cfn-boto-cfg',
                             boto_cfg)
            yamld = yaml_add(yamld, 'cfn-userdata',
                             userdata)

            if 'Metadata' in self.t:
                yamld = yaml_add(yamld, 'cfn-init-data',
                                 json.dumps(self.metadata))

            yamld = "%swrite_files:\n%s" % (yaml.safe_dump(read_cfg_file()),
                    yamld)

            attachments = [(yamld, 'cloud-config'),
                           (read_cloudinit_file('boothook.sh'), 'boothook.sh',
                           'cloud-boothook'),
                           (read_cloudinit_file('loguserdata.py'),
                           'x-shellscript')]
            subparts = [make_subpart(*args) for args in attachments]
            mime_blob = MIMEMultipart(_subparts=subparts)

            self.mime_string = mime_blob.as_string()

        return self.mime_string

    def _build_nics(self, network_interfaces, subnet_id=None):

        nics = None

        if network_interfaces:
            unsorted_nics = []
            for entry in network_interfaces:
                nic = (entry
                       if not isinstance(entry, basestring)
                       else {'NetworkInterfaceId': entry,
                             'DeviceIndex': len(unsorted_nics)})
                unsorted_nics.append(nic)
            sorted_nics = sorted(unsorted_nics,
                                 key=lambda nic: int(nic['DeviceIndex']))
            nics = [{'port-id': nic['NetworkInterfaceId']}
                    for nic in sorted_nics]
        else:
            # if SubnetId property in Instance, ensure subnet exists
            if subnet_id:
                quantumclient = self.quantum()
                network_id = NetworkInterface.network_id_from_subnet_id(
                    quantumclient, subnet_id)
                # if subnet verified, create a port to use this subnet
                # if port is not created explicitly, nova will choose
                # the first subnet in the given network.
                if network_id:
                    fixed_ip = {'subnet_id': subnet_id}
                    props = {
                        'admin_state_up': True,
                        'network_id': network_id,
                        'fixed_ips': [fixed_ip]
                    }
                    port = quantumclient.create_port({'port': props})['port']
                    nics = [{'port-id': port['id']}]

        return nics

    def _get_security_groups(self):
        security_groups = []
        for property in ('SecurityGroups', 'SecurityGroupIds'):
            if self.properties.get(property) is not None:
                for sg in self.properties.get(property):
                    security_groups.append(sg)
        if not security_groups:
            security_groups = None
        return security_groups

    def _get_flavor_id(self, flavor):
        flavor_id = None
        flavor_list = self.nova().flavors.list()
        for o in flavor_list:
            if o.name == flavor:
                flavor_id = o.id
                break
        if flavor_id is None:
            raise exception.FlavorMissing(flavor_id=flavor)
        return flavor_id

    def _get_keypair(self, key_name):
        for keypair in self.nova().keypairs.list():
            if keypair.name == key_name:
                return keypair
        raise exception.UserKeyPairMissing(key_name=key_name)

    def handle_create(self):
        security_groups = self._get_security_groups()

        userdata = self.properties['UserData'] or ''
        flavor = self.properties['InstanceType']
        availability_zone = self.properties['AvailabilityZone']

        key_name = self.properties['KeyName']
        if key_name:
            # confirm keypair exists
            self._get_keypair(key_name)

        image_name = self.properties['ImageId']

        image_id = self._get_image_id(image_name)

        flavor_id = self._get_flavor_id(flavor)

        tags = {}
        if self.properties['Tags']:
            for tm in self.properties['Tags']:
                tags[tm['Key']] = tm['Value']
        else:
            tags = None

        scheduler_hints = {}
        if self.properties['NovaSchedulerHints']:
            for tm in self.properties['NovaSchedulerHints']:
                scheduler_hints[tm['Key']] = tm['Value']
        else:
            scheduler_hints = None

        nics = self._build_nics(self.properties['NetworkInterfaces'],
                                subnet_id=self.properties['SubnetId'])

        server_userdata = self._build_userdata(userdata)
        server = None
        try:
            server = self.nova().servers.create(
                name=self.physical_resource_name(),
                image=image_id,
                flavor=flavor_id,
                key_name=key_name,
                security_groups=security_groups,
                userdata=server_userdata,
                meta=tags,
                scheduler_hints=scheduler_hints,
                nics=nics,
                availability_zone=availability_zone)
        finally:
            # Avoid a race condition where the thread could be cancelled
            # before the ID is stored
            if server is not None:
                self.resource_id_set(server.id)

        return server, scheduler.TaskRunner(self._attach_volumes_task())

    def _attach_volumes_task(self):
        attach_tasks = (volume.VolumeAttachTask(self.stack,
                                                self.resource_id,
                                                volume_id,
                                                device)
                        for volume_id, device in self.volumes())
        return scheduler.PollingTaskGroup(attach_tasks)

    def check_create_complete(self, cookie):
        return self._check_active(cookie)

    def _check_active(self, cookie):
        server, volume_attach = cookie

        if not volume_attach.started():
            if server.status != 'ACTIVE':
                server.get()

            # Some clouds append extra (STATUS) strings to the status
            short_server_status = server.status.split('(')[0]
            if short_server_status in self._deferred_server_statuses:
                return False
            elif server.status == 'ACTIVE':
                self._set_ipaddress(server.networks)
                volume_attach.start()
                return volume_attach.done()
            elif server.status == 'ERROR':
                delete = scheduler.TaskRunner(self._delete_server, server)
                delete(wait_time=0.2)
                exc = exception.Error("Build of server %s failed." %
                                      server.name)
                raise exception.ResourceFailure(exc)
            else:
                exc = exception.Error('%s instance[%s] status[%s]' %
                                      ('nova reported unexpected',
                                       self.name, server.status))
                raise exception.ResourceFailure(exc)
        else:
            return volume_attach.step()

    def volumes(self):
        """
        Return an iterator over (volume_id, device) tuples for all volumes
        that should be attached to this instance.
        """
        volumes = self.properties['Volumes']
        if volumes is None:
            return []

        return ((vol['VolumeId'], vol['Device']) for vol in volumes)

    def handle_update(self, json_snippet, tmpl_diff, prop_diff):
        if 'Metadata' in tmpl_diff:
            self.metadata = tmpl_diff['Metadata']
        if 'InstanceType' in prop_diff:
            flavor = prop_diff['InstanceType']
            flavor_id = self._get_flavor_id(flavor)
            server = self.nova().servers.get(self.resource_id)
            server.resize(flavor_id)
            scheduler.TaskRunner(self._check_resize, server, flavor)()

    def _check_resize(self, server, flavor):
        """
        Verify that the server is properly resized. If that's the case, confirm
        the resize, if not raise an error.
        """
        yield
        server.get()
        while server.status == 'RESIZE':
            yield
            server.get()
        if server.status == 'VERIFY_RESIZE':
            server.confirm_resize()
        else:
            raise exception.Error(
                "Resizing to '%s' failed, status '%s'" % (
                    flavor, server.status))

    def metadata_update(self, new_metadata=None):
        '''
        Refresh the metadata if new_metadata is None
        '''
        if new_metadata is None:
            self.metadata = self.parsed_template('Metadata')

    def validate(self):
        '''
        Validate any of the provided params
        '''
        res = super(Instance, self).validate()
        if res:
            return res

        # check validity of key
        key_name = self.properties.get('KeyName', None)
        if key_name:
            keypairs = self.nova().keypairs.list()
            if not any(k.name == key_name for k in keypairs):
                raise exception.UserKeyPairMissing(key_name=key_name)

        # check validity of security groups vs. network interfaces
        security_groups = self._get_security_groups()
        if security_groups and self.properties.get('NetworkInterfaces'):
            raise exception.ResourcePropertyConflict(
                'SecurityGroups/SecurityGroupIds',
                'NetworkInterfaces')

        # make sure the image exists.
        image_identifier = self.properties['ImageId']
        self._get_image_id(image_identifier)

        return

    def _delete_server(self, server):
        '''
        Return a co-routine that deletes the server and waits for it to
        disappear from Nova.
        '''
        server.delete()

        while True:
            yield

            try:
                server.get()
            except clients.novaclient.exceptions.NotFound:
                break

    def _detach_volumes_task(self):
        '''
        Detach volumes from the instance
        '''
        detach_tasks = (volume.VolumeDetachTask(self.stack,
                                                self.resource_id,
                                                volume_id)
                        for volume_id, device in self.volumes())
        return scheduler.PollingTaskGroup(detach_tasks)

    def handle_delete(self):
        '''
        Delete an instance, blocking until it is disposed by OpenStack
        '''
        if self.resource_id is None:
            return

        scheduler.TaskRunner(self._detach_volumes_task())()

        try:
            server = self.nova().servers.get(self.resource_id)
        except clients.novaclient.exceptions.NotFound:
            pass
        else:
            delete = scheduler.TaskRunner(self._delete_server, server)
            delete(wait_time=0.2)

        self.resource_id = None

    def _get_image_id(self, image_identifier):
        image_id = None
        if uuidutils.is_uuid_like(image_identifier):
            try:
                image_id = self.nova().images.get(image_identifier).id
            except clients.novaclient.exceptions.NotFound:
                logger.info("Image %s was not found in glance"
                            % image_identifier)
                raise exception.ImageNotFound(image_name=image_identifier)
        else:
            try:
                image_list = self.nova().images.list()
            except clients.novaclient.exceptions.ClientException as ex:
                raise exception.ServerError(message=str(ex))
            image_names = dict(
                (o.id, o.name)
                for o in image_list if o.name == image_identifier)
            if len(image_names) == 0:
                logger.info("Image %s was not found in glance" %
                            image_identifier)
                raise exception.ImageNotFound(image_name=image_identifier)
            elif len(image_names) > 1:
                logger.info("Mulitple images %s were found in glance with name"
                            % image_identifier)
                raise exception.NoUniqueImageFound(image_name=image_identifier)
            image_id = image_names.popitem()[0]
        return image_id

    def handle_suspend(self):
        '''
        Suspend an instance - note we do not wait for the SUSPENDED state,
        this is polled for by check_suspend_complete in a similar way to the
        create logic so we can take advantage of coroutines
        '''
        if self.resource_id is None:
            raise exception.Error(_('Cannot suspend %s, resource_id not set') %
                                  self.name)

        try:
            server = self.nova().servers.get(self.resource_id)
        except clients.novaclient.exceptions.NotFound:
            raise exception.NotFound(_('Failed to find instance %s') %
                                     self.resource_id)
        else:
            logger.debug("suspending instance %s" % self.resource_id)
            # We want the server.suspend to happen after the volume
            # detachement has finished, so pass both tasks and the server
            suspend_runner = scheduler.TaskRunner(server.suspend)
            volumes_runner = scheduler.TaskRunner(self._detach_volumes_task())
            return server, suspend_runner, volumes_runner

    def check_suspend_complete(self, cookie):
        server, suspend_runner, volumes_runner = cookie

        if not volumes_runner.started():
            volumes_runner.start()

        if volumes_runner.done():
            if not suspend_runner.started():
                suspend_runner.start()

            if suspend_runner.done():
                if server.status == 'SUSPENDED':
                    return True

                server.get()
                logger.debug("%s check_suspend_complete status = %s" %
                             (self.name, server.status))
                if server.status in list(self._deferred_server_statuses +
                                         ['ACTIVE']):
                    return server.status == 'SUSPENDED'
                else:
                    raise exception.Error(_(' nova reported unexpected '
                                            'instance[%(instance)s] '
                                            'status[%(status)s]') %
                                          {'instance': self.name,
                                           'status': server.status})
            else:
                suspend_runner.step()
        else:
            return volumes_runner.step()

    def handle_resume(self):
        '''
        Resume an instance - note we do not wait for the ACTIVE state,
        this is polled for by check_resume_complete in a similar way to the
        create logic so we can take advantage of coroutines
        '''
        if self.resource_id is None:
            raise exception.Error(_('Cannot resume %s, resource_id not set') %
                                  self.name)

        try:
            server = self.nova().servers.get(self.resource_id)
        except clients.novaclient.exceptions.NotFound:
            raise exception.NotFound(_('Failed to find instance %s') %
                                     self.resource_id)
        else:
            logger.debug("resuming instance %s" % self.resource_id)
            server.resume()
            return server, scheduler.TaskRunner(self._attach_volumes_task())

    def check_resume_complete(self, cookie):
        return self._check_active(cookie)


def resource_mapping():
    return {
        'AWS::EC2::Instance': Instance,
        'OS::Heat::HARestarter': Restarter,
    }
