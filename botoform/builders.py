from botoform.enriched import EnrichedVPC

from botoform.util import (
  BotoConnections,
  Log,
  update_tags,
  make_tag_dict,
  get_port_range,
  get_ids,
  collection_len,
)

from botoform.subnetallocator import allocate

import traceback

class EnvironmentBuilder(object):

    def __init__(self, vpc_name, config=None, region_name=None, profile_name=None, log=None):
        """
        vpc_name:
         The human readable Name tag of this VPC.

        config:
         The dict returned by botoform.config.ConfigLoader's load method.
        """
        self.vpc_name = vpc_name
        self.config = config if config is not None else {}
        self.log = log if log is not None else Log()
        self.boto = BotoConnections(region_name, profile_name)
        self.reflect = False

    def apply_all(self):
        """Build the environment specified in the config."""
        try:
            self._apply_all(self.config)
        except Exception as e:
            self.log.emit('Botoform failed to build environment!', 'error')
            self.log.emit('Failure reason: {}'.format(e), 'error')
            self.log.emit(traceback.format_exc(), 'debug')
            self.log.emit('Tearing down failed environment!', 'error')
            #self.evpc.terminate()
            raise

    def _apply_all(self, config):

        # Make sure amis is setup early. (TODO: raise exception if missing)
        self.amis = config['amis']

        # set a var for no_cfg.
        no_cfg = {}

        # attach EnrichedVPC to self.
        self.evpc = EnrichedVPC(self.vpc_name, self.boto.region_name, self.boto.profile_name)

        # the order of these method calls matters for new VPCs.
        self.route_tables(config.get('route_tables', no_cfg))
        self.subnets(config.get('subnets', no_cfg))
        self.associate_route_tables_with_subnets(config.get('subnets', no_cfg))
        self.endpoints(config.get('endpoints', []))
        self.security_groups(config.get('security_groups', no_cfg))
        self.instance_roles(config.get('instance_roles', no_cfg))
        self.security_group_rules(config.get('security_groups', no_cfg))

        for instance in self.evpc.instances:
            self.log.emit('waiting for {} to start'.format(instance.identity))
            instance.wait_until_running()

        try:
            self.log.emit('locking instances to prevent termination')
            self.evpc.lock_instances()
        except:
            self.log.emit('could not lock instances, continuing...', 'warning')

    def build_vpc(self, cidrblock):
        """Build VPC"""
        self.log.emit('creating vpc ({}, {})'.format(self.vpc_name, cidrblock))
        vpc = self.boto.ec2.create_vpc(CidrBlock = cidrblock)

        self.log.emit('tagging vpc (Name:{})'.format(self.vpc_name), 'debug')
        update_tags(vpc, Name = self.vpc_name)

        self.log.emit('modifying vpc for dns support', 'debug')
        vpc.modify_attribute(EnableDnsSupport={'Value': True})
        self.log.emit('modifying vpc for dns hostnames', 'debug')
        vpc.modify_attribute(EnableDnsHostnames={'Value': True})

        igw_name = 'igw-' + self.vpc_name
        self.log.emit('creating internet_gateway ({})'.format(igw_name))
        gw = self.boto.ec2.create_internet_gateway()
        self.log.emit('tagging gateway (Name:{})'.format(igw_name), 'debug')
        update_tags(gw, Name = igw_name)

        self.log.emit('attaching igw to vpc ({})'.format(igw_name))
        vpc.attach_internet_gateway(
            DryRun=False,
            InternetGatewayId=gw.id,
            VpcId=vpc.id,
        )

    def route_tables(self, route_cfg):
        """Build route_tables defined in config"""
        for name, data in route_cfg.items():
            longname = '{}-{}'.format(self.evpc.name, name)
            route_table = self.evpc.get_route_table(longname)
            if route_table is None:
                self.log.emit('creating route_table ({})'.format(longname))
                if data.get('main', False) == True:
                    route_table = self.evpc.get_main_route_table()
                else:
                    route_table = self.evpc.create_route_table()
                self.log.emit('tagging route_table (Name:{})'.format(longname), 'debug')
                update_tags(route_table, Name = longname)

    def subnets(self, subnet_cfg):
        """Build subnets defined in config."""
        sizes = sorted([x['size'] for x in subnet_cfg.values()])
        cidrs = allocate(self.evpc.cidr_block, sizes)

        azones = self.evpc.azones

        subnets = {}
        for size, cidr in zip(sizes, cidrs):
            subnets.setdefault(size, []).append(cidr)

        for name, sn in subnet_cfg.items():
            longname = '{}-{}'.format(self.evpc.name, name)
            az_letter = sn.get('availability_zone', None)
            if az_letter is not None:
                az_name = self.evpc.region_name + az_letter
            else:
                az_index = int(name.split('-')[-1]) - 1
                az_name = azones[az_index]

            cidr = subnets[sn['size']].pop()
            self.log.emit('creating subnet {} in {}'.format(cidr, az_name))
            subnet = self.evpc.create_subnet(
                          CidrBlock = str(cidr),
                          AvailabilityZone = az_name
            )
            self.log.emit('tagging subnet (Name:{})'.format(longname), 'debug')
            update_tags(
                subnet,
                Name = longname,
                description = sn.get('description', ''),
            )

    def associate_route_tables_with_subnets(self, subnet_cfg):
        for sn_name, sn_data in subnet_cfg.items():
            rt_name = sn_data.get('route_table', None)
            if rt_name is None:
                continue
            self.log.emit('associating rt {} with sn {}'.format(rt_name, sn_name))
            self.evpc.associate_route_table_with_subnet(rt_name, sn_name)

    def endpoints(self, route_tables):
        """Build VPC endpoints for given route_tables"""
        if len(route_tables) == 0:
            return None
        self.log.emit(
            'creating vpc endpoints in {}'.format(', '.join(route_tables))
        )
        self.evpc.vpc_endpoint.create_all(route_tables)

    def security_groups(self, security_group_cfg):
        """Build Security Groups defined in config."""

        for sg_name, rules in security_group_cfg.items():
            sg = self.evpc.get_security_group(sg_name)
            if sg is not None:
                continue
            longname = '{}-{}'.format(self.evpc.name, sg_name)
            self.log.emit('creating security_group {}'.format(longname))
            security_group = self.evpc.create_security_group(
                GroupName   = longname,
                Description = longname,
            )
            self.log.emit(
                'tagging security_group (Name:{})'.format(longname), 'debug'
            )
            update_tags(security_group, Name = longname)

    def security_group_rules(self, security_group_cfg):
        """Build Security Group Rules defined in config."""
        msg = "'{}' into '{}' over ports {} ({})"
        for sg_name, rules in security_group_cfg.items():
            sg = self.evpc.get_security_group(sg_name)
            permissions = []
            for rule in rules:
                protocol = rule[1]
                from_port, to_port = get_port_range(rule[2], protocol)
                src_sg = self.evpc.get_security_group(rule[0])

                permission = {
                    'IpProtocol' : protocol,
                    'FromPort'   : from_port,
                    'ToPort'     : to_port,
                }

                if src_sg is None:
                    permission['IpRanges'] = [{'CidrIp' : rule[0]}]
                else:
                    permission['UserIdGroupPairs'] = [{'GroupId':src_sg.id}]

                permissions.append(permission)

                fmsg = msg.format(rule[0],sg_name,rule[2],rule[1].upper())
                self.log.emit(fmsg)

            sg.authorize_ingress(
                IpPermissions = permissions
            )

    def instance_roles(self, instance_role_cfg):
        for role_name, role_data in instance_role_cfg.items():
            desired_count = role_data.get('count', 0)
            self.instance_role(role_name, role_data, desired_count)

    def instance_role(self, role_name, role_data, desired_count):
        ami = self.amis[role_data['ami']][self.evpc.region_name]

        security_groups = map(
            self.evpc.get_security_group,
            role_data.get('security_groups', [])
        )

        subnets = map(
            self.evpc.get_subnet,
            role_data.get('subnets', [])
        )

        if len(subnets) == 0:
            self.log.emit(
                'no subnets found for role: {}'.format(role_name), 'warning'
            )
            # exit early.
            return None

        # sort by subnets by amount of instances, smallest first.
        subnets = sorted(
                      subnets,
                      key = lambda sn : collection_len(sn.instances),
                  )

        # determine the count of this role's existing instances.
        existing_count = sum(
                             map(
                                 lambda sn : collection_len(sn.instances),
                                 subnets,
                             )
                         )

        if existing_count >= desired_count:
            # for now we exit early, maybe terminate extras...
            return None

        # determine count of additional instances needed to reach desired_count.
        needed_count      = desired_count - existing_count
        needed_per_subnet = needed_count / len(subnets)
        needed_remainder  = needed_count % len(subnets)

        tag_msg = 'tagging instance {} (Name:{}, role:{})'
        for subnet in subnets:
            count = needed_per_subnet - collection_len(subnet.instances)
            if needed_remainder != 0:
                needed_remainder -= 1
                count += 1

            subnet_name = make_tag_dict(subnet)['Name']
            msg = '{} instances of role {} launching into {}'
            self.log.emit(msg.format(count, role_name, subnet_name))

            # create a batch of instances in subnet!
            instances = subnet.create_instances(
                       ImageId           = ami,
                       InstanceType      = role_data.get('instance_type'),
                       MinCount          = count,
                       MaxCount          = count,
                       KeyName           = self.evpc.key_name,
                       SecurityGroupIds = get_ids(security_groups)
            )

            for instance in instances:
                instance_id = instance.id.lstrip('i-')
                hostname = self.evpc.name + '-' + role_name + '-' + instance_id
                self.log.emit(tag_msg.format(instance.id, hostname, role_name))
                update_tags(instance, Name = hostname, role = role_name)


