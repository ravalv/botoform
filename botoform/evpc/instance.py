import re

from ..util import reflect_attrs

class EnrichedInstance(object):
    """
    This class uses composition to enrich Boto3's ec2.Instance resource class.
    """

    def __init__(self, instance, vpc=None):
        """Composted ec2.Instance(boto3.resources.base.ServiceResource) class"""
        if vpc is not None:
            self.vpc = vpc
        self.instance = instance
        # reflect all attributes of ec2.Instance into self.
        reflect_attrs(self, self.instance)

    def __eq__(self, other):
        """Determine if equal by instance id"""
        return self.id == other.id

    def __ne__(self, other):
        """Determine if not equal by instance id"""
        return (not self.__eq__(other))

    def __hash__(self):
        return hash(self.id)

    @property
    def tag_dict(self):
        tags = {}
        for tag in self.instance.tags:
            tags[tag['Key']] = tag['Value']
        return tags

    @property
    def hostname(self):
        return self.tag_dict.get('Name', None)

    @property
    def shortname(self):
        """get shortname from instance Name tag, ex: proxy02, web01, ..."""
        return self._regex_hostname(r".*?-(.*)$")

    @property
    def role(self):
        """get role from instance Name tag, ex: api, vpn, ..."""
        #if self.is_autoscale:
        #    return self.autoscale_groupname.split('-')[-1]
        return self._regex_hostname(r".*?-(.*?)\d+$")

    def _regex_hostname(self, regex):
        if self.hostname is None:
            return None
        match = re.match(regex, self.hostname)
        if match is None:
            raise Exception(
              "Invalid Name=%s tag, custid-<role>NN" % (self.hostname)
            )

    
